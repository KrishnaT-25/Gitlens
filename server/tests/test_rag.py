from types import SimpleNamespace
from app.services import rag

def test_no_evidence_never_calls_model(monkeypatch):
    monkeypatch.setattr(rag, "search", lambda *_args: [])
    monkeypatch.setattr(rag, "generate_json", lambda *_args: (_ for _ in ()).throw(AssertionError("model called")))
    result = rag.answer(None, SimpleNamespace(id="s", sha="a" * 40), "unknown", [])
    assert result["insufficientEvidence"] is True

def test_citations_restricted_to_retrieved_chunks(monkeypatch):
    chunk = SimpleNamespace(id="known", path="src/a b.py", line_start=3, line_end=8, text="def answer(): pass")
    monkeypatch.setattr(rag, "search", lambda *_args: [chunk])
    monkeypatch.setattr(rag, "expand_context", lambda _db, _snapshot_id, chunks, _limit: chunks)
    monkeypatch.setattr(rag, "generate_json", lambda *_args: {"answer": "See the function.", "citedChunkIds": ["known", "other-snapshot"], "insufficientEvidence": False})
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="team", name="repo")
        def commit(self): pass
    result = rag.answer(Db(), SimpleNamespace(id="s", repository_id="r", sha="a" * 40), "where?", [])
    assert len(result["citations"]) == 1
    assert result["citations"][0]["url"].endswith("/src/a%20b.py#L3-L8")

def test_invalid_citation_becomes_evidence_gap(monkeypatch):
    chunk = SimpleNamespace(id="known", path="a.py", line_start=1, line_end=1, text="hello")
    monkeypatch.setattr(rag, "search", lambda *_args: [chunk])
    monkeypatch.setattr(rag, "expand_context", lambda _db, _snapshot_id, chunks, _limit: chunks)
    monkeypatch.setattr(rag, "generate_json", lambda *_args: {"answer": "Invented", "citedChunkIds": ["unknown"], "insufficientEvidence": False})
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="x", name="y")
        def commit(self): pass
    result = rag.answer(Db(), SimpleNamespace(id="s", repository_id="r", sha="a" * 40), "question", [])
    assert result["insufficientEvidence"] is True
