"""Bounded Gemini embedding requests and the identity of their embedding space."""
import hashlib
import json
import math
import time

import httpx

from app.config import settings
from app.errors import AppError

# Bump whenever document/query formatting, task instructions, or truncation changes.
EMBEDDING_FORMAT_VERSION = 2
MAX_INPUT_CHARS = 8000
FLOAT32_MAX = 3.4028234663852886e38


def embedding_profile() -> dict:
    """Return a stable fingerprint shared by document and query embeddings."""
    profile = {"model": settings.gemini_embedding_model, "dimensions": settings.embedding_dimensions,
               "formatVersion": EMBEDDING_FORMAT_VERSION}
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**profile, "fingerprint": fingerprint}


def semantic_status(report: dict | None) -> dict:
    """Describe effective search mode without treating legacy vectors as compatible."""
    report = report or {}
    compatible = report.get("embedding") == embedding_profile()
    state = report.get("semantic", "pending")
    reason = report.get("semanticErrorCode")
    if not compatible and report:
        state, reason = "incompatible", "embedding_incompatible"
    elif compatible and not settings.gemini_api_key:
        state, reason = "unavailable", "embedding_unconfigured"
    return {"mode": "hybrid" if state == "ready" else "keyword", "semanticStatus": state,
            "keywordSearch": report.get("keywordSearch", "postgres_fts"),
            "embeddingFingerprint": (report.get("embedding") or {}).get("fingerprint"), "reason": reason}


def validate_vectors(vectors: object, count: int) -> list[list[float]]:
    """Reject wrong shapes, booleans, non-finite/float32-overflow values and zero vectors."""
    try:
        if not isinstance(vectors, list) or len(vectors) != count:
            raise ValueError("Embedding count mismatch")
        validated = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != settings.embedding_dimensions:
                raise ValueError("Embedding dimensions mismatch")
            if any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > FLOAT32_MAX for value in vector):
                raise ValueError("Invalid embedding coordinate")
            norm = math.hypot(*vector)
            if norm == 0:
                raise ValueError("Zero embedding cannot be searched with cosine distance")
            validated.append([float(value) / norm for value in vector])
        return validated
    except (ValueError, TypeError, OverflowError) as exc:
        raise AppError("embedding_response", "Embedding service returned an invalid response.", 502) from exc


def _request_body(texts: list[str], task_type: str, titles: list[str]) -> dict:
    requests = []
    for content, title in zip(texts, titles):
        content, title = content[:MAX_INPUT_CHARS], title[:1000]
        request = {"model": f"models/{settings.gemini_embedding_model}",
                   "outputDimensionality": settings.embedding_dimensions}
        if settings.gemini_embedding_model == "gemini-embedding-2":
            content = (f"task: code retrieval | query: {content}" if task_type == "RETRIEVAL_QUERY"
                       else f"title: {title or 'none'} | text: {content}")
        else:
            request["taskType"] = "CODE_RETRIEVAL_QUERY" if task_type == "RETRIEVAL_QUERY" else task_type
            if task_type == "RETRIEVAL_DOCUMENT" and title:
                request["title"] = title
        request["content"] = {"parts": [{"text": content}]}
        requests.append(request)
    return {"requests": requests}


class _SplitBatch(Exception):
    """Provider or local size boundary requires smaller requests."""


def _send(client: httpx.Client, body: dict, deadline: float) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_embedding_model}:batchEmbedContents"
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("embedding_timeout", "Embedding request timed out.", 503)
        with client.stream("POST", url, headers={"x-goog-api-key": settings.gemini_api_key, "Accept-Encoding": "identity"}, json=body,
                           timeout=httpx.Timeout(min(40, remaining), connect=min(5, remaining))) as response:
            if response.status_code == 413:
                raise _SplitBatch()
            if response.status_code == 429:
                if attempt == 2:
                    raise AppError("embedding_rate_limit", "Embedding capacity is temporarily exhausted.", 503)
            elif response.status_code >= 400:
                raise AppError("embedding_unavailable", "Semantic search is temporarily unavailable.", 503)
            else:
                # Keep the cap on bytes actually allocated, not just on output
                # after a potentially unbounded compressed response expands.
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise AppError("embedding_response", "Embedding service returned an unsupported encoding.", 502)
                payload = bytearray()
                for piece in response.iter_bytes(chunk_size=8192):
                    if time.monotonic() > deadline:
                        raise AppError("embedding_timeout", "Embedding request timed out.", 503)
                    if len(payload) + len(piece) > settings.embedding_response_bytes:
                        raise _SplitBatch()
                    payload.extend(piece)
                result = json.loads(payload)
                if not isinstance(result, dict) or not isinstance(result.get("embeddings"), list):
                    raise ValueError("Invalid embedding envelope")
                return [item["values"] for item in result["embeddings"]]
        delay = 2 ** attempt
        if time.monotonic() + delay >= deadline:
            raise AppError("embedding_timeout", "Embedding request timed out.", 503)
        time.sleep(delay)
    raise AssertionError("Unreachable retry state")


def embed_texts(texts: list[str], task_type: str, *, titles: list[str] | None = None) -> list[list[float]]:
    """Embed at most 100 inputs, splitting oversized batches under one total deadline.

    Embedding 2 uses text instructions; 001 uses native taskType/title fields.
    Separate Content requests produce one vector per input, in input order.
    """
    if not texts:
        return []
    if not settings.gemini_api_key:
        raise AppError("embedding_unconfigured", "Semantic search is not configured.", 503)
    if task_type not in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}:
        raise ValueError("Unsupported embedding task type")
    if len(texts) > 100 or titles is not None and len(titles) != len(texts):
        raise ValueError("Invalid embedding batch size or titles")
    titles = titles if titles is not None else [""] * len(texts)
    deadline = time.monotonic() + settings.embedding_deadline_seconds

    def batch(client: httpx.Client, inputs: list[str], labels: list[str]) -> list[list[float]]:
        body = _request_body(inputs, task_type, labels)
        try:
            if len(inputs) > settings.embedding_batch_size or len(json.dumps(body).encode()) > settings.embedding_request_bytes:
                raise _SplitBatch()
            return validate_vectors(_send(client, body, deadline), len(inputs))
        except _SplitBatch as exc:
            if len(inputs) == 1:
                raise AppError("embedding_size_limit", "A single embedding exceeded its size limit.", 502) from exc
            middle = len(inputs) // 2
            return batch(client, inputs[:middle], labels[:middle]) + batch(client, inputs[middle:], labels[middle:])

    try:
        with httpx.Client() as client:
            return batch(client, texts, titles)
    except httpx.TimeoutException as exc:
        raise AppError("embedding_timeout", "Embedding request timed out.", 503) from exc
    except httpx.RequestError as exc:
        raise AppError("embedding_unavailable", "Semantic search is temporarily unavailable.", 503) from exc
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise AppError("embedding_response", "Embedding service returned an invalid response.", 502) from exc
