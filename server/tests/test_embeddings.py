"""Deterministic embedding adapter regressions; no live Gemini calls."""
import json
import math

import httpx
import pytest

from app.adapters import embeddings
from app.config import Settings
from app.errors import AppError


@pytest.fixture(autouse=True)
def embedding_settings(monkeypatch):
    for key, value in {"gemini_api_key": "mock-only-key", "gemini_embedding_model": "gemini-embedding-2",
                       "embedding_dimensions": 768, "embedding_batch_size": 32,
                       "embedding_response_bytes": 4 * 1024 * 1024,
                       "embedding_request_bytes": 1024 * 1024, "embedding_deadline_seconds": 120}.items():
        monkeypatch.setattr(embeddings.settings, key, value)


def mock_provider(monkeypatch, handler):
    """Replace HTTP transport, retaining httpx's real streaming response behavior."""
    client = httpx.Client
    monkeypatch.setattr(embeddings.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def response(count):
    return {"embeddings": [{"values": [0.012345678901234567] * 768} for _ in range(count)]}


def test_default_batch_larger_than_256kb_succeeds(monkeypatch):
    body = response(32)
    assert len(json.dumps(body).encode()) > 256_000
    seen = []
    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    mock_provider(monkeypatch, handle)
    vectors = embeddings.embed_texts(["source"] * 32, "RETRIEVAL_DOCUMENT", titles=["app.py"] * 32)
    assert len(vectors) == 32 and all(len(v) == 768 for v in vectors)
    assert math.hypot(*vectors[0]) == pytest.approx(1)
    item = seen[0]["requests"][0]
    assert item == {"model": "models/gemini-embedding-2", "outputDimensionality": 768,
                    "content": {"parts": [{"text": "title: app.py | text: source"}]}}


@pytest.mark.parametrize("model,task,expected,field", [
    ("gemini-embedding-2", "RETRIEVAL_QUERY", "task: code retrieval | query: question", None),
    ("gemini-embedding-2", "RETRIEVAL_DOCUMENT", "title: none | text: question", None),
    ("gemini-embedding-001", "RETRIEVAL_QUERY", "question", "CODE_RETRIEVAL_QUERY"),
    ("gemini-embedding-001", "RETRIEVAL_DOCUMENT", "question", "RETRIEVAL_DOCUMENT"),
])
def test_model_specific_documented_formats(monkeypatch, model, task, expected, field):
    monkeypatch.setattr(embeddings.settings, "gemini_embedding_model", model)
    def handle(request):
        assert request.url.path.endswith(f"/{model}:batchEmbedContents")
        item = json.loads(request.content)["requests"][0]
        assert item["content"]["parts"][0]["text"] == expected
        assert item.get("taskType") == field
        return httpx.Response(200, json=response(1))
    mock_provider(monkeypatch, handle)
    embeddings.embed_texts(["question"], task)


@pytest.mark.parametrize("invalid", [None, True, "0.1", {}, [], float("nan"), float("inf"), -float("inf"), 1e40, 10 ** 400])
def test_invalid_coordinates_fail_safely(invalid):
    with pytest.raises(AppError, match="invalid response") as error:
        embeddings.validate_vectors([[invalid] + [0.1] * 767], 1)
    assert error.value.code == "embedding_response"


@pytest.mark.parametrize("body", [None, [], {}, {"embeddings": None}, {"embeddings": []},
                                    {"embeddings": [None]}, {"embeddings": [{"values": [0.1]}]},
                                    {"embeddings": [{"values": [0] * 768}]}])
def test_malformed_response_is_not_retried_or_split(monkeypatch, body):
    seen = []
    mock_provider(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, json=body))[1])
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_response" and len(seen) == 1


def test_response_cap_splits_and_preserves_input_order(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "embedding_response_bytes", 20_000)
    sizes = []
    def handle(request):
        items = json.loads(request.content)["requests"]
        sizes.append(len(items))
        body = response(len(items))
        for item, vector in zip(items, body["embeddings"]):
            vector["values"][0] = int(item["content"]["parts"][0]["text"].split(" | text: ")[1]) + 1
        return httpx.Response(200, json=body)
    mock_provider(monkeypatch, handle)
    vectors = embeddings.embed_texts([str(i) for i in range(4)], "RETRIEVAL_DOCUMENT")
    assert sizes == [4, 2, 1, 1, 2, 1, 1]
    assert [v[0] for v in vectors] == sorted(v[0] for v in vectors)


def test_provider_413_splits_batch(monkeypatch):
    sizes = []
    def handle(request):
        count = len(json.loads(request.content)["requests"])
        sizes.append(count)
        return httpx.Response(413) if count > 2 else httpx.Response(200, json=response(count))
    mock_provider(monkeypatch, handle)
    assert len(embeddings.embed_texts(["source"] * 4, "RETRIEVAL_DOCUMENT")) == 4
    assert sizes == [4, 2, 2]


def test_request_cap_splits_before_sending(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "embedding_request_bytes", 5000)
    sizes = []
    def handle(request):
        assert len(request.content) <= 5000
        sizes.append(len(json.loads(request.content)["requests"]))
        return httpx.Response(200, json=response(sizes[-1]))
    mock_provider(monkeypatch, handle)
    embeddings.embed_texts(["x" * 3000] * 2, "RETRIEVAL_DOCUMENT")
    assert sizes == [1, 1]


def test_single_oversized_response_stops_reading_and_closes_stream(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "embedding_response_bytes", 1024)
    class Endless(httpx.SyncByteStream):
        closed = False
        reads = 0
        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b"x" * 8192
        def close(self): self.closed = True
    stream = Endless()
    mock_provider(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_size_limit"
    assert stream.closed and stream.reads == 1


def test_rate_limit_retries_are_bounded(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(embeddings.time, "sleep", sleeps.append)
    mock_provider(monkeypatch, lambda request: (calls.append(request), httpx.Response(429))[1])
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_rate_limit"
    assert len(calls) == 3 and sleeps == [1, 2]


def test_timeout_has_safe_error(monkeypatch):
    def handle(request): raise httpx.ReadTimeout("sensitive provider details", request=request)
    mock_provider(monkeypatch, handle)
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_timeout" and "sensitive" not in str(error.value)


def test_response_stream_obeys_total_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock[0])
    def handle(_request):
        clock[0] = 121
        return httpx.Response(200, json=response(1))
    mock_provider(monkeypatch, handle)
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_timeout"


@pytest.mark.parametrize("payload", [b'{"embeddings":', json.dumps(response(2)).encode(), b"[" * 2000 + b"]" * 2000],
                         ids=["invalid-json", "wrong-count", "excessive-nesting"])
def test_invalid_json_or_wrong_batch_count_fails(monkeypatch, payload):
    mock_provider(monkeypatch, lambda _request: httpx.Response(200, content=payload))
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_response"


def test_unrequested_compressed_response_is_closed_before_decoding(monkeypatch):
    class Unread(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            pytest.fail("Must reject compressed payload before reading it")
            yield b""
        def close(self): self.closed = True
    stream = Unread()
    mock_provider(monkeypatch, lambda _request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream))
    with pytest.raises(AppError) as error:
        embeddings.embed_texts(["source"], "RETRIEVAL_DOCUMENT")
    assert error.value.code == "embedding_response" and stream.closed


def test_fingerprint_tracks_model_dimensions_and_format(monkeypatch):
    profiles = [embeddings.embedding_profile()]
    monkeypatch.setattr(embeddings.settings, "gemini_embedding_model", "gemini-embedding-001")
    profiles.append(embeddings.embedding_profile())
    monkeypatch.setattr(embeddings.settings, "embedding_dimensions", 1536)
    profiles.append(embeddings.embedding_profile())
    monkeypatch.setattr(embeddings, "EMBEDDING_FORMAT_VERSION", 3)
    profiles.append(embeddings.embedding_profile())
    assert len({p["fingerprint"] for p in profiles}) == 4


def test_status_rejects_legacy_or_changed_space(monkeypatch):
    profile = embeddings.embedding_profile()
    assert embeddings.semantic_status(None)["semanticStatus"] == "pending"
    assert embeddings.semantic_status({"semantic": "ready"})["mode"] == "keyword"
    report = {"semantic": "ready", "embedding": profile}
    assert embeddings.semantic_status(report)["mode"] == "hybrid"
    monkeypatch.setattr(embeddings.settings, "gemini_embedding_model", "gemini-embedding-001")
    assert embeddings.semantic_status(report)["semanticStatus"] == "incompatible"


@pytest.mark.parametrize("options", [{"embedding_dimensions": 1536}, {"gemini_embedding_model": "unknown"},
                                     {"embedding_response_bytes": 0}, {"embedding_request_bytes": 999},
                                     {"embedding_deadline_seconds": float("nan")}])
def test_invalid_embedding_configuration_rejected(options):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **options)
