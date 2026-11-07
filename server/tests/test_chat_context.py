"""Deterministic context/agent routing checks; no live model judgments."""
from types import SimpleNamespace

import httpx
import pytest

from app.adapters import gemini
from app.config import Settings
from app.services import rag
from app.services.chat_context import MAP_NOTICE, compact_map, prepare_context, question_kind


def chunk(cid, start, end, path="src/app.py", text=None):
    return SimpleNamespace(id=cid, path=path, line_start=start, line_end=end,
        text=text if text is not None else "\n".join(f"unique_source_line_{i}_" + "x" * 70 for i in range(start, end + 1)))


def test_overlap_removal_preserves_all_source_lines_and_real_citation_ranges():
    chunks = [chunk("first", 1, 12), chunk("second", 7, 18), chunk("inside", 2, 5)]
    before = sum(len(f"CHUNK {c.id} FILE {c.path} LINES {c.line_start}-{c.line_end}\n{c.text}") + 2 for c in chunks)
    result = prepare_context(chunks, 56000)
    assert result.duplicate_chunks == 1 and result.removed_overlap_lines == 10
    assert len(result.text) < before
    for n in range(1, 19):
        assert result.text.count(f"unique_source_line_{n}_") == 1
    assert [c.id for c in result.chunks] == ["first", "second"]
    assert "CHUNK second FILE src/app.py LINES 7-18" in result.text
    assert "LINES 13-18" in result.text
    assert chunks[1].line_start == 7 and len(chunks[1].text.splitlines()) == 12


def test_out_of_order_overlaps_keep_disjoint_new_ranges():
    result = prepare_context([chunk("anchor", 5, 20), chunk("parent", 1, 30)], 56000)
    assert "LINES 1-4" in result.text and "LINES 21-30" in result.text
    for n in range(1, 31): assert result.text.count(f"unique_source_line_{n}_") == 1


def test_identical_content_in_distinct_files_is_never_deduplicated():
    result = prepare_context([chunk("a", 1, 2, path="a.py"), chunk("b", 1, 2, path="b.py")], 56000)
    assert len(result.chunks) == 2 and result.duplicate_chunks == 0


def test_conflicts_and_inconsistent_metadata_remain_intact():
    result = prepare_context([chunk("a", 1, 2), chunk("b", 1, 2, text="different\nsource"),
                              chunk("c", 1, 99, text="opaque partial node")], 56000)
    assert len(result.chunks) == 3
    assert "different\nsource" in result.text and "opaque partial node" in result.text
    assert result.removed_overlap_lines == 0


def test_context_budget_never_truncates_source_or_authorizes_omitted_ids():
    first, second = chunk("a", 1, 2), chunk("b", 20, 30)
    only_first = prepare_context([first], 56000)
    result = prepare_context([first, second, first], len(only_first.text))
    assert result.text == only_first.text and [c.id for c in result.chunks] == ["a"]


@pytest.mark.parametrize("question,kind", [
    ("What is the default embedding batch size?", "lookup"),
    ("Which scripts are in package.json?", "lookup"),
    ("Explain the authentication flow for a beginner in detail", "complex"),
    ("Compare src/a.py and src/b.py", "complex"),
    ("Why are retries needed?", "complex"),
    ("My login fails", "standard"),
])
def test_conservative_question_routes(question, kind):
    assert question_kind(question) == kind
    assert question_kind("What does that do?", has_history=True) == "standard"


def test_map_compaction_keeps_navigation_and_relevant_paths_without_cutting_entries():
    nodes = [SimpleNamespace(path="", parent_path="", node_type="directory", summary="Repository root"),
             SimpleNamespace(path="src", parent_path="", node_type="directory", summary="Sources"),
             SimpleNamespace(path="src/app.py", parent_path="src", node_type="file", summary="Entry point"),
             SimpleNamespace(path="README.md", parent_path="", node_type="file", summary="Readme")]
    nodes += [SimpleNamespace(path="assets/" + "x" * 100 + str(i), parent_path="assets", node_type="file", summary="irrelevant" * 40) for i in range(50)]
    result = compact_map(nodes, ["src/app.py"], 1000)
    assert "ROOT Repository root" in result and "DIR src" in result
    assert "FILE src/app.py — Entry point" in result and "FILE README.md" in result
    assert result.endswith(MAP_NOTICE) and len(result) <= 1000
    assert "irrelevant" not in result


def setup_agent(monkeypatch, chunks):
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: chunks)
    monkeypatch.setattr(rag, "expand_context", lambda *_args: chunks)
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "ROOT MAP")
    db = SimpleNamespace(commit=lambda: None, get=lambda *_args: SimpleNamespace(owner="o", name="r"))
    snapshot = SimpleNamespace(id="s", repository_id="r", index_version=2, sha="a" * 40)
    return db, snapshot


def final(cid):
    return {"content": {"parts": []}, "calls": [{"name": "final_answer", "args": {
        "answer": "The value is 32.", "citedChunkIds": [cid], "insufficientEvidence": False}}]}


def test_simple_lookup_can_finish_in_one_turn_with_validated_citation(monkeypatch):
    source = chunk("known", 1, 1, text="embedding_batch_size = 32")
    db, snapshot = setup_agent(monkeypatch, [source])
    calls = []
    def model(*_args, **kwargs):
        calls.append(kwargs)
        return final("known")
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", lambda *_args: pytest.fail("No mandatory inspection for supported lookup"))
    result = rag.answer(db, snapshot, "What is the default batch size?", [])
    assert len(calls) == 1 and calls[0]["require_tool"] is False
    assert calls[0]["thinking_level"] == "minimal"
    assert result["citations"][0]["lineStart"] == 1


def test_simple_lookup_can_inspect_when_initial_evidence_is_inadequate(monkeypatch):
    db, snapshot = setup_agent(monkeypatch, [chunk("initial", 1, 1, text="General project description")])
    calls = []
    def model(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"content": {"parts": []}, "calls": [{"name": "read_file", "args": {"path": "config.py"}}]}
        return final("found")
    def tool(_db, _snapshot, _name, _args, evidence):
        evidence["found"] = chunk("found", 5, 5, path="config.py", text="batch_size = 32")
        return {"text": "batch_size = 32", "chunkId": "found"}
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", tool)
    result = rag.answer(db, snapshot, "What is the batch size?", [])
    assert len(calls) == 2 and result["citations"][0]["path"] == "config.py"


def test_complex_route_keeps_initial_inspection_and_medium_thinking(monkeypatch):
    db, snapshot = setup_agent(monkeypatch, [chunk("known", 1, 1)])
    seen = []
    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_args, **kwargs: (seen.append(kwargs), final("known"))[1])
    rag.answer(db, snapshot, "Explain the flow across the API and worker", [])
    assert seen[0]["require_tool"] is True and seen[0]["thinking_level"] == "medium"


def test_direct_answer_cannot_cite_an_unseen_chunk(monkeypatch):
    db, snapshot = setup_agent(monkeypatch, [chunk("known", 1, 1)])
    monkeypatch.setattr(rag, "generate_tool_turn", lambda *_args, **_kwargs: final("other-snapshot"))
    result = rag.answer(db, snapshot, "What is the default value?", [])
    assert result["insufficientEvidence"] is True and result["citations"] == []


@pytest.mark.parametrize("model,requested,effective", [
    ("gemini-3.5-flash-lite", "minimal", "minimal"),
    ("gemini-3.8-flash", "minimal", "low"),
    ("gemini-3.5-flash-lite", "medium", "medium"),
])
def test_provider_thinking_level_respects_model_capabilities(monkeypatch, model, requested, effective):
    import json
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "mock-only-key")
    monkeypatch.setattr(gemini.settings, "gemini_model", model)
    real_client = httpx.Client
    def handle(request):
        body = json.loads(request.content)
        assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == effective
        assert body["toolConfig"]["functionCallingConfig"] == {"mode": "ANY"}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    gemini.generate_tool_turn("instruction", [], [], thinking_level=requested)


@pytest.mark.parametrize("options", [{"chat_map_chars": 0}, {"chat_map_chars": 13000},
                                     {"chat_simple_thinking": "invalid"}, {"chat_complex_thinking": "invalid"}])
def test_invalid_context_configuration_rejected(options):
    with pytest.raises(ValueError): Settings(_env_file=None, **options)
