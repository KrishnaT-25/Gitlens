import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit
import httpx
from app.config import settings
from app.errors import AppError

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
RESERVED = {"about", "account", "apps", "codespaces", "collections", "contact", "dashboard", "enterprise", "events", "explore", "features", "issues", "login", "marketplace", "new", "notifications", "orgs", "pricing", "pulls", "search", "settings", "signup", "site", "sponsors", "topics", "trending", "users"}
ROUTES = {"tree", "blob", "issues", "commit", "commits"}
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

@dataclass(frozen=True)
class RepoURL:
    owner: str
    name: str
    explicit_sha: str | None
    route: str = ""
    ref_path: str | None = None


@dataclass(frozen=True)
class ResolvedRepository:
    owner: str
    name: str
    default_branch: str
    sha: str
    ref: str

def parse_repo_url(url: str) -> RepoURL:
    try:
        if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\x7f\\]", url):
            raise ValueError("Invalid URL characters")
        parsed = urlsplit(url)
        port = parsed.port
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
            raise ValueError("Invalid URL encoding")
        path = unquote(parsed.path, errors="strict")
        parts = path.removeprefix("/").removesuffix("/").split("/")
        if any(not p or p in {".", ".."} or re.search(r"[\x00-\x1f\x7f\\]", p) for p in parts):
            raise ValueError("Invalid path")
    except ValueError:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or port is not None:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    if len(parts) < 2 or parts[0].lower() in RESERVED or not OWNER_RE.fullmatch(parts[0]) or not REPO_RE.fullmatch(parts[1]) or parts[1] in {".", ".."}:
        raise AppError("invalid_url", "Enter a supported GitHub repository URL.")
    route = parts[2] if len(parts) > 2 else ""
    if route in {"pull", "pulls"}:
        raise AppError("unsupported_route", "Pull request pages are not supported. Open a branch, tag, or commit instead.", 422)
    if route and route not in ROUTES:
        raise AppError("unsupported_route", "This GitHub page is not supported.")
    tail = parts[3:]
    if ((route in {"tree", "blob", "commit"} and not tail)
            or (route == "blob" and len(tail) < 2)
            or (route == "commit" and (len(tail) != 1 or not COMMIT_RE.fullmatch(tail[0])))
            or (route == "issues" and tail and (len(tail) != 1 or not tail[0].isdigit()))):
        raise AppError("invalid_url", "Enter a valid branch, tag, file, or commit URL.")
    sha = parts[3].lower() if len(parts) > 3 and parts[2] in {"tree", "blob", "commit"} and SHA_RE.fullmatch(parts[3]) else None
    ref_path = "/".join(tail) if tail and route != "issues" else None
    return RepoURL(parts[0], parts[1], sha, route, ref_path)

def _headers():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "GitLens-Phase1", "X-GitHub-Api-Version": "2026-03-10"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers

def resolve_repo(repo: RepoURL) -> ResolvedRepository:
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), headers=_headers()) as client:
            data = _get_json(client, f"https://api.github.com/repos/{repo.owner}/{repo.name}")
            if data.get("private"):
                raise AppError("unsupported_repository", "Only public repositories are supported.", 422)
            branch = data["default_branch"]
            if not isinstance(branch, str) or not branch or len(branch) > 255:
                raise ValueError("Invalid default branch")
            base = f"https://api.github.com/repos/{repo.owner}/{repo.name}"
            ref = _resolve_ref(client, base, repo, branch)
            try:
                sha = _get_json(client, f"{base}/commits/{quote(ref, safe='')}")["sha"]
                if not isinstance(sha, str):
                    raise ValueError("Invalid GitHub SHA")
                sha = sha.lower()
            except AppError as exc:
                if exc.status == 404:
                    raise AppError("ref_not_found", "The selected branch, tag, or commit was not found.", 422) from exc
                raise
            if not SHA_RE.fullmatch(sha):
                raise ValueError("Invalid GitHub SHA")
            owner, name = data["owner"]["login"], data["name"]
            if not OWNER_RE.fullmatch(owner) or not REPO_RE.fullmatch(name):
                raise ValueError("Invalid canonical repository")
            return ResolvedRepository(owner, name, branch, sha, sha if COMMIT_RE.fullmatch(ref) else ref)
    except httpx.TimeoutException:
        raise AppError("github_timeout", "GitHub timed out. Try again.", 503)
    except httpx.RequestError:
        raise AppError("github_unavailable", "GitHub is unavailable. Try again.", 503)
    except (KeyError, ValueError, TypeError):
        raise AppError("github_response", "GitHub returned an unexpected response.", 502)

def _resolve_ref(client: httpx.Client, base: str, repo: RepoURL, default_branch: str) -> str:
    """Match the longest branch/tag prefix before interpreting the remaining file path."""
    if not repo.ref_path:
        return f"refs/heads/{default_branch}"
    if repo.route == "commit" or repo.explicit_sha:
        return repo.ref_path.split("/")[0]
    tail = repo.ref_path
    namespaces = ("heads", "tags")
    for namespace in namespaces:
        prefix = f"refs/{namespace}/"
        if tail.startswith(prefix):
            tail, namespaces = tail[len(prefix):], (namespace,)
            break
    matches = []
    for namespace in namespaces:
        refs = _get_json(client, f"{base}/git/matching-refs/{namespace}/{quote(tail.split('/')[0], safe='')}", expect_list=True)
        for entry in refs:
            ref = entry["ref"]
            prefix = f"refs/{namespace}/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise ValueError("Invalid reference response")
            name = ref[len(prefix):]
            if tail == name or tail.startswith(name + "/"):
                if repo.route != "blob" or tail != name:
                    matches.append((len(name), namespace == "heads", ref))
    if matches:
        return max(matches)[2]
    # GitHub also displays abbreviated immutable commits in tree/blob URLs.
    first = tail.split("/")[0]
    if COMMIT_RE.fullmatch(first):
        return first
    raise AppError("ref_not_found", "The selected branch or tag was not found. No default-branch fallback was used.", 422)


def _get_json(client: httpx.Client, url: str, *, expect_list: bool = False) -> dict | list:
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
    if not isinstance(parsed, list if expect_list else dict):
        raise ValueError("Unexpected GitHub JSON shape")
    return parsed

def _check(response: httpx.Response):
    if response.status_code == 404:
        raise AppError("repository_not_found", "Public repository not found.", 404)
    if response.status_code in (403, 429):
        raise AppError("github_rate_limit", "GitHub request limit reached. Try later.", 503)
    if response.status_code >= 400:
        raise AppError("github_unavailable", "GitHub is unavailable. Try again.", 503)
