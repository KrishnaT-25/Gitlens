"""Mocked transport tests; live model comparisons live under eval/ instead."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app import deadlines
from app.adapters import gemini
from app.config import Settings
from app.errors import AppError


@pytest.fixture(autouse=True)
def retry_settings(monkeypatch):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "mock-only-key")
    monkeypatch.setattr(gemini.settings, "gemini_max_retries", 2)
    monkeypatch.setattr(gemini.settings, "gemini_retry_base_seconds", 1)
    monkeypatch.setattr(gemini.settings, "gemini_retry_max_seconds", 8)


@pytest.fixture
def clock(monkeypatch):
    now, sleeps = [0.0], []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    fake = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    monkeypatch.setattr(gemini, "time", fake)
    monkeypatch.setattr(deadlines, "time", fake)
    monkeypatch.setattr(gemini.random, "uniform", lambda _low, high: high)
    return now, sleeps


def provider(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def success():
    return {"candidates": [{"content": {"parts": [{"text": '{"answer":"ok"}'},
            {"functionCall": {"name": "final_answer", "args": {"answer": "ok"}}}]}}]}


def call(mode):
    if mode == "json":
        return gemini.generate_json("instructions", "source", {"type": "OBJECT"})
    return gemini.generate_tool_turn("instructions", [], [], force_final=True)


@pytest.mark.parametrize("mode", ["json", "agent"])
@pytest.mark.parametrize("status", [429, 503])
def test_transient_failure_retries_after_closing_response(monkeypatch, clock, mode, status):
    _now, sleeps = clock
    streams, calls = [], []
    class Stream(httpx.SyncByteStream):
        closed = False
        def __iter__(self): yield b'{"error":{"message":"temporary"}}'
        def close(self): self.closed = True
    def handle(request):
        assert all(s.closed for s in streams)
        calls.append(request)
        if len(calls) < 3:
            stream = Stream(); streams.append(stream)
            return httpx.Response(status, stream=stream)
        return httpx.Response(200, json=success())
    provider(monkeypatch, handle)
    assert call(mode)
    assert len(calls) == 3 and sleeps == [1, 2]
    assert all(s.closed for s in streams)
    assert len({str(request.url) for request in calls}) == 1  # No model fallback.


@pytest.mark.parametrize("status,code", [(429, "gemini_rate_limit"), (503, "gemini_unavailable")])
def test_retry_exhaustion_is_bounded(monkeypatch, clock, status, code):
    _now, sleeps = clock
    calls = []
    provider(monkeypatch, lambda request: (calls.append(request), httpx.Response(status, json={}))[1])
    with pytest.raises(AppError) as error:
        call("agent")
    assert error.value.code == code and len(calls) == 3 and sleeps == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_other_provider_errors_are_not_retried(monkeypatch, clock, status):
    _now, sleeps = clock
    calls = []
    provider(monkeypatch, lambda request: (calls.append(request), httpx.Response(status, json={}))[1])
    with pytest.raises(AppError): call("agent")
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("header,details,expected", [("3", [], 3),
    (None, [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2.5s"}], 2.5),
    ("2", [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "4s"}], 4)])
def test_provider_retry_guidance_is_honored(monkeypatch, clock, header, details, expected):
    _now, sleeps = clock
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=success()) if len(calls) > 1 else httpx.Response(429,
            headers={"retry-after": header} if header else {}, json={"error": {"details": details}})
    provider(monkeypatch, handle)
    call("agent")
    assert sleeps == [expected] and len(calls) == 2


def test_http_date_and_invalid_retry_delays():
    assert gemini._retry_after(httpx.Headers({"retry-after": "Thu, 01 Jan 2099 00:00:00 GMT"}), b"{}") > 30
    assert gemini._retry_after(httpx.Headers({"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"}), b"{}") == 0
    for value in ("NaN", "Infinity", "-1", "invalid"):
        assert gemini._retry_after(httpx.Headers({"retry-after": value}), b"not json") is None


def test_long_provider_delay_is_not_shortened(monkeypatch, clock):
    _now, sleeps = clock
    calls = []
    provider(monkeypatch, lambda request: (calls.append(request), httpx.Response(429, headers={"retry-after": "120"}))[1])
    with pytest.raises(AppError) as error: call("agent")
    assert error.value.code == "gemini_rate_limit" and len(calls) == 1 and not sleeps


def test_retry_respects_remaining_chat_time_and_does_not_restart_clock(monkeypatch, clock):
    now, sleeps = clock
    calls = []
    def handle(request):
        calls.append(request.extensions["timeout"]["read"])
        if len(calls) == 1:
            now[0] += 0.5
            return httpx.Response(503, json={})
        return httpx.Response(200, json=success())
    provider(monkeypatch, handle)
    with deadlines.chat_deadline(2): call("agent")
    assert sleeps == [1] and calls == [2, 0.5]


def test_retry_is_skipped_when_provider_delay_cannot_fit_budget(monkeypatch, clock):
    now, sleeps = clock
    calls = []
    def handle(request):
        calls.append(request)
        now[0] = 8
        return httpx.Response(503, headers={"retry-after": "3"})
    provider(monkeypatch, handle)
    with deadlines.chat_deadline(10), pytest.raises(AppError) as error: call("agent")
    assert error.value.code == "gemini_unavailable" and len(calls) == 1 and not sleeps


def test_malformed_success_response_is_not_retried(monkeypatch, clock):
    calls = []
    provider(monkeypatch, lambda request: (calls.append(request), httpx.Response(200, content=b"not json"))[1])
    with pytest.raises(AppError) as error: call("agent")
    assert error.value.code == "gemini_response" and len(calls) == 1


def test_error_response_is_bounded_and_closed(monkeypatch, clock):
    class Stream(httpx.SyncByteStream):
        closed = False
        reads = 0
        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b"x" * 8192
        def close(self): self.closed = True
    stream = Stream()
    monkeypatch.setattr(gemini.settings, "gemini_max_retries", 0)
    provider(monkeypatch, lambda _request: httpx.Response(503, stream=stream))
    with pytest.raises(AppError): call("agent")
    assert stream.closed and stream.reads == 3


def test_jitter_remains_in_backoff_bounds(monkeypatch, clock):
    _now, sleeps = clock
    monkeypatch.setattr(gemini.random, "uniform", lambda _low, _high: 0)
    provider(monkeypatch, lambda _request: httpx.Response(503, json={}))
    with pytest.raises(AppError): call("agent")
    assert sleeps == [0.5, 1]


@pytest.mark.parametrize("options", [{"gemini_max_retries": 4}, {"gemini_retry_base_seconds": 0},
                                    {"gemini_retry_max_seconds": 31}, {"gemini_retry_base_seconds": float("nan")}])
def test_invalid_retry_configuration_rejected(options):
    with pytest.raises(ValueError): Settings(_env_file=None, **options)
