import hmac
import re
import secrets
from datetime import timedelta
from hashlib import sha256
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.config import settings
from app.errors import AppError
from app.models import CodeChallenge, SessionToken, User, now, uid
from app.quotas import consume, ip_scope
from app.adapters.email import send_code
from app.adapters.github_oauth import identify

EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}$")

def code_digest(challenge_id: str, code: str) -> str:
    return hmac.new(settings.auth_pepper.encode(), f"{challenge_id}:{code}".encode(), sha256).hexdigest()

def token_digest(token: str) -> str:
    return hmac.new(settings.session_pepper.encode(), token.encode(), sha256).hexdigest()

def request_code(db: Session, email: str, ip: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise AppError("invalid_email", "Enter a valid email address.")
    recent = db.execute(select(CodeChallenge).where(CodeChallenge.email == email, CodeChallenge.consumed_at.is_(None)).order_by(CodeChallenge.created_at.desc()).limit(1)).scalar_one_or_none()
    if recent and recent.created_at > now() - timedelta(seconds=60) and recent.expires_at > now():
        return recent.id
    hour = now().strftime("%Y-%m-%dT%H")
    consume(db, "email:" + sha256(email.encode()).hexdigest(), "otp", 3, hour)
    consume(db, ip_scope(ip, settings.auth_pepper), "otp", 10, hour)
    consume(db, "global", "otp", 80)
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge = CodeChallenge(email=email, code_hash="", expires_at=now() + timedelta(minutes=10))
    db.add(challenge)
    db.flush()
    challenge.code_hash = code_digest(challenge.id, code)
    send_code(email, code)
    db.commit()
    return challenge.id

def verify_code(db: Session, challenge_id: str, code: str) -> tuple[str, str]:
    challenge = db.execute(select(CodeChallenge).where(CodeChallenge.id == challenge_id).with_for_update()).scalar_one_or_none()
    if not challenge or challenge.consumed_at or challenge.expires_at < now() or challenge.attempts >= 5:
        raise AppError("invalid_code", "Code invalid or expired.", 401)
    challenge.attempts += 1
    if not re.fullmatch(r"\d{6}", code) or not hmac.compare_digest(challenge.code_hash, code_digest(challenge.id, code)):
        db.commit()
        raise AppError("invalid_code", "Code invalid or expired.", 401)
    challenge.consumed_at = now()
    user = db.execute(select(User).where(User.email == challenge.email)).scalar_one_or_none()
    if not user:
        user = User(email=challenge.email)
        db.add(user)
        db.flush()
    return issue_session(db, user.id)


def issue_session(db: Session, user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expiry = now() + timedelta(days=7)
    db.add(SessionToken(user_id=user_id, token_hash=token_digest(token), expires_at=expiry))
    db.commit()
    return token, expiry.isoformat()


def github_sign_in(db: Session, code: str, verifier: str) -> tuple[str, str]:
    github_id, login = identify(code, verifier)
    user_id = db.execute(
        insert(User).values(id=uid(), github_id=github_id, github_login=login, created_at=now())
        .on_conflict_do_update(index_elements=[User.github_id], set_={"github_login": login})
        .returning(User.id)
    ).scalar_one()
    return issue_session(db, user_id)

def authenticate(db: Session, token: str | None) -> User:
    if not token:
        raise AppError("unauthorized", "Sign in to continue.", 401)
    session = db.execute(select(SessionToken).where(SessionToken.token_hash == token_digest(token))).scalar_one_or_none()
    if not session or session.revoked_at or session.expires_at < now():
        raise AppError("unauthorized", "Session expired. Sign in again.", 401)
    user = db.get(User, session.user_id)
    if not user:
        raise AppError("unauthorized", "Session expired. Sign in again.", 401)
    return user

def logout(db: Session, token: str):
    session = db.execute(select(SessionToken).where(SessionToken.token_hash == token_digest(token))).scalar_one_or_none()
    if session:
        session.revoked_at = now()
        db.commit()
