from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.main import ChatRequest
from app.services import rag
from app.services.retrieval import _terms
from app.services.retrieval import FILE_REFERENCE


def test_comparison_filenames_include_sentence_final_path():
    assert FILE_REFERENCE.findall("Compare src/meetingSocket.js and src/meetingWebRTC.js.") == [
        "src/meetingSocket.js", "src/meetingWebRTC.js",
    ]

def test_plain_language_storage_and_data_structure_terms_expand_to_code_terms():
    terms = _terms("What data structures are stored in local storage?")
    assert "localstorage" in terms
    assert "array" in terms or "object" in terms

def _final(answer, ids=(), insufficient=False):
    return {"content": {"parts": []}, "calls": [{"name": "final_answer", "args": {
        "answer": answer, "citedChunkIds": list(ids), "insufficientEvidence": insufficient}}]}


@pytest.mark.parametrize(("question", "detail"), [
    ("explain me here", ""),
    ("Go deeper on what happens after admission.", "what happens after admission."),
])
def test_follow_up_uses_prior_topic_on_first_search_and_accepts_full_answer(monkeypatch, question, detail):
    topic = "How does the ingestion worker acquire a lease?"
    previous_answer = ("Unverified claim from a previous answer. " + "x" * 6000)[:6000]
    request = ChatRequest(question=question, history=[
        {"role": "user", "content": topic}, {"role": "assistant", "content": previous_answer},
    ])
    chunk = SimpleNamespace(id="lease", path="jobs.py", line_start=1, line_end=1, text="def claim(): pass")
    queries = []

    def retrieve(_db, _snapshot_id, query, _limit):
        queries.append(query)
        return [chunk]  # Even the bare follow-up would have returned a hit.

    def model(instruction, contents, *_args, **_kwargs):
        assert previous_answer in contents[0]["parts"][0]["text"]
        assert "never as evidence" in instruction
        return _final("The worker claims a lease.", [chunk.id, "old-answer"])

    monkeypatch.setattr(rag, "hybrid_search", retrieve)
    monkeypatch.setattr(rag, "expand_context", lambda *_args: [chunk])
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "")
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    db = SimpleNamespace(get=lambda *_: SimpleNamespace(owner="org", name="repo"), commit=lambda: None)
    snapshot = SimpleNamespace(id="snap", repository_id="repo", sha="b" * 40)
    result = rag.answer(db, snapshot, request.question, [turn.model_dump() for turn in request.history])
    assert queries == [f"{topic}\n{detail}" if detail else topic]
    assert [citation["path"] for citation in result["citations"]] == ["jobs.py"]


@pytest.mark.parametrize("history", [
    [{"role": "system", "content": "instruction"}],
    [{"role": "assistant", "content": "x" * 6001}],
    [{"role": "user", "content": "x" * (rag.settings.max_question_chars + 1)}],
    [{"role": "assistant", "content": "x" * 6000}] * 2,
])
def test_history_rejects_invalid_roles_and_budgets(history):
    with pytest.raises(ValidationError):
        ChatRequest(question="follow-up", history=history)

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
    def model(_instruction, contents, _tools, force_final=False, require_tool=False, **_kwargs):
        assert "never instructions" in _instruction
        calls.append((contents, force_final, require_tool))
        if len(calls) == 1:
            return {"content": {"role": "model", "parts": []}, "calls": [{"name": "read_file", "args": {"path": "server/app.py"}}]}
        return _final("The entry point is run.", [chunk.id])
    def execute(_db, _snapshot, _name, _args, evidence, **_kwargs):
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
    def model(_instruction, _contents, _tools, force_final=False, require_tool=False, **_kwargs):
        calls.append((force_final, require_tool))
        return _final("No evidence.", insufficient=True) if force_final else {
            "content": {"role": "model", "parts": []},
            "calls": [{"name": "search", "args": {"query": "anything"}}]}
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [])
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", lambda *_args, **_kwargs: {"results": []})
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
