"""Fixed PostgreSQL corpus for measuring scoped retrieval and evidence coverage."""

import os

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.models import Chunk, ChunkEmbedding, Repository, Snapshot, SnapshotFile, SymbolReference
from app.services import retrieval
from app.services.repo_tools import execute_tool

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database not configured")


@pytest.fixture
def corpus():
    with SessionLocal() as db:
        repo = Repository(owner="eval", name="retrieval", canonical_url="https://github.com/eval/retrieval", default_branch="main")
        db.add(repo)
        db.flush()
        snapshot = Snapshot(repository_id=repo.id, sha="a" * 40, status="ready", summary={}, index_version=2)
        other = Snapshot(repository_id=repo.id, sha="b" * 40, status="ready", summary={}, index_version=2)
        db.add_all([snapshot, other])
        db.flush()

        def add(path, source, *, line=1, symbol=None, version=2, owner=snapshot):
            chunk = Chunk(snapshot_id=owner.id, index_version=version, path=path, text=source,
                          line_start=line, line_end=line, symbol_path=symbol)
            db.add(chunk)
            db.flush()
            return chunk

        yield db, snapshot, other, add


@pytest.mark.parametrize("scopes", [{"path": "z_scope%_"}, {"symbol": "read_value"}, {"path": "z_scope%_", "symbol": "read_value"}])
def test_keyword_scope_survives_candidate_starvation(corpus, scopes):
    """More than the keyword candidate cap of matching out-of-scope chunks."""
    db, snapshot, other, add = corpus
    target = add("z_scope%_/target.py", "lookupneedle", symbol="Loader.READ_VALUE")
    for i in range(82):
        add(f"a/{i:03}.py", "lookupneedle lookupneedle lookupneedle", symbol="readXvalue")
    add("z_scope%_else/target.py", "lookupneedle", symbol="readXvalue")
    add("z_scopeXX/target.py", "lookupneedle", symbol="readXvalue")
    add(target.path, "lookupneedle", symbol=target.symbol_path, version=1)
    add(target.path, "lookupneedle", symbol=target.symbol_path, owner=other)
    evidence = {}
    result = execute_tool(db, snapshot, "search", {"query": "lookupneedle", **scopes}, evidence)
    assert [item["chunkId"] for item in result["results"]] == [target.id]
    assert list(evidence) == [target.id]


def test_read_file_exposes_only_requested_lines_as_evidence(corpus):
    db, snapshot, _, _ = corpus
    source = "first\nsecond\nthird"
    db.add(SnapshotFile(snapshot_id=snapshot.id, index_version=2, path="src/a.py",
                        content=source, line_count=3, byte_size=len(source)))
    chunk = Chunk(snapshot_id=snapshot.id, index_version=2, path="src/a.py",
                  line_start=1, line_end=3, text=source)
    db.add(chunk)
    db.flush()
    evidence = {}
    output = execute_tool(db, snapshot, "read_file", {"path": "src/a.py", "start_line": 2, "end_line": 2}, evidence)
    excerpt_id = f"{chunk.id}:L2-L2"
    assert output["evidence"] == [{"chunkId": excerpt_id, "path": "src/a.py", "lineStart": 2,
                                   "lineEnd": 2, "text": "second"}]
    assert list(evidence) == [excerpt_id]


def test_named_file_fast_path_honors_scope(corpus):
    db, snapshot, _, add = corpus
    add("a/main.py", "outside")
    target = add("z_scope/main.py", "target")
    result = execute_tool(db, snapshot, "search", {"query": "main.py:1", "path": target.path}, {})
    assert [item["chunkId"] for item in result["results"]] == [target.id]


def test_named_file_comparison_reserves_evidence_for_both_files(corpus):
    db, snapshot, _, add = corpus
    first = add("src/meetingSocket.js", "join")
    add(first.path, "admit", line=2)
    second = add("src/meetingWebRTC.js", "offer")
    hits = retrieval.hybrid_search(db, snapshot.id,
        "Compare src/meetingSocket.js and src/meetingWebRTC.js.", 2)
    assert [chunk.id for chunk in hits] == [first.id, second.id]


def test_exact_symbol_definition_beats_broad_matches_and_stays_in_scope(corpus):
    db, snapshot, other, add = corpus
    target = add("src/peers.js", "class PeerSession {}")
    outside = add("outside/peers.js", "class PeerSession {}", owner=other)
    for source in (target, outside):
        db.add(SymbolReference(snapshot_id=source.snapshot_id, index_version=2, chunk_id=source.id,
                               path=source.path, line=1, name="PeerSession", is_definition=True))
    for i in range(30):
        add(f"docs/{i}.md", "connection create setup " * 8)
    db.flush()
    hits = retrieval.hybrid_search(db, snapshot.id, "How does PeerSession create a connection?", 1)
    assert [hit.id for hit in hits] == [target.id]


def test_broad_flow_retrieves_evidence_for_each_clause(corpus):
    db, snapshot, _, add = corpus
    target = [
        add("join.js", "browser"),
        add("socket.js", "signaling"),
        add("peer.js", "webrtc"),
    ]
    for i in range(10):
        add(f"noise/{i}.md", "browser browser browser browser")
    hits = retrieval.hybrid_search(db, snapshot.id,
        "Walk me through what happens in the browser, signaling, and WebRTC.", 3)
    assert {chunk.path for chunk in hits} == {chunk.path for chunk in target}


def test_native_symbol_occurrences_cover_more_than_one_file(corpus):
    db, snapshot, _, add = corpus
    for line in range(1, 7):
        add("files.js", "new RTCPeerConnection({});", line=line)
    add("meetings.js", "new RTCPeerConnection({});")
    hits = retrieval.hybrid_search(db, snapshot.id, "Which code creates an RTCPeerConnection?", 2)
    assert {hit.path for hit in hits} == {"files.js", "meetings.js"}


def test_bm25_scope_or_fts_fallback_keeps_caller_transaction(corpus, monkeypatch):
    db, snapshot, _, add = corpus
    snapshot.index_report = {"keywordSearch": "bm25"}
    for i in range(50):
        add(f"outside/{i}.py", "lookupneedle")
    target = add("scope/target.py", "lookupneedle")
    if db.scalar(text("SELECT to_regclass('ix_chunks_bm25') IS NOT NULL")):
        monkeypatch.setattr(retrieval, "_fts", lambda *_: pytest.fail("Expected the BM25 index to serve this query"))
    result = execute_tool(db, snapshot, "search", {"query": "lookupneedle", "path": "scope"}, {})
    assert [item["chunkId"] for item in result["results"]] == [target.id]
    assert db.get(Chunk, target.id) is target


def test_vector_scope_survives_candidate_starvation(corpus, monkeypatch):
    """Use real pgvector ordering with fixed vectors and no external provider."""
    db, snapshot, other, add = corpus
    monkeypatch.setattr(retrieval.settings, "gemini_api_key", "test-only")
    profile = retrieval.embedding_profile()
    snapshot.index_report = {"semantic": "ready", "embedding": profile}
    near = [1.0] + [0.0] * 767
    far = [0.0, 1.0] + [0.0] * 766
    monkeypatch.setattr(retrieval, "embed_texts", lambda *_: [near])

    def embed(chunk, values=near, fingerprint=profile["fingerprint"]):
        db.add(ChunkEmbedding(chunk_id=chunk.id, model=profile["model"], fingerprint=fingerprint,
                              dimensions=768, format_version=profile["formatVersion"], values=values))

    path = "z_scope/target.py"
    target = add(path, "content", line=1, symbol="Reader")
    embed(target, far)
    for i in range(50):
        embed(add(f"a/{i}.py", "content", symbol="Reader"))
        embed(add(path, "content", line=i + 2, symbol="Other"))
    embed(add(path, "content", symbol="Reader", version=1))
    embed(add(path, "content", symbol="Reader", owner=other))
    embed(add(path, "content", symbol="Reader", line=100), fingerprint="wrong")
    db.flush()
    result = execute_tool(db, snapshot, "search", {"query": "vectoronly", "path": "z_scope", "symbol": "reader"}, {})
    assert [item["chunkId"] for item in result["results"]] == [target.id]


def test_retrieves_two_strong_chunks_ahead_of_diverse_synonym_matches(corpus):
    db, snapshot, _, add = corpus
    relevant = [
        add("src/auth.py", "token validation returns structured errors", line=3),
        add("src/auth.py", "token validation reports errors for invalid claims", line=24),
    ]
    for i in range(12):
        add(f"docs/{i}.md", "failures exceptions bugs failure exceptions bug")
    hits = retrieval.hybrid_search(db, snapshot.id, "How does token validation handle errors?", 2)
    assert {chunk.id for chunk in hits} == {chunk.id for chunk in relevant}


@pytest.mark.parametrize("question", ["Explain the RAG pipeline end to end", "Explain the whole pipeline starting in worker.py"])
def test_pipeline_question_retrieves_both_stages(corpus, question):
    db, snapshot, _, add = corpus
    indexing = add("worker.py", "ingest the archive then chunk source files and build embeddings")
    answering = add("assistant.py", "answer chat by retrieval of context and building verified citations")
    for i in range(12):
        add(f"docs/{i}.md", "RAG pipeline end to end overview")
    add("tests/test_pipeline.py", "ingest archive chunk index embeddings answer chat retrieval context citation job agent " * 5)
    add("scripts/smoke_pipeline.py", "ingest archive chunk index embeddings answer chat retrieval context citation job agent " * 5)
    hits = retrieval.hybrid_search(db, snapshot.id, question, 2)
    assert {chunk.id for chunk in hits} == {indexing.id, answering.id}
