import json
import time
import httpx
from app.config import settings
from app.errors import AppError

def embed_texts(texts: list[str], task_type: str, _retry: int = 0) -> list[list[float]]:
    """Return one normalized-dimension vector per input, or raise a bounded provider error."""
    if not texts:
        return []
    if not settings.gemini_api_key:
        raise AppError("embedding_unconfigured", "Semantic search is not configured.", 503)
    if task_type not in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}:
        raise ValueError("Unsupported embedding task type")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_embedding_model}:batchEmbedContents"
    body = {"requests": [
        {"model": f"models/{settings.gemini_embedding_model}",
         "content": {"parts": [{"text": text[:8000]}]},
         "taskType": task_type,
         "outputDimensionality": settings.embedding_dimensions}
        for text in texts
    ]}
    try:
        deadline = time.monotonic() + 45
        with httpx.Client(timeout=httpx.Timeout(40, connect=5)) as client:
            with client.stream("POST", url, headers={"x-goog-api-key": settings.gemini_api_key}, json=body) as response:
                if response.status_code == 429:
                    if _retry < 2:
                        time.sleep(min(2 ** _retry, 4))
                        return embed_texts(texts, task_type, _retry + 1)
                    raise AppError("embedding_rate_limit", "Embedding capacity is temporarily exhausted.", 503)
                if response.status_code >= 400:
                    raise AppError("embedding_unavailable", "Semantic search is temporarily unavailable.", 503)
                payload = bytearray()
                for piece in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise AppError("embedding_timeout", "Embedding request timed out.", 503)
                    if len(payload) + len(piece) > 256_000:
                        raise AppError("embedding_response", "Embedding response exceeded its size limit.", 502)
                    payload.extend(piece)
        result = json.loads(payload)
        vectors = [item["values"] for item in result["embeddings"]]
        if len(vectors) != len(texts) or any(len(vector) != settings.embedding_dimensions for vector in vectors):
            raise ValueError("Embedding batch dimensions did not match the request")
        return vectors
    except httpx.TimeoutException as exc:
        raise AppError("embedding_timeout", "Embedding request timed out.", 503) from exc
    except httpx.RequestError as exc:
        raise AppError("embedding_unavailable", "Embedding service is temporarily unavailable.", 503) from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AppError("embedding_response", "Embedding service returned an invalid response.", 502) from exc
