import os
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select, func, text
from fastapi.testclient import TestClient
from app.db import SessionLocal
from app.models import Chunk, CodeChallenge, Job, Quota, Repository, Snapshot, User, now
from app.main import app
from app.services import auth, snapshots, jobs, rag
from app.services.retrieval import search
from app.errors import AppError

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database not configured")

@pytest.fixture(autouse=True)
def clean_database():
    with SessionLocal() as db:
        db.execute(text("TRUNCATE chunks, jobs, snapshots, repositories, quotas, sessions, code_challenges, users CASCADE"))
        db.commit()
    yield

def test_auth_code_expiry_attempts_and_revocation(monkeypatch):
    sent = []
    monkeypatch.setattr(auth, "send_code", lambda email, code: sent.append((email, code)))
    with SessionLocal() as db:
        challenge_id = auth.request_code(db, "TEST@example.com", "127.0.0.1")
        assert len(sent) == 1
        assert auth.request_code(db, "test@example.com", "127.0.0.1") == challenge_id
        assert len(sent) == 1
        with pytest.raises(AppError): auth.verify_code(db, challenge_id, "000000" if sent[0][1] != "000000" else "111111")
        token, _expiry = auth.verify_code(db, challenge_id, sent[0][1])
        assert auth.authenticate(db, token).email == "test@example.com"
        auth.logout(db, token)
        with pytest.raises(AppError): auth.authenticate(db, token)
        expired = CodeChallenge(email="x@example.com", code_hash="bad", expires_at=now() - timedelta(seconds=1))
        db.add(expired); db.commit()
        with pytest.raises(AppError): auth.verify_code(db, expired.id, "123456")


def test_github_oauth_identity_session_and_repeat_login(monkeypatch):
    monkeypatch.setattr(auth, "identify", lambda code, verifier: ("12345", "octocat"))
    monkeypatch.setattr(auth.settings, "github_oauth_client_id", "public-client-id")
    monkeypatch.setattr(auth.settings, "github_oauth_client_secret", "server-only-secret")
    with TestClient(app) as client:
        config = client.get("/api/auth/github/config")
        assert config.status_code == 200
        assert config.json()["clientId"] == "public-client-id"
        assert "clientSecret" not in config.json()
        ids = []
        for _ in range(2):
            result = client.post("/api/auth/github", json={"code": "test-code", "codeVerifier": "a" * 43})
            assert result.status_code == 200
            headers = {"Authorization": f"Bearer {result.json()['token']}"}
            me = client.get("/api/auth/me", headers=headers).json()
            assert me["githubLogin"] == "octocat" and me["email"] is None
            ids.append(me["id"])
            assert client.post("/api/auth/logout", headers=headers).status_code == 200
            assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert ids[0] == ids[1]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1

def test_simultaneous_submission_creates_one_snapshot_and_job(monkeypatch):
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ("owner", "repo", "main", "a" * 40))
    def submit_once(_):
        with SessionLocal() as db:
            return snapshots.submit(db, "https://github.com/owner/repo", "user1").id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit_once, range(4)))
    assert len(set(ids)) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Snapshot)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(Quota.count).where(Quota.scope == "user:user1", Quota.action == "snapshots")) == 1

def test_search_never_crosses_commits():
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        old = Snapshot(repository_id=repo.id, sha="a" * 40, status="ready", summary={"purpose": "x"})
        new = Snapshot(repository_id=repo.id, sha="b" * 40, status="ready", summary={"purpose": "y"})
        db.add_all([old, new]); db.flush()
        db.add_all([Chunk(snapshot_id=old.id, path="old.py", line_start=1, line_end=1, text="unique oldsymbol"), Chunk(snapshot_id=new.id, path="new.py", line_start=1, line_end=1, text="unique newsymbol")]); db.commit()
        assert [c.path for c in search(db, old.id, "oldsymbol")] == ["old.py"]
        assert search(db, old.id, "newsymbol") == []


def test_search_finds_named_file_with_short_content():
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="9" * 40, status="ready", summary={"purpose": "test"})
        db.add(snapshot); db.flush()
        db.add(Chunk(snapshot_id=snapshot.id, path="README", line_start=1, line_end=1, text="Hello World!"))
        db.commit()
        assert [c.path for c in search(db, snapshot.id, "What does the README say?")] == ["README"]

def test_worker_retries_and_rejects_stale_lease(monkeypatch):
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="c" * 40, status="queued")
        db.add(snapshot); db.flush()
        db.add(Job(snapshot_id=snapshot.id, status="queued")); db.commit()
        snapshot_id = snapshot.id
    with SessionLocal() as db:
        job_id, old_token = jobs.claim(db)
        job = db.get(Job, job_id)
        job.lease_until = now() - timedelta(seconds=1)
        db.commit()
    with SessionLocal() as db:
        same_job_id, new_token = jobs.claim(db)
    assert same_job_id == job_id and old_token != new_token
    monkeypatch.setattr(jobs, "download_archive", lambda *_args: make_zip("root/app.py", "def answer(): return 42"))
    monkeypatch.setattr(jobs, "make_summary", lambda *_args: {"purpose": "test"})
    jobs.process(job_id, old_token)
    with SessionLocal() as db:
        assert db.get(Snapshot, snapshot_id).status == "indexing"
        assert db.scalar(select(func.count()).select_from(Chunk)) == 0
    jobs.process(job_id, new_token)
    with SessionLocal() as db:
        assert db.get(Snapshot, snapshot_id).status == "ready"
        assert db.scalar(select(func.count()).select_from(Chunk)) == 1

def test_worker_recovers_exhausted_expired_lease():
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="e" * 40, status="indexing")
        db.add(snapshot); db.flush()
        job = Job(snapshot_id=snapshot.id, status="running", attempts=3, lease_until=now() - timedelta(seconds=1), lease_token="stale")
        db.add(job); db.commit()
        snapshot_id, job_id = snapshot.id, job.id
    with SessionLocal() as db:
        assert jobs.claim(db) is None
        assert db.get(Job, job_id).status == "failed"
        assert db.get(Snapshot, snapshot_id).status == "failed"

def test_two_workers_cannot_claim_same_job():
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="f" * 40, status="queued")
        db.add(snapshot); db.flush()
        db.add(Job(snapshot_id=snapshot.id, status="queued")); db.commit()
    def claim_once(_):
        with SessionLocal() as db:
            return jobs.claim(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_once, range(2)))
    assert sum(result is not None for result in results) == 1

def test_worker_dispatch_is_throttled(monkeypatch):
    monkeypatch.setattr(jobs.settings, "worker_mode", "actions")
    monkeypatch.setattr(jobs.settings, "worker_dispatch_repo", "owner/gitlens")
    dispatched = []
    monkeypatch.setattr(jobs, "dispatch_ingestion", lambda: dispatched.append(True))
    with SessionLocal() as db:
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="1" * 40, status="queued")
        db.add(snapshot); db.flush()
        db.add(Job(snapshot_id=snapshot.id, status="queued")); db.commit()
        jobs.kick(db, snapshot.id)
        jobs.kick(db, snapshot.id)
        assert db.scalar(select(Job.dispatched_at).where(Job.snapshot_id == snapshot.id)) is not None
    assert len(dispatched) == 1

def make_zip(path, content):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(path, content)
    return out.getvalue()

def test_authenticated_end_to_end_contract(monkeypatch):
    sent = []
    monkeypatch.setattr(auth, "send_code", lambda email, code: sent.append(code))
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ("o", "r", "main", "d" * 40))
    monkeypatch.setattr(jobs, "download_archive", lambda *_args: make_zip("root/app.py", "def answer(): return 42"))
    monkeypatch.setattr(jobs, "make_summary", lambda *_args: {"purpose": "A test project", "components": [], "entrypoints": ["app.py"], "technologies": ["Python"], "uncertainty": "", "suggestedQuestions": []})
    monkeypatch.setattr(rag, "generate_json", lambda *_args: {"answer": "The answer is in app.py.", "citedChunkIds": [next(iter(_args[1].split("CHUNK ")[1].split(" ")))], "insufficientEvidence": False})
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    challenge = client.post("/api/auth/code", json={"email": "user@example.com"}).json()["challengeId"]
    token = client.post("/api/auth/verify", json={"challengeId": challenge, "code": sent[0]}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).json()["email"] == "user@example.com"
    submitted = client.post("/api/repositories", json={"url": "https://github.com/o/r"}, headers=headers)
    assert submitted.status_code == 200
    snapshot_id = submitted.json()["snapshotId"]
    assert client.get(f"/api/snapshots/{snapshot_id}/status", headers=headers).json()["status"] == "queued"
    with SessionLocal() as db:
        claimed = jobs.claim(db)
    jobs.process(*claimed)
    assert client.get(f"/api/snapshots/{snapshot_id}/summary", headers=headers).json()["summary"]["purpose"] == "A test project"
    reply = client.post(f"/api/snapshots/{snapshot_id}/chat", json={"question": "Where is answer?"}, headers=headers)
    assert reply.status_code == 200
    assert reply.json()["citations"][0]["url"].endswith("app.py#L1-L1")
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
