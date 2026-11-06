"""Ref-resolution regressions with a mocked GitHub HTTP transport (no live providers)."""

from urllib.parse import unquote

import httpx
import pytest

from app.adapters import github
from app.errors import AppError

BASE = "https://api.github.com/repos/owner/repo"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def mock_github(monkeypatch, refs=None, *, commit_status=200):
    """Serve metadata, matching refs, and peeled commits through the real adapter."""
    seen = []
    refs = refs or {"heads": ["main", "develop", "feature/search", "feature/searches"],
                    "tags": ["v1.0", "release/2026", "develop"]}

    def handle(request):
        seen.append(request)
        path = unquote(str(request.url)).removeprefix(BASE)
        if path == "":
            return httpx.Response(200, json={"private": False, "default_branch": "main",
                                           "owner": {"login": "owner"}, "name": "repo"})
        if path.startswith("/git/matching-refs/"):
            namespace, prefix = path.removeprefix("/git/matching-refs/").split("/", 1)
            return httpx.Response(200, json=[{"ref": f"refs/{namespace}/{name}"}
                for name in refs.get(namespace, []) if name.startswith(prefix)])
        if path.startswith("/commits/"):
            ref = path.removeprefix("/commits/")
            return httpx.Response(commit_status, json={"sha": SHA if ref == "refs/heads/main" else OTHER_SHA})
        pytest.fail(f"Unexpected GitHub request: {path}")

    client = httpx.Client
    monkeypatch.setattr(github.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    return seen


@pytest.mark.parametrize("path,expected_ref", [
    ("", "refs/heads/main"),
    ("/issues/12", "refs/heads/main"),
    ("/commits", "refs/heads/main"),
    ("/tree/develop", "refs/heads/develop"),
    ("/blob/develop/src/app.py", "refs/heads/develop"),
    ("/tree/feature/search", "refs/heads/feature/search"),
    ("/blob/feature/search/src/app.py", "refs/heads/feature/search"),
    ("/tree/feature%2Fsearch/docs", "refs/heads/feature/search"),
    ("/tree/v1.0", "refs/tags/v1.0"),
    ("/tree/release/2026/docs", "refs/tags/release/2026"),
    ("/tree/refs/tags/develop", "refs/tags/develop"),
    ("/tree/refs/heads/feature/search", "refs/heads/feature/search"),
    ("/commit/" + OTHER_SHA, OTHER_SHA),
    ("/commit/bbbbbbb", "bbbbbbb"),
    ("/tree/" + OTHER_SHA + "/src", OTHER_SHA),
    ("/blob/" + OTHER_SHA + "/src/app.py", OTHER_SHA),
    ("/commits/feature/search", "refs/heads/feature/search"),
])
def test_selected_ref_is_resolved_to_immutable_commit(monkeypatch, path, expected_ref):
    seen = mock_github(monkeypatch)
    resolved = github.resolve_repo(github.parse_repo_url("https://github.com/owner/repo" + path))
    assert resolved.sha == (SHA if expected_ref == "refs/heads/main" else OTHER_SHA)
    assert resolved.default_branch == "main"
    assert resolved.ref == (resolved.sha if github.COMMIT_RE.fullmatch(expected_ref) else expected_ref)
    assert unquote(str(seen[-1].url)) == BASE + "/commits/" + expected_ref
    assert len(seen) <= 4


def test_missing_ref_never_falls_back_to_default_branch(monkeypatch):
    seen = mock_github(monkeypatch)
    with pytest.raises(AppError) as error:
        github.resolve_repo(github.parse_repo_url("https://github.com/owner/repo/tree/feature/searc"))
    assert (error.value.code, error.value.status) == ("ref_not_found", 422)
    assert not any("/commits/" in str(request.url) for request in seen)


def test_longest_ref_prefix_wins_over_shorter_ref(monkeypatch):
    mock_github(monkeypatch, {"heads": ["release"], "tags": ["release/2026"]})
    resolved = github.resolve_repo(github.parse_repo_url("https://github.com/owner/repo/blob/release/2026/src/app.py"))
    assert resolved.ref == "refs/tags/release/2026"


def test_missing_commit_returns_ref_error(monkeypatch):
    mock_github(monkeypatch, commit_status=404)
    with pytest.raises(AppError) as error:
        github.resolve_repo(github.parse_repo_url("https://github.com/owner/repo/commit/" + OTHER_SHA))
    assert (error.value.code, error.value.status) == ("ref_not_found", 422)


@pytest.mark.parametrize("url", [
    "https://github.com:bad/a/b", "https://github.com:0/a/b", "https://github.com:443/a/b", "https://github.com:/a/b",
    "https://github.com/a/b/tree", "https://github.com/a/b/blob/main", "https://github.com/a/b/commit/main",
    "https://github.com/a/b/commit/12345", "https://github.com/a/b/commit/" + SHA + "/extra",
    "https://github.com/a//b", "https://github.com/a/b/tree/main/../secret",
    "https://github.com/a/b/tree/main/%2e%2e/secret", "https://github.com/a/b/tree/%ZZ",
    "https://github.com/a/b/tree/main%00", "https://github.com/a/b/issues/not-a-number",
    "https://github.com/a/b\\tree\\main", " https://github.com/a/b",
])
def test_malformed_urls_return_validation_errors(url):
    with pytest.raises(AppError) as error:
        github.parse_repo_url(url)
    assert (error.value.code, error.value.status) == ("invalid_url", 400)


@pytest.mark.parametrize("path", ["/pull", "/pull/123", "/pull/123/files", "/pulls"])
def test_pull_request_context_is_explicitly_unsupported(path):
    with pytest.raises(AppError) as error:
        github.parse_repo_url("https://github.com/a/b" + path)
    assert (error.value.code, error.value.status) == ("unsupported_route", 422)
    assert "Pull request" in error.value.message


@pytest.mark.parametrize("path,code,status", [
    ("/tree", "invalid_url", 400),
    ("/pull/12", "unsupported_route", 422),
])
def test_submission_returns_structured_validation_errors_without_github_or_db(path, code, status):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app.main import app, current_user, get_db

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id="user")
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).post("/api/repositories", json={"url": "https://github.com/o/r" + path})
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
    finally:
        app.dependency_overrides = previous
