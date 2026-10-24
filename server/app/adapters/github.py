import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
import httpx
from app.config import settings
from app.errors import AppError

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
RESERVED = {"about", "account", "apps", "codespaces", "collections", "contact", "dashboard", "enterprise", "events", "explore", "features", "issues", "login", "marketplace", "new", "notifications", "orgs", "pricing", "pulls", "search", "settings", "signup", "site", "sponsors", "topics", "trending", "users"}
ROUTES = {"tree", "blob", "issues", "pull", "pulls", "commit", "commits"}

@dataclass(frozen=True)
class RepoURL:
    owner: str
    name: str
    explicit_sha: str | None

def parse_repo_url(url: str) -> RepoURL:
    try:
        parsed = urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
    except ValueError:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port or parsed.username or parsed.password:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    if len(parts) < 2 or parts[0].lower() in RESERVED or not OWNER_RE.fullmatch(parts[0]) or not REPO_RE.fullmatch(parts[1]) or parts[1] in {".", ".."}:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    if len(parts) > 2 and parts[2] not in ROUTES:
        raise AppError("unsupported_route", "This GitHub page is not supported.")
    sha = parts[3].lower() if len(parts) > 3 and parts[2] in {"tree", "blob", "commit"} and SHA_RE.fullmatch(parts[3]) else None
    return RepoURL(parts[0], parts[1], sha)

def _headers():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "GitLens-Phase1", "X-GitHub-Api-Version": "2026-03-10"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers

def resolve_repo(repo: RepoURL) -> tuple[str, str, str, str]:
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), headers=_headers()) as client:
            data = _get_json(client, f"https://api.github.com/repos/{repo.owner}/{repo.name}")
            if data.get("private"):
                raise AppError("unsupported_repository", "Only public repositories are supported.", 422)
            branch = data["default_branch"]
            sha_ref = repo.explicit_sha or branch
            if not isinstance(branch, str) or not branch or len(branch) > 255:
                raise ValueError("Invalid default branch")
            sha = _get_json(client, f"https://api.github.com/repos/{repo.owner}/{repo.name}/commits/{quote(sha_ref, safe='')}")["sha"].lower()
            if not SHA_RE.fullmatch(sha):
                raise ValueError("Invalid GitHub SHA")
            owner, name = data["owner"]["login"], data["name"]
            if not OWNER_RE.fullmatch(owner) or not REPO_RE.fullmatch(name):
                raise ValueError("Invalid canonical repository")
            return owner, name, branch, sha
    except httpx.TimeoutException:
        raise AppError("github_timeout", "GitHub timed out. Try again.", 503)
    except httpx.RequestError:
        raise AppError("github_unavailable", "GitHub is unavailable. Try again.", 503)
    except (KeyError, ValueError, TypeError):
        raise AppError("github_response", "GitHub returned an unexpected response.", 502)

def _get_json(client: httpx.Client, url: str) -> dict:
    deadline = time.monotonic() + 15
    with client.stream("GET", url) as response:
        _check(response)
        data = bytearray()
        for piece in response.iter_bytes():
            if time.monotonic() > deadline:
                raise AppError("github_timeout", "GitHub timed out. Try again.", 503)
            if len(data) + len(piece) > 1_000_000:
                raise AppError("github_response", "GitHub response is too large.", 502)
            data.extend(piece)
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("GitHub JSON object expected")
    return parsed

def _check(response: httpx.Response):
    if response.status_code == 404:
        raise AppError("repository_not_found", "Public repository not found.", 404)
    if response.status_code in (403, 429):
        raise AppError("github_rate_limit", "GitHub request limit reached. Try later.", 503)
    if response.status_code >= 400:
        raise AppError("github_unavailable", "GitHub is unavailable. Try again.", 503)
