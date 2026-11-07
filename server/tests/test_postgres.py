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


def test_chat_deadline_cancels_sql_and_does_not_leak_to_pooled_connections():
    """Real PostgreSQL statement cancellation; no model/provider involved."""
    import time
    from sqlalchemy.exc import DBAPIError
    from app.deadlines import chat_deadline
    with SessionLocal() as db:
        db.execute(text("SELECT 1")); db.commit()
        start = time.monotonic()
        with chat_deadline(0.1), pytest.raises(DBAPIError) as error:
            db.execute(text("SELECT pg_sleep(2)"))
        elapsed = time.monotonic() - start
        assert error.value.orig.sqlstate == "57014"
        assert elapsed < 1
        db.rollback()
        assert db.scalar(text("SHOW statement_timeout")) == "0"
        assert db.scalar(text("SELECT 1")) == 1


def test_chat_deadline_covers_authentication_and_closes_its_own_session(monkeypatch):
    import threading
    import time
    from app import main
    closed = threading.Event()
    original = main.get_db
    def tracked_database():
        try:
            yield from original()
        finally:
            closed.set()
    def slow_authentication(db, _token):
        db.execute(text("SELECT pg_sleep(2)"))
        pytest.fail("Authentication must not outlive the chat budget")
    monkeypatch.setattr(main, "get_db", tracked_database)
    monkeypatch.setattr(main, "authenticate", slow_authentication)
    monkeypatch.setattr(main.settings, "chat_timeout_seconds", 0.1)
    with TestClient(app) as client:
        start = time.monotonic()
        result = client.post("/api/snapshots/s/chat", json={"question": "test"}, headers={"Authorization": "Bearer mock-only"})
        assert result.status_code == 504
        assert result.json()["error"]["code"] == "chat_timeout"
        assert time.monotonic() - start < 1
        assert closed.wait(2)
    with SessionLocal() as db:
        assert db.scalar(text("SHOW statement_timeout")) == "0"


def embedding_fixture(db, count=5, *, published=True):
    """Build a ready text index whose vectors can be repaired without reingestion."""
    from app.models import SnapshotNode
    repo = Repository(owner="embedding", name="fixture", canonical_url="https://github.com/embedding/fixture", default_branch="main")
    db.add(repo); db.flush()
    snapshot = Snapshot(repository_id=repo.id, sha="a" * 40, status="ready" if published else "indexing", index_version=2,
                        summary={"purpose": "cached"} if published else None,
                        index_report={"keywordSearch": "postgres_fts", "semantic": "ready"} if published else None)
    db.add(snapshot); db.flush()
    chunks = [Chunk(snapshot_id=snapshot.id, index_version=2, path=f"file{i}.py",
                    line_start=1, line_end=1, text=f"source {i}") for i in range(count)]
    db.add_all(chunks)
    db.add(SnapshotNode(snapshot_id=snapshot.id, index_version=2, path="", parent_path="",
                        node_type="directory", summary="fixture", metadata_json={}))
    job = Job(snapshot_id=snapshot.id, status="running", attempts=1, lease_token="owner", lease_until=now() + timedelta(minutes=15))
    db.add(job); db.commit()
    return snapshot, chunks, job


def test_compact_repository_map_prioritizes_evidence_paths_and_exact_snapshot():
    from app.models import SnapshotNode
    with SessionLocal() as db:
        snapshot, _chunks, _job = embedding_fixture(db, 1)
        other = Snapshot(repository_id=snapshot.repository_id, sha="b" * 40, status="ready", summary={})
        db.add(other); db.flush()
        db.add_all([SnapshotNode(snapshot_id=snapshot.id, index_version=2, path=f"file{i:04}.py", parent_path="",
                                node_type="file", summary="unrelated") for i in range(350)])
        db.add_all([
            SnapshotNode(snapshot_id=snapshot.id, index_version=2, path="zz/target.py", parent_path="zz", node_type="file", summary="Selected source"),
            SnapshotNode(snapshot_id=snapshot.id, index_version=1, path="stale.py", parent_path="", node_type="file", summary="Wrong index"),
            SnapshotNode(snapshot_id=other.id, index_version=2, path="other.py", parent_path="", node_type="file", summary="Wrong snapshot"),
        ])
        db.commit()
        result = rag._repository_map(db, snapshot, ["zz/target.py"])
        assert "FILE zz/target.py — Selected source" in result
        assert "stale.py" not in result and "other.py" not in result
        assert len(result) <= min(jobs.settings.chat_map_chars, jobs.settings.max_repo_map_chars)


def test_embedding_rebuild_checkpoints_and_resumes_only_compatible_vectors(monkeypatch):
    """Real pgvector storage; the provider alone is mocked."""
    from app.adapters.embeddings import embedding_profile
    from app.models import ChunkEmbedding
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "mock-only-key")
    monkeypatch.setattr(jobs.settings, "embedding_batch_size", 2)
    profile = embedding_profile()
    with SessionLocal() as db:
        snapshot, chunks, job = embedding_fixture(db, published=False)
        for i, chunk in enumerate(chunks):
            values = {"model": profile["model"], "fingerprint": profile["fingerprint"],
                      "dimensions": 768, "format_version": profile["formatVersion"]}
            if i == 0: values["fingerprint"] = None
            if i == 1: values["model"] = "old-model"
            if i == 2: values["dimensions"] = 1536
            if i == 3: values["format_version"] = 0
            db.add(ChunkEmbedding(chunk_id=chunk.id, values=[1.0] + [0.0] * 767, **values))
        db.commit()
        retained = db.scalar(select(ChunkEmbedding.id).where(ChunkEmbedding.chunk_id == chunks[-1].id))
        calls = []
        def provider(texts, task, *, titles):
            calls.append((texts, titles))
            if len(calls) == 2:
                raise AppError("embedding_rate_limit", "mock capacity", 503)
            return [[1.0] + [0.0] * 767 for _ in texts]
        monkeypatch.setattr(jobs, "embed_texts", provider)
        assert jobs._embed_staged_chunks(db, snapshot.id, 2, job.id, "owner")["semanticErrorCode"] == "embedding_rate_limit"
        assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) == 3
        assert snapshot.index_report is None
        assert snapshot.status == "indexing"
        report = jobs._embed_staged_chunks(db, snapshot.id, 2, job.id, "owner")
        assert calls[-1][0] == ["source 2", "source 3"]
        assert calls[-1][1] == ["file2.py", "file3.py"]
        rows = db.scalars(select(ChunkEmbedding)).all()
        assert len(rows) == 5 and all(row.fingerprint == profile["fingerprint"] for row in rows)
        assert db.scalar(select(ChunkEmbedding.id).where(ChunkEmbedding.chunk_id == chunks[-1].id)) == retained
        assert report["semantic"] == "ready"
        assert report["semanticErrorCode"] is None


@pytest.mark.parametrize("report_compatible", [False, True])
def test_incompatible_vectors_cannot_enter_retrieval(monkeypatch, report_compatible):
    """Exclude different spaces, snapshots and versions even when their distance is smaller."""
    from app.adapters.embeddings import embedding_profile
    from app.models import ChunkEmbedding
    from app.services import retrieval
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "mock-only-key")
    profile = embedding_profile()
    calls = []
    def query_embedding(*_args):
        calls.append(True)
        return [[1.0] + [0.0] * 767]
    monkeypatch.setattr(retrieval, "embed_texts", query_embedding)
    with SessionLocal() as db:
        snapshot, chunks, _job = embedding_fixture(db, 6)
        other = Snapshot(repository_id=snapshot.repository_id, sha="b" * 40, status="ready", summary={})
        db.add(other); db.flush()
        outside = [Chunk(snapshot_id=other.id, path="outside.py", line_start=1, line_end=1, text="other"),
                   Chunk(snapshot_id=snapshot.id, index_version=1, path="stale.py", line_start=1, line_end=1, text="old")]
        db.add_all(outside); db.flush()
        for i, chunk in enumerate(chunks + outside):
            values = {"model": profile["model"], "fingerprint": profile["fingerprint"],
                      "dimensions": 768, "format_version": profile["formatVersion"]}
            if i == 0: values["fingerprint"] = None
            if i == 1: values["fingerprint"] = "f" * 64
            if i == 2: values["model"] = "wrong-model"
            if i == 3: values["dimensions"] = 1536
            if i == 4: values["format_version"] = 0
            vector = [0.0, 1.0] + [0.0] * 766 if i == 5 else [1.0] + [0.0] * 767
            db.add(ChunkEmbedding(chunk_id=chunk.id, values=vector, **values))
        if report_compatible:
            snapshot.index_report = {**snapshot.index_report, "embedding": profile}
        db.commit()
        ranked = hybrid_search_scored(db, snapshot.id, "unmatchable", 10)
        assert [chunk.id for chunk, _score in ranked] == ([chunks[5].id] if report_compatible else [])
        assert len(calls) == int(report_compatible)


def test_embedding_worker_rejects_stolen_lease_and_invalid_vectors(monkeypatch):
    from app.models import ChunkEmbedding
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "mock-only-key")
    with SessionLocal() as db:
        snapshot, _chunks, job = embedding_fixture(db, 1, published=False)
        monkeypatch.setattr(jobs, "embed_texts", lambda *_args, **_kwargs: [[float("nan")] * 768])
        report = jobs._embed_staged_chunks(db, snapshot.id, 2, job.id, "owner")
        assert report["semanticErrorCode"] == "embedding_response"
        def provider(*_args, **_kwargs):
            with SessionLocal() as other:
                other.get(Job, job.id).lease_token = "new-owner"
                other.commit()
            return [[1.0] + [0.0] * 767]
        monkeypatch.setattr(jobs, "embed_texts", provider)
        with pytest.raises(jobs.LeaseLost):
            jobs._embed_staged_chunks(db, snapshot.id, 2, job.id, "owner")
        db.rollback()
        assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0


def test_embedding_repair_preserves_ready_index_and_cached_summary(monkeypatch):
    from app.adapters.embeddings import embedding_profile
    from app.main import snapshot_status
    from app.models import ChunkEmbedding
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "mock-only-key")
    monkeypatch.setattr(jobs.settings, "worker_mode", "separate")
    def forbidden(*_args): pytest.fail("Vector repair must reuse text and summary")
    monkeypatch.setattr(jobs, "download_archive", forbidden)
    monkeypatch.setattr(jobs, "make_summary", forbidden)
    monkeypatch.setattr(jobs, "summarize_repository_nodes", forbidden)
    monkeypatch.setattr(jobs, "embed_texts", lambda texts, *_args, **_kwargs: [[1.0] + [0.0] * 767 for _ in texts])
    with SessionLocal() as db:
        snapshot, chunks, job = embedding_fixture(db, 1)
        job.status = "done"; db.commit()
        sid, cid, jid = snapshot.id, chunks[0].id, job.id
        status = snapshot_status(sid, _user=None, db=db)
        assert status["status"] == "ready" and status["search"]["mode"] == "keyword"
        assert status["search"]["reason"] == "embedding_incompatible"
        claimed = jobs.claim(db)
        assert db.get(Snapshot, sid).status == "ready"
    jobs.process(*claimed)
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, sid)
        assert snapshot.status == "ready" and snapshot.summary == {"purpose": "cached"}
        assert db.get(Chunk, cid) is not None and db.get(Job, jid).status == "done"
        vector = db.scalar(select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == cid))
        assert vector.fingerprint == embedding_profile()["fingerprint"]
        assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.snapshot_id == sid, Chunk.index_version < 0)) == 0
        assert snapshot_status(sid, _user=None, db=db)["search"]["mode"] == "hybrid"
        assert snapshot.index_report["embedding"] == embedding_profile()
        # A new model schedules one repair; repeated polling preserves the job.
        monkeypatch.setattr(jobs.settings, "gemini_embedding_model", "gemini-embedding-001")
        jobs.kick(db, sid)
        assert db.get(Job, jid).status == "queued" and snapshot.status == "ready"
        jobs.kick(db, sid)
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Job, jid).attempts == 0


def test_missing_embedding_key_exposes_keyword_fallback(monkeypatch):
    from app.adapters.embeddings import semantic_status
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "")
    with SessionLocal() as db:
        snapshot, _chunks, job = embedding_fixture(db, 1, published=False)
        report = jobs._embed_staged_chunks(db, snapshot.id, 2, job.id, "owner")
        status = semantic_status(report)
        assert status["mode"] == "keyword" and status["reason"] == "embedding_unconfigured"


def test_chat_uses_published_index_during_schema_rebuild(monkeypatch):
    from app.main import ChatRequest, _chat_work
    from app.models import SnapshotFile
    from app.services.repo_tools import execute_tool

    monkeypatch.setattr(jobs.settings, "index_schema_version", 3)
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "")
    monkeypatch.setattr(jobs, "download_archive", lambda *_: make_zip("root/replacement.py", "replacement_value = 42"))
    with SessionLocal() as db:
        snapshot, chunks, job = embedding_fixture(db, 1)
        user = User(email="reader@example.com")
        db.add(user)
        db.flush()
        token, _ = auth.issue_session(db, user.id)
        sid, cid, jid = snapshot.id, chunks[0].id, job.id
        original_report = dict(snapshot.index_report)
        db.add(SnapshotFile(snapshot_id=sid, index_version=2, path="file0.py", content="source 0",
                            line_count=1, byte_size=8))
        db.commit()

    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_, **__: {
        "content": {"parts": []}, "calls": [{"name": "final_answer", "args": {
            "answer": "The published file is available.", "citedChunkIds": [cid], "insufficientEvidence": False}}],
    })

    def summarize(*_):
        with SessionLocal() as reader:
            visible = reader.get(Snapshot, sid)
            assert (visible.status, visible.index_version, visible.summary) == ("ready", 2, {"purpose": "cached"})
            assert visible.index_report == original_report
            assert reader.scalar(select(Chunk.path).where(Chunk.snapshot_id == sid, Chunk.index_version == 3)) == "replacement.py"
            answer = _chat_work(sid, ChatRequest(question="What is in file0.py?"), token)
            assert answer["citations"][0]["path"] == "file0.py"
        return {"purpose": "replacement"}

    monkeypatch.setattr(jobs, "make_summary", summarize)
    with SessionLocal() as reader:
        previous = reader.get(Snapshot, sid)
        reader.commit()
        jobs.process(jid, "owner")
        source = execute_tool(reader, previous, "read_file", {"path": "file0.py"}, {})
        assert source["text"] == "1: source 0"
    with SessionLocal() as db:
        visible = db.get(Snapshot, sid)
        assert (visible.status, visible.index_version, visible.summary) == ("ready", 3, {"purpose": "replacement"})
        assert db.get(Job, jid).status == "done"
        assert db.get(Chunk, cid) is not None  # A reader with the former version can finish.
        assert [item.path for item in search(db, sid, "replacement_value")] == ["replacement.py"]


@pytest.mark.parametrize("failure", ["permanent", "final_attempt", "expired_lease"])
def test_failed_rebuild_preserves_published_data(monkeypatch, failure):
    monkeypatch.setattr(jobs.settings, "index_schema_version", 3)
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "")
    monkeypatch.setattr(jobs, "download_archive", lambda *_: make_zip("root/replacement.py", "replacement_value = 42"))
    code = "unsafe_archive" if failure == "permanent" else "processing_failed"

    def fail(*_):
        raise AppError(code, "Replacement failed", 500)

    monkeypatch.setattr(jobs, "make_summary", fail)
    with SessionLocal() as db:
        snapshot, chunks, job = embedding_fixture(db, 1)
        if failure != "permanent":
            job.attempts = 3
        if failure == "expired_lease":
            job.lease_until = now() - timedelta(seconds=1)
            snapshot.status = "indexing"  # A previously interrupted legacy rebuild.
        sid, cid, jid = snapshot.id, chunks[0].id, job.id
        original_report = dict(snapshot.index_report)
        db.commit()
        if failure == "expired_lease":
            assert jobs.claim(db) is None
    if failure != "expired_lease":
        jobs.process(jid, "owner")
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, sid)
        assert (snapshot.status, snapshot.index_version, snapshot.summary) == ("ready", 2, {"purpose": "cached"})
        assert snapshot.error_code is None and snapshot.index_report == original_report
        assert db.get(Chunk, cid).text == "source 0"
        assert db.get(Job, jid).status == "failed"
        assert db.get(Job, jid).error_code == code


@pytest.mark.parametrize("phase", ["directory_summary", "final_summary", "expired_final_summary", "stale_failure"])
def test_rebuild_rechecks_lease_after_summary_provider(monkeypatch, phase):
    from app.models import SnapshotNode

    monkeypatch.setattr(jobs.settings, "index_schema_version", 3)
    monkeypatch.setattr(jobs.settings, "gemini_api_key", "")
    monkeypatch.setattr(jobs, "download_archive", lambda *_: make_zip("root/replacement.py", "replacement_value = 42"))
    with SessionLocal() as db:
        snapshot, _chunks, job = embedding_fixture(db, 1)
        sid, jid = snapshot.id, job.id

    def steal(*_, **__):
        with SessionLocal() as other:
            job = other.get(Job, jid)
            job.lease_until = now() - timedelta(seconds=1)
            other.commit()
            if phase != "expired_final_summary":
                assert jobs.claim(other)[0] == jid
        if phase == "stale_failure":
            raise AppError("unsafe_archive", "Stale worker failed", 400)
        return {"purpose": "stale", "summaries": [{"path": "", "summary": "stale"}]}

    if phase == "directory_summary":
        monkeypatch.setattr(rag, "generate_json", steal)
    else:
        monkeypatch.setattr(jobs, "make_summary", steal)
    jobs.process(jid, "owner")
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, sid)
        assert snapshot.index_version == 2 and snapshot.summary == {"purpose": "cached"}
        job = db.get(Job, jid)
        assert job.status == "running" and job.error_code is None
        assert (job.lease_token == "owner") == (phase == "expired_final_summary")
        root = db.scalar(select(SnapshotNode).where(SnapshotNode.snapshot_id == sid,
                        SnapshotNode.index_version == 3, SnapshotNode.path == ""))
        assert "stale" not in root.summary


def test_embedding_repair_failure_keeps_published_vectors(monkeypatch):
    from app.models import ChunkEmbedding

    monkeypatch.setattr(jobs.settings, "gemini_api_key", "mock-only-key")
    with SessionLocal() as db:
        snapshot, chunks, job = embedding_fixture(db, 1)
        vector = ChunkEmbedding(chunk_id=chunks[0].id, model="previous-model", values=[1.0] + [0.0] * 767)
        db.add(vector)
        db.commit()
        sid, cid, eid, jid = snapshot.id, chunks[0].id, vector.id, job.id
        original_report = dict(snapshot.index_report)

    def fail(*_, **__):
        with SessionLocal() as reader:
            assert reader.get(ChunkEmbedding, eid).chunk_id == cid
            assert reader.get(Snapshot, sid).index_report == original_report
        raise AppError("embedding_rate_limit", "Replacement failed", 503)

    monkeypatch.setattr(jobs, "embed_texts", fail)
    jobs.process(jid, "owner")
    with SessionLocal() as db:
        assert db.get(Job, jid).status == "queued"
        assert db.get(Job, jid).error_code == "embedding_rate_limit"
        snapshot = db.get(Snapshot, sid)
        assert (snapshot.status, snapshot.index_report) == ("ready", original_report)
        assert db.get(ChunkEmbedding, eid).chunk_id == cid


def test_embedding_migration_invalidates_legacy_and_schedules_repairs():
    """Run upgrade/downgrade on populated legacy tables in a rolled-back schema."""
    import importlib.util
    import json
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.db import engine
    from app.adapters.embeddings import embedding_profile
    spec = importlib.util.spec_from_file_location("embedding_migration", Path(__file__).parents[1] / "migrations/versions/0005_embedding_compatibility.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("CREATE SCHEMA embedding_migration_test"))
            connection.execute(text("SET LOCAL search_path TO embedding_migration_test, public"))
            connection.execute(text("CREATE TABLE chunk_embeddings (id integer PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE snapshots (id text PRIMARY KEY, status text, index_report json, summary json)"))
            connection.execute(text("""CREATE TABLE jobs (id text PRIMARY KEY, snapshot_id text UNIQUE, status text,
                attempts integer, lease_until timestamptz, lease_token text, dispatched_at timestamptz,
                next_attempt_at timestamptz, error_code text)"""))
            connection.execute(text("INSERT INTO chunk_embeddings (id) VALUES (1)"))
            report = json.dumps({"semantic": "ready", "embedding": embedding_profile(),
                                 "embeddingRepairAttempt": embedding_profile()["fingerprint"], "keywordSearch": "postgres_fts"})
            connection.execute(text("""INSERT INTO snapshots VALUES
                ('done', 'ready', CAST(:report AS json), '{"purpose":"keep"}'),
                ('missing', 'ready', NULL, '{}'), ('running', 'ready', NULL, '{}')"""), {"report": report})
            connection.execute(text("""INSERT INTO jobs (id,snapshot_id,status,attempts,lease_token) VALUES
                ('job-done','done','done',3,NULL), ('job-running','running','running',1,'keep')"""))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT fingerprint,dimensions,format_version FROM chunk_embeddings")).one() == (None, 768, 0)
                stored = connection.scalar(text("SELECT index_report FROM snapshots WHERE id='done'"))
                assert stored["semantic"] == "keyword_only" and "embedding" not in stored
                assert stored["keywordSearch"] == "postgres_fts" and "embeddingRepairAttempt" not in stored
                assert connection.scalar(text("SELECT summary FROM snapshots WHERE id='done'")) == {"purpose": "keep"}
                assert dict(connection.execute(text("SELECT snapshot_id,status FROM jobs")).all()) == {"done": "queued", "missing": "queued", "running": "running"}
                assert connection.scalar(text("SELECT lease_token FROM jobs WHERE snapshot_id='running'")) == "keep"
                migration.downgrade()
                migration.upgrade()
                assert connection.scalar(text("SELECT count(*) FROM jobs")) == 3
        finally:
            transaction.rollback()

def test_authenticated_end_to_end_contract(monkeypatch):
    sent = []
    monkeypatch.setattr(auth, "send_code", lambda email, code: sent.append(code))
    monkeypatch.setattr(snapshots, "resolve_repo", lambda _repo: ResolvedRepository("o", "r", "main", "d" * 40, "refs/heads/main"))
    monkeypatch.setattr(jobs, "download_archive", lambda *_args: make_zip("root/app.py", "def answer(): return 42"))
    monkeypatch.setattr(jobs, "make_summary", lambda *_args: {"purpose": "A test project", "components": [], "entrypoints": ["app.py"], "technologies": ["Python"], "uncertainty": "", "suggestedQuestions": []})
    monkeypatch.setattr(jobs, "summarize_repository_nodes", lambda *_args: None)
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
