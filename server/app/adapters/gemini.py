"""Bounded Gemini generation with one retry policy for summaries and agent turns."""
import json
import logging
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.config import settings
from app.errors import AppError
from app.deadlines import bounded_seconds, check_deadline, budgeted_sleep

logger = logging.getLogger(__name__)
ERROR_RESPONSE_BYTES = 16_384
MIN_RETRY_ATTEMPT_SECONDS = 0.1


def _provider_error(status: int) -> AppError:
    if status == 429:
        return AppError("gemini_rate_limit", "AI capacity is temporarily exhausted. Try later.", 503)
    return AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503)


def _retry_after(headers: httpx.Headers, payload: bytes) -> float | None:
    """Honor the longest valid HTTP or google.rpc.RetryInfo delay, without logging bodies."""
    delays = []
    value = headers.get("retry-after")
    if value:
        try:
            delays.append(float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delays.append(max(0, (date - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        details = json.loads(payload).get("error", {}).get("details", [])
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and detail.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
                    delay = detail.get("retryDelay")
                    if isinstance(delay, str) and delay.endswith("s"):
                        delays.append(float(delay[:-1]))
    except (ValueError, TypeError, AttributeError, RecursionError):
        pass
    valid = [delay for delay in delays if math.isfinite(delay) and delay >= 0]
    return max(valid) if valid else None


def _read(response: httpx.Response, limit: int, deadline: float, *, truncate: bool = False) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        raise AppError("gemini_response", "AI service returned an unsupported encoding.", 502)
    payload = bytearray()
    for piece in response.iter_bytes(chunk_size=8192):
        check_deadline()
        if time.monotonic() >= deadline:
            raise AppError("gemini_timeout", "AI service timed out. Try again.", 503)
        if len(payload) + len(piece) > limit:
            if truncate:
                return bytes(payload)
            raise AppError("gemini_response", "AI service response is too large.", 502)
        payload.extend(piece)
    return bytes(payload)


def _generate_content(body: dict, response_limit: int, timeout_seconds: float, total_seconds: float) -> dict:
    """Retry only 429/503, sharing the original operation and parent chat deadlines."""
    if not settings.gemini_api_key:
        raise AppError("gemini_unconfigured", "Repository analysis is not configured.", 503)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    deadline = time.monotonic() + bounded_seconds(total_seconds)
    try:
        with httpx.Client() as client:
            for attempt in range(settings.gemini_max_retries + 1):
                check_deadline()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError("gemini_timeout", "AI service timed out. Try again.", 503)
                operation_timeout = min(timeout_seconds, bounded_seconds(remaining))
                with client.stream("POST", url, headers={"x-goog-api-key": settings.gemini_api_key,
                                   "Accept-Encoding": "identity"}, json=body,
                                   timeout=httpx.Timeout(operation_timeout, connect=min(5, operation_timeout))) as response:
                    status = response.status_code
                    if status in {429, 503}:
                        error_payload = _read(response, ERROR_RESPONSE_BYTES, deadline, truncate=True)
                        hint = _retry_after(response.headers, error_payload)
                    elif status >= 400:
                        logger.warning("Gemini request failed with HTTP %s", status)
                        raise _provider_error(status)
                    else:
                        payload = _read(response, response_limit, deadline)
                        check_deadline()
                        result = json.loads(payload)
                        if not isinstance(result, dict):
                            raise ValueError("JSON object expected")
                        return result
                # The response is closed before waiting or making another request.
                if attempt == settings.gemini_max_retries:
                    raise _provider_error(status)
                cap = min(settings.gemini_retry_max_seconds, settings.gemini_retry_base_seconds * 2 ** attempt)
                delay = max(cap / 2 + random.uniform(0, cap / 2), hint or 0)
                remaining = bounded_seconds(deadline - time.monotonic())
                if delay > settings.gemini_retry_max_seconds or delay + MIN_RETRY_ATTEMPT_SECONDS >= remaining:
                    raise _provider_error(status)
                logger.warning("Retrying Gemini HTTP %s (retry %s/%s)", status, attempt + 1, settings.gemini_max_retries)
                budgeted_sleep(delay)
        raise AssertionError("Unreachable retry state")
    except httpx.TimeoutException as exc:
        check_deadline()
        raise AppError("gemini_timeout", "AI service timed out. Try again.", 503) from exc
    except httpx.RequestError as exc:
        check_deadline()
        raise AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503) from exc
    except (KeyError, IndexError, ValueError, TypeError, RecursionError) as exc:
        raise AppError("gemini_response", "AI service returned an invalid response. Try again.", 502) from exc


def generate_json(instruction: str, prompt: str, schema: dict, max_tokens: int = 2000, timeout_seconds: int = 20) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": instruction}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "maxOutputTokens": max_tokens,
                             "temperature": 0.1, "thinkingConfig": {"thinkingLevel": "low"}},
    }
    result = _generate_content(body, 128_000, timeout_seconds, timeout_seconds + 2)
    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("JSON object expected")
        return parsed
    except (KeyError, IndexError, ValueError, TypeError, RecursionError) as exc:
        raise AppError("gemini_response", "AI service returned an invalid response. Try again.", 502) from exc


def generate_tool_turn(instruction: str, contents: list[dict], declarations: list[dict], force_final: bool = False, require_tool: bool = False, *, thinking_level: str = "low") -> dict:
    """One model turn; transport retries neither consume extra agent turns nor change models."""
    if thinking_level not in {"minimal", "low", "medium", "high"}:
        raise ValueError("Unsupported thinking level")
    # Minimal is documented for Flash-Lite; newer Flash variants such as 3.8
    # reject it. Unknown models conservatively retain the existing low setting.
    if thinking_level == "minimal" and settings.gemini_model not in {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}:
        thinking_level = "low"
    config = {"maxOutputTokens": 3000, "temperature": 0.1, "thinkingConfig": {"thinkingLevel": thinking_level}}
    calling = {"mode": "ANY"}
    if force_final:
        calling = {"mode": "ANY", "allowedFunctionNames": ["final_answer"]}
    elif require_tool:
        calling = {"mode": "ANY", "allowedFunctionNames": ["search", "read_file", "list_dir", "find_references"]}
    body = {"systemInstruction": {"parts": [{"text": instruction}]}, "contents": contents,
            "tools": [{"functionDeclarations": declarations}], "toolConfig": {"functionCallingConfig": calling},
            "generationConfig": config}
    result = _generate_content(body, 256_000, 40, 45)
    try:
        content = result["candidates"][0]["content"]
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            raise ValueError("Invalid model content")
        calls = [part["functionCall"] for part in content["parts"] if isinstance(part, dict) and "functionCall" in part]
        return {"content": content, "calls": calls}
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise AppError("gemini_response", "AI service returned an invalid answer. Try again.", 502) from exc
