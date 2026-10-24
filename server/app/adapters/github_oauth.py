"""Exchange a GitHub OAuth code and read the public identity only."""

import json
import re
import time

import httpx

from app.config import settings
from app.errors import AppError

LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


def _bounded_json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    try:
        deadline = time.monotonic() + 12
        with client.stream(method, url, **kwargs) as response:
            if response.status_code >= 500:
                raise AppError("github_unavailable", "GitHub sign-in is unavailable. Try again.", 503)
            if response.status_code >= 400:
                raise AppError("github_signin_failed", "GitHub sign-in failed. Try again.", 401)
            data = bytearray()
            for piece in response.iter_bytes():
                if time.monotonic() > deadline or len(data) + len(piece) > 64_000:
                    raise AppError("github_response", "GitHub sign-in response was invalid.", 502)
                data.extend(piece)
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("Object expected")
        return parsed
    except httpx.TimeoutException:
        raise AppError("github_timeout", "GitHub sign-in timed out. Try again.", 503)
    except httpx.RequestError:
        raise AppError("github_unavailable", "GitHub sign-in is unavailable. Try again.", 503)
    except (ValueError, TypeError):
        raise AppError("github_response", "GitHub sign-in response was invalid.", 502)


def identify(code: str, verifier: str) -> tuple[str, str]:
    if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
        raise AppError("oauth_unconfigured", "GitHub sign-in is not configured.", 503)
    with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False) as client:
        token_data = _bounded_json(client, "POST", "https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, data={
            "client_id": settings.github_oauth_client_id,
            "client_secret": settings.github_oauth_client_secret,
            "code": code,
            "redirect_uri": settings.github_oauth_redirect_uri,
            "code_verifier": verifier,
        })
        token = token_data.get("access_token")
        if token_data.get("error") or not isinstance(token, str) or not token or token_data.get("scope") not in (None, ""):
            raise AppError("github_signin_failed", "GitHub sign-in failed or requested unexpected access.", 401)
        profile = _bounded_json(client, "GET", "https://api.github.com/user", headers={
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "User-Agent": "GitLens-Phase1",
        })
    github_id, login = profile.get("id"), profile.get("login")
    if not isinstance(github_id, int) or isinstance(github_id, bool) or github_id <= 0 or not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise AppError("github_response", "GitHub returned an invalid identity.", 502)
    return str(github_id), login
