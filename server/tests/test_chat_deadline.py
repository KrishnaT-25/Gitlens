"""Chat timing regressions. Providers and wall-clock work are controlled locally."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import deadlines, main
from app.adapters import gemini, embeddings
from app.config import Settings
from app.errors import AppError
from app.services import rag


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    sleeps = []
    monkeypatch.setattr(deadlines, "time", SimpleNamespace(monotonic=lambda: value[0], sleep=sleeps.append))
    return value, sleeps


def test_nested_deadlines_do_not_reset_and_context_is_restored(clock):
    now, _sleeps = clock
    assert deadlines.remaining_seconds() is None
    with deadlines.chat_deadline(60):
        now[0] = 20
        with deadlines.chat_deadline(60):
            assert deadlines.remaining_seconds() == 40
            now[0] = 45
        assert deadlines.remaining_seconds() == 15
    assert deadlines.remaining_seconds() is None


def test_retry_cannot_sleep_past_original_deadline(clock):
    now, sleeps = clock
    with deadlines.chat_deadline(2):
        now[0] = 1.5
        with pytest.raises(AppError) as error:
            deadlines.budgeted_sleep(1)
    assert error.value.code == "chat_timeout" and not sleeps


@pytest.mark.parametrize("operation", ["agent", "json", "embedding"])
def test_provider_timeout_uses_remaining_shared_budget(monkeypatch, clock, operation):
    now, _sleeps = clock
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "mock-only-key")
    client = httpx.Client
    calls = []
    def handle(request):
        calls.append(request.extensions["timeout"])
        if operation == "embedding":
            return httpx.Response(200, json={"embeddings": [{"values": [1.0] + [0.0] * 767}]})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"answer": "ok"})}]}}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    with deadlines.chat_deadline(60):
        now[0] = 55
        if operation == "agent": gemini.generate_tool_turn("instructions", [], [])
        elif operation == "json": gemini.generate_json("instructions", "evidence", {"type": "OBJECT"})
        else: embeddings.embed_texts(["source"], "RETRIEVAL_QUERY")
    assert calls[0]["read"] <= 5 and calls[0]["connect"] <= 5


def test_stream_that_finishes_after_chat_budget_cannot_return_a_late_answer(monkeypatch, clock):
    now, _sleeps = clock
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "mock-only-key")
    client = httpx.Client
    def handle(_request):
        now[0] = 61
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    with deadlines.chat_deadline(60), pytest.raises(AppError) as error:
        gemini.generate_tool_turn("instructions", [], [])
    assert error.value.code == "chat_timeout" and error.value.status == 504


def test_agent_tools_and_turns_share_one_deadline(monkeypatch, clock):
    now, _sleeps = clock
    calls = []
    def model(*_args, **_kwargs):
        calls.append("model")
        now[0] += 20
        return {"content": {"parts": []}, "calls": [{"name": "search", "args": {"query": "test"}}]}
    def tool(*_args, **_kwargs):
        calls.append("tool")
        now[0] += 45
        return {"results": []}
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: [])
    monkeypatch.setattr(rag, "expand_context", lambda *_args: [])
    monkeypatch.setattr(rag, "_repository_map", lambda *_args: "map")
    monkeypatch.setattr(rag, "generate_tool_turn", model)
    monkeypatch.setattr(rag, "execute_tool", tool)
    db = SimpleNamespace(commit=lambda: None, get=lambda *_args: SimpleNamespace(owner="o", name="r"))
    snapshot = SimpleNamespace(id="s", repository_id="r", index_version=2, sha="a" * 40)
    with pytest.raises(AppError) as error:
        rag.answer(db, snapshot, "question", [])
    assert error.value.code == "chat_timeout"
    assert calls == ["model", "tool"]


def test_expired_budget_starts_no_retrieval(monkeypatch, clock):
    now, _sleeps = clock
    monkeypatch.setattr(rag, "hybrid_search", lambda *_args: pytest.fail("Must not begin retrieval"))
    with deadlines.chat_deadline(60):
        now[0] = 61
        with pytest.raises(AppError) as error:
            rag.answer(None, None, "question", [])
    assert error.value.code == "chat_timeout"


def test_deadline_during_initial_database_probe_closes_session(monkeypatch, clock):
    from app import db
    now, _sleeps = clock
    closed = []
    def probe(_sql):
        now[0] = 61
        deadlines.check_deadline()
    session = SimpleNamespace(execute=probe, close=lambda: closed.append(True))
    monkeypatch.setattr(db, "SessionLocal", lambda: session)
    with deadlines.chat_deadline(60), pytest.raises(AppError) as error:
        next(db.get_db())
    assert error.value.code == "chat_timeout" and closed == [True]


def test_http_deadline_returns_504_while_blocking_work_finishes_safely(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def work(*_args):
        observed.append(deadlines.remaining_seconds())
        started.set()
        try:
            release.wait(2)
            deadlines.check_deadline()
            pytest.fail("Expired work must not continue")
        except AppError as exc:
            observed.append(exc.code)
        finally:
            finished.set()
    monkeypatch.setattr(main, "_chat_work", work)
    monkeypatch.setattr(main.settings, "chat_timeout_seconds", 0.05)
    previous = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.current_user] = lambda: SimpleNamespace(id="user")
    try:
        with TestClient(main.app) as client:
            start = time.monotonic()
            result = client.post("/api/snapshots/s/chat", json={"question": "test"})
            elapsed = time.monotonic() - start
            assert started.is_set() and not finished.is_set()
            assert result.status_code == 504
            assert result.json()["error"]["code"] == "chat_timeout"
            assert elapsed < 0.5
    finally:
        release.set()
        assert finished.wait(2)
        main.app.dependency_overrides = previous
    assert 0 < observed[0] <= 0.05 and observed[1] == "chat_timeout"


def test_chat_endpoint_keeps_success_shape_and_deadline_is_request_local(monkeypatch):
    expected = {"answer": "ok", "citations": [], "insufficientEvidence": False, "sha": "a" * 40}
    def work(_sid, _body, _user):
        assert 0 < deadlines.remaining_seconds() <= 60
        return expected
    monkeypatch.setattr(main, "_chat_work", work)
    previous = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[main.current_user] = lambda: SimpleNamespace(id="user")
    try:
        with TestClient(main.app) as client:
            assert client.post("/api/snapshots/s/chat", json={"question": "test"}).json() == expected
    finally:
        main.app.dependency_overrides = previous
    assert deadlines.remaining_seconds() is None


@pytest.mark.parametrize("seconds", [0, 61, float("inf"), float("nan")])
def test_server_budget_cannot_exceed_extension_wait(seconds):
    with pytest.raises(ValueError):
        Settings(_env_file=None, chat_timeout_seconds=seconds)
