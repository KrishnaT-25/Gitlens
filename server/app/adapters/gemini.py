import json
import logging
import time
import httpx
from app.config import settings
from app.errors import AppError

logger = logging.getLogger(__name__)

def generate_json(instruction: str, prompt: str, schema: dict, max_tokens: int = 2000, timeout_seconds: int = 20, _retry: int = 0) -> dict:
    if not settings.gemini_api_key:
        raise AppError("gemini_unconfigured", "Repository analysis is not configured.", 503)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": instruction}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "maxOutputTokens": max_tokens, "temperature": 0.1, "thinkingConfig": {"thinkingLevel": "low"}},
    }
    try:
        deadline = time.monotonic() + timeout_seconds + 2
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=5)) as client:
            with client.stream("POST", url, headers={"x-goog-api-key": settings.gemini_api_key}, json=body) as response:
                if response.status_code == 429:
                    if _retry < 2:
                        time.sleep(min(2 ** _retry, 4))
                        return generate_json(instruction, prompt, schema, max_tokens, timeout_seconds, _retry + 1)
                    raise AppError("gemini_rate_limit", "AI capacity is temporarily exhausted. Try later.", 503)
                if response.status_code >= 400:
                    logger.error("Gemini status %s", response.status_code)
                    raise AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503)
                data = bytearray()
                for piece in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise AppError("gemini_timeout", "AI service timed out. Try again.", 503)
                    if len(data) + len(piece) > 128_000:
                        raise AppError("gemini_response", "AI service response is too large.", 502)
                    data.extend(piece)
        text = json.loads(data)["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("JSON object expected")
        return parsed
    except httpx.TimeoutException:
        raise AppError("gemini_timeout", "AI service timed out. Try again.", 503)
    except httpx.RequestError:
        raise AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503)
    except (KeyError, IndexError, ValueError, TypeError):
        raise AppError("gemini_response", "AI service returned an invalid response. Try again.", 502)

def generate_tool_turn(instruction: str, contents: list[dict], declarations: list[dict], force_final: bool = False) -> dict:
    """One bounded Gemini function-calling turn. Returns the provider content and function calls."""
    if not settings.gemini_api_key:
        raise AppError("gemini_unconfigured", "Repository analysis is not configured.", 503)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    config = {"maxOutputTokens": 3000, "temperature": 0.1, "thinkingConfig": {"thinkingLevel": "low"}}
    if force_final:
        config["functionCallingConfig"] = {"mode": "ANY", "allowedFunctionNames": ["final_answer"]}
    body = {"systemInstruction": {"parts": [{"text": instruction}]}, "contents": contents,
            "tools": [{"functionDeclarations": declarations}], "toolConfig": {"functionCallingConfig": config.pop("functionCallingConfig", {"mode": "AUTO"})},
            "generationConfig": config}
    try:
        deadline = time.monotonic() + 45
        with httpx.Client(timeout=httpx.Timeout(40, connect=5)) as client:
            with client.stream("POST", url, headers={"x-goog-api-key": settings.gemini_api_key}, json=body) as response:
                if response.status_code == 429:
                    raise AppError("gemini_rate_limit", "AI capacity is temporarily exhausted. Try later.", 503)
                if response.status_code >= 400:
                    logger.error("Gemini agent status %s", response.status_code)
                    raise AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503)
                payload = bytearray()
                for piece in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise AppError("gemini_timeout", "AI service timed out. Try again.", 503)
                    if len(payload) + len(piece) > 256_000:
                        raise AppError("gemini_response", "AI service response is too large.", 502)
                    payload.extend(piece)
        content = json.loads(payload)["candidates"][0]["content"]
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            raise ValueError("Invalid model content")
        calls = [part["functionCall"] for part in content["parts"] if isinstance(part, dict) and "functionCall" in part]
        return {"content": content, "calls": calls}
    except httpx.TimeoutException as exc:
        raise AppError("gemini_timeout", "AI service timed out. Try again.", 503) from exc
    except httpx.RequestError as exc:
        raise AppError("gemini_unavailable", "AI service is unavailable. Try later.", 503) from exc
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AppError("gemini_response", "AI service returned an invalid response. Try again.", 502) from exc
