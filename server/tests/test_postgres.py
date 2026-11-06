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
from app.services.retrieval import search, hybrid_search, hybrid_search_scored
from app.errors import AppError
from app.adapters.github import ResolvedRepository

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
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ResolvedRepository("owner", "repo", "main", "a" * 40, "refs/heads/main"))
    def submit_once(_):
        with SessionLocal() as db:
            return snapshots.submit(db, "https://github.com/owner/repo", "user1").snapshot.id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit_once, range(4)))
    assert len(set(ids)) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Snapshot)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(Quota.count).where(Quota.scope == "user:user1", Quota.action == "snapshots")) == 1


def test_branch_and_tag_aliases_share_snapshot_but_keep_request_ref(monkeypatch):
    refs = iter(["refs/heads/develop", "refs/tags/v1.0"])
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ResolvedRepository("o", "r", "main", "a" * 40, next(refs)))
    with SessionLocal() as db:
        branch = snapshots.submit(db, "https://github.com/o/r/tree/develop", "user")
        tag = snapshots.submit(db, "https://github.com/o/r/tree/v1.0", "user")
        assert branch.snapshot.id == tag.snapshot.id
        assert branch.ref == "refs/heads/develop" and tag.ref == "refs/tags/v1.0"
        assert db.scalar(select(Repository.default_branch)) == "main"
        assert db.scalar(select(func.count()).select_from(Job)) == 1

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


def test_optional_search_extension_unavailable_does_not_abort_migration():
    """Exercise the actual missing-control-file SQLSTATE in PostgreSQL, without mocks."""
    import importlib.util
    from pathlib import Path
    from sqlalchemy.exc import DBAPIError

    path = Path(__file__).parents[1] / "migrations/versions/0004_agentic_rag.py"
    spec = importlib.util.spec_from_file_location("agentic_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    missing = "gitlens_missing_optional_search"
    with SessionLocal() as db:
        with pytest.raises(DBAPIError) as error:
            with db.begin_nested():
                db.execute(text(f"CREATE EXTENSION {missing}"))
        assert error.value.orig.sqlstate == "0A000"
        db.execute(text(migration.OPTIONAL_PG_SEARCH_SQL.replace("pg_search", missing)))
        assert db.scalar(text("SELECT 1")) == 1
        assert db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
        indexes = dict(db.execute(text("""
            SELECT c.relname, a.amname FROM pg_class c
            JOIN pg_am a ON a.oid=c.relam
            WHERE c.relname IN ('ix_chunks_search', 'ix_chunk_embeddings_hnsw')
        """)).all())
        assert indexes == {"ix_chunks_search": "gin", "ix_chunk_embeddings_hnsw": "hnsw"}


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_scored_fts_fallback_without_pg_search(limit, monkeypatch):
    """The full result path must retain scores and isolate snapshot/index versions."""
    from app.services import retrieval

    def unexpected_embedding(*_args):
        pytest.fail("FTS-only retrieval must not call the embedding provider")

    monkeypatch.setattr(retrieval, "embed_texts", unexpected_embedding)
    with SessionLocal() as db:
        if db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='pg_search'")):
            pytest.skip("This regression requires PostgreSQL without pg_search")
        repo = Repository(owner="o", name="r", canonical_url="https://github.com/o/r", default_branch="main")
        db.add(repo); db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="a" * 40, status="ready", summary={},
                            index_version=2, index_report={"keywordSearch": "postgres_fts", "semantic": "bm25_only"})
        other = Snapshot(repository_id=repo.id, sha="b" * 40, status="ready", summary={})
        db.add_all([snapshot, other]); db.flush()
        db.add_all([Chunk(snapshot_id=snapshot.id, index_version=2, path=f"src/file{i}.py",
                          line_start=1, line_end=1, text="migrationneedle") for i in range(3)])
        db.add_all([
            Chunk(snapshot_id=snapshot.id, index_version=1, path="stale.py", line_start=1, line_end=1, text="migrationneedle"),
            Chunk(snapshot_id=other.id, path="other.py", line_start=1, line_end=1, text="migrationneedle"),
        ])
        db.commit()
        ranked = hybrid_search_scored(db, snapshot.id, "migrationneedle", limit)
        assert len(ranked) == limit
        assert all(isinstance(score, float) and score > 0 for _, score in ranked)
        assert all(chunk.path.startswith("src/") and chunk.index_version == 2 for chunk, _ in ranked)
        assert [chunk.id for chunk in hybrid_search(db, snapshot.id, "migrationneedle", limit)] == [chunk.id for chunk, _ in ranked]

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
    monkeypatch.setattr(jobs, "summarize_repository_nodes", lambda *_args: None)
    monkeypatch.setattr(jobs, "_embed_staged_chunks", lambda *_args: False)
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
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ResolvedRepository("o", "r", "main", "d" * 40, "refs/heads/main"))
    monkeypatch.setattr(jobs, "download_archive", lambda *_args: make_zip("root/app.py", "def answer(): return 42"))
    monkeypatch.setattr(jobs, "make_summary", lambda *_args: {"purpose": "A test project", "components": [], "entrypoints": ["app.py"], "technologies": ["Python"], "uncertainty": "", "suggestedQuestions": []})
    monkeypatch.setattr(jobs, "summarize_repository_nodes", lambda *_args: None)
    monkeypatch.setattr(jobs, "_embed_staged_chunks", lambda *_args: False)
    def fake_tool_turn(_instruction, contents, _tools, **_kwargs):
        import re
        text = contents[0]["parts"][0]["text"]
        match = re.search(r"CHUNK ([0-9a-f-]+) FILE", text)
        return {"content": {"role": "model", "parts": []}, "calls": [{"name": "final_answer", "args": {
            "answer": "The answer is in app.py.", "citedChunkIds": [match.group(1)] if match else [],
            "insufficientEvidence": not bool(match)}}]}
    monkeypatch.setattr(rag, "generate_tool_turn", fake_tool_turn)
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    challenge = client.post("/api/auth/code", json={"email": "user@example.com"}).json()["challengeId"]
    token = client.post("/api/auth/verify", json={"challengeId": challenge, "code": sent[0]}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).json()["email"] == "user@example.com"
    submitted = client.post("/api/repositories", json={"url": "https://github.com/o/r"}, headers=headers)
    assert submitted.status_code == 200
    assert submitted.json()["ref"] == "refs/heads/main"
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
