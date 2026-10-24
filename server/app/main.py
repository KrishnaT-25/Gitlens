import logging
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text, select, func
from sqlalchemy.orm import Session
from app.config import settings
from app.db import get_db
from app.errors import AppError, app_error_handler
from app.models import User, Job, Snapshot, now
from app.quotas import consume
from app.services.auth import authenticate, github_sign_in, logout, request_code, verify_code
from app.quotas import ip_scope
from app.services.snapshots import submit, get_snapshot, retry_snapshot
from app.services.jobs import kick
from app.services.rag import answer, answerable_suggestions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker = None
    if settings.worker_mode == "inprocess":
        from app.services.worker_runtime import InProcessWorker
        worker = InProcessWorker()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()

app = FastAPI(title="GitLens API", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)
allowed_origins = [origin for origin in (settings.extension_origin, "https://github.com") if origin]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = str(uuid.uuid4())
    request.state.request_id = rid
    start = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    logger.info(json.dumps({"requestId": rid, "method": request.method, "path": request.url.path, "status": response.status_code, "durationMs": round((time.monotonic() - start) * 1000)}))
    return response

@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request fields are invalid."}})

@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled request failure: %s", request.state.request_id, exc_info=exc)
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Unexpected server error."}})

class CodeRequest(BaseModel):
    email: str = Field(max_length=320)
class CodeVerify(BaseModel):
    challengeId: str
    code: str
class GithubExchange(BaseModel):
    code: str = Field(min_length=1, max_length=500)
    codeVerifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
class RepoRequest(BaseModel):
    url: str = Field(max_length=2000)
class HistoryTurn(BaseModel):
    role: str
    content: str = Field(max_length=2000)
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=settings.max_question_chars)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=settings.max_history_turns)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question is blank")
        return value.strip()

def bearer(authorization: str | None = Header(default=None)) -> str | None:
    return authorization[7:] if authorization and authorization.startswith("Bearer ") else None

def current_user(db: Session = Depends(get_db), token: str | None = Depends(bearer)) -> User:
    return authenticate(db, token)

@app.get("/health")
@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/readyz")
def readyz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/internal/metrics")
def metrics(token: str | None = Depends(bearer), db: Session = Depends(get_db)):
    if not settings.admin_token or not token or not hmac.compare_digest(token, settings.admin_token):
        raise AppError("unauthorized", "Unauthorized.", 401)
    jobs = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    snapshots = dict(db.execute(select(Snapshot.status, func.count()).group_by(Snapshot.status)).all())
    return {"jobs": jobs, "snapshots": snapshots}

@app.post("/api/auth/code")
def auth_code(body: CodeRequest, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    challenge_id = request_code(db, body.email, ip)
    return {"challengeId": challenge_id}

@app.post("/api/auth/verify")
def auth_verify(body: CodeVerify, db: Session = Depends(get_db)):
    token, expires_at = verify_code(db, body.challengeId, body.code)
    return {"token": token, "expiresAt": expires_at}

@app.get("/api/auth/github/config")
def github_config():
    if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
        raise AppError("oauth_unconfigured", "GitHub sign-in is not configured.", 503)
    return {"clientId": settings.github_oauth_client_id, "redirectUri": settings.github_oauth_redirect_uri}

@app.post("/api/auth/github")
def github_exchange(body: GithubExchange, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    consume(db, ip_scope(ip, settings.session_pepper), "oauth", 10, now().strftime("%Y-%m-%dT%H"))
    consume(db, "global", "oauth", 200)
    db.commit()
    token, expires_at = github_sign_in(db, body.code, body.codeVerifier)
    return {"token": token, "expiresAt": expires_at}

@app.get("/api/auth/me")
def auth_me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "githubLogin": user.github_login}

@app.post("/api/auth/logout")
def auth_logout(token: str | None = Depends(bearer), _user: User = Depends(current_user), db: Session = Depends(get_db)):
    logout(db, token)
    return {"ok": True}

@app.post("/api/repositories")
def repositories(body: RepoRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    snapshot = submit(db, body.url, user.id)
    kick(db, snapshot.id)
    return {"snapshotId": snapshot.id, "sha": snapshot.sha, "status": snapshot.status}

@app.get("/api/snapshots/{snapshot_id}/status")
def snapshot_status(snapshot_id: str, _user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_snapshot(db, snapshot_id)
    if s.status == "queued":
        kick(db, s.id)
    return {"snapshotId": s.id, "sha": s.sha, "status": s.status, "errorCode": s.error_code}

@app.post("/api/snapshots/{snapshot_id}/retry")
def snapshot_retry(snapshot_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = retry_snapshot(db, snapshot_id, user.id)
    kick(db, s.id)
    return {"snapshotId": s.id, "sha": s.sha, "status": s.status}

@app.get("/api/snapshots/{snapshot_id}/summary")
def snapshot_summary(snapshot_id: str, _user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_snapshot(db, snapshot_id)
    if s.status != "ready" or not s.summary:
        raise AppError("not_ready", "Repository summary is not ready.", 409)
    summary = dict(s.summary)
    summary["suggestedQuestions"] = answerable_suggestions(db, s.id, summary.get("suggestedQuestions", []))
    return {"sha": s.sha, "summary": summary}

@app.post("/api/snapshots/{snapshot_id}/chat")
def snapshot_chat(snapshot_id: str, body: ChatRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_snapshot(db, snapshot_id)
    if s.status != "ready":
        raise AppError("not_ready", "Repository analysis is not ready.", 409)
    consume(db, f"user:{user.id}", "chats", settings.daily_user_chats)
    consume(db, "global", "chats", settings.daily_global_chats)
    db.commit()
    return answer(db, s, body.question.strip(), [h.model_dump() for h in body.history])
