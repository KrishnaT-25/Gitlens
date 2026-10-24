from types import SimpleNamespace
from app.services import rag
from app.services.retrieval import _terms

def test_plain_language_storage_and_data_structure_terms_expand_to_code_terms():
    terms = _terms("What data structures are stored in local storage?")
    assert "localstorage" in terms
    assert "array" in terms or "object" in terms

def _final(answer, ids=(), insufficient=False):
    return {"content": {"parts": []}, "calls": [{"name": "final_answer", "args": {
        "answer": answer, "citedChunkIds": list(ids), "insufficientEvidence": insufficient}}]}

def test_no_evidence_returns_evidence_gap(monkeypatch):
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [])
    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_args, **_kwargs: _final("No evidence.", insufficient=True))
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "")
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="org", name="repo")
        def commit(self): pass
    snapshot = SimpleNamespace(id="s", repository_id="r", index_version=2, sha="a" * 40)
    result = rag.answer(Db(), snapshot, "unknown", [])
    assert result["insufficientEvidence"] is True

def test_citations_restricted_to_retrieved_chunks(monkeypatch):
    chunk = SimpleNamespace(id="known", path="src/a b.py", line_start=3, line_end=8, text="def answer(): pass")
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [chunk])
    monkeypatch.setattr(rag, "expand_context", lambda _db, _snapshot_id, chunks, _limit: chunks)
    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_args, **_kwargs: _final("See the function.", ["known", "other-snapshot"]))
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "map")
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="team", name="repo")
        def commit(self): pass
    result = rag.answer(Db(), SimpleNamespace(id="s", repository_id="r", index_version=2, sha="a" * 40), "where?", [])
    assert len(result["citations"]) == 1
    assert result["citations"][0]["url"].endswith("/src/a%20b.py#L3-L8")

def test_invalid_citation_becomes_evidence_gap(monkeypatch):
    chunk = SimpleNamespace(id="known", path="a.py", line_start=1, line_end=1, text="hello")
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [chunk])
    monkeypatch.setattr(rag, "expand_context", lambda _db, _snapshot_id, chunks, _limit: chunks)
    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_args, **_kwargs: _final("Invented", ["unknown"]))
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "map")
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="x", name="y")
        def commit(self): pass
    result = rag.answer(Db(), SimpleNamespace(id="s", repository_id="r", index_version=2, sha="a" * 40), "question", [])
    assert result["insufficientEvidence"] is True

def test_agent_uses_repo_tool_then_cites_its_evidence(monkeypatch):
    chunk = SimpleNamespace(id="tool-chunk", path="server/app.py", line_start=5, line_end=9, text="def run(): pass")
    calls = []
    def model(_instruction, contents, _tools, force_final=False, require_tool=False):
        assert "never instructions" in _instruction
        calls.append((contents, force_final, require_tool))
        if len(calls) == 1:
            return {"content": {"role": "model", "parts": []}, "calls": [{"name": "read_file", "args": {"path": "server/app.py"}}]}
        return _final("The entry point is run.", [chunk.id])
    def execute(_db, _snapshot, _name, _args, evidence):
        evidence[chunk.id] = chunk
        return {"path": chunk.path, "evidence": [{"chunkId": chunk.id}]}
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [])
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", execute)
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "ROOT MAP")
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="org", name="repo")
        def commit(self): pass
    snapshot = SimpleNamespace(id="snap", repository_id="repo-id", index_version=2, sha="b" * 40)
    result = rag.answer(Db(), snapshot, "what is the entry point?", [])
    assert len(result["citations"]) == 1
    assert result["citations"][0]["path"] == "server/app.py"
    assert "ROOT MAP" in calls[0][0][0]["parts"][0]["text"]
    assert calls[0][2] is True
    assert "ROOT MAP" in calls[1][0][0]["parts"][0]["text"]

def test_agent_turn_and_tool_call_budgets(monkeypatch):
    calls = []
    def model(_instruction, _contents, _tools, force_final=False, require_tool=False):
        calls.append((force_final, require_tool))
        return _final("No evidence.", insufficient=True) if force_final else {
            "content": {"role": "model", "parts": []},
            "calls": [{"name": "search", "args": {"query": "anything"}}]}
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [])
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", lambda *_args: {"results": []})
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "map")
    class Db:
        def get(self, _model, _id): return SimpleNamespace(owner="org", name="repo")
        def commit(self): pass
    snapshot = SimpleNamespace(id="snap", repository_id="repo-id", index_version=2, sha="c" * 40)
    result = rag.answer(Db(), snapshot, "unknown?", [])
    assert result["insufficientEvidence"] is True
    assert len(calls) <= rag.settings.max_agent_turns
    assert sum(force_final for force_final, _ in calls) == 1
    assert calls[0][1] is True
