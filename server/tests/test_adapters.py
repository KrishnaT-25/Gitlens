import json
import httpx
import pytest
from app.adapters import gemini
from app.adapters import workflow
from app.adapters import github_oauth
from app.errors import AppError

@pytest.mark.parametrize("status,body,expected", [
    (429, {}, "gemini_rate_limit"),
    (200, {"candidates": []}, "gemini_response"),
    (200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}, "gemini_response"),
])
def test_gemini_rejects_provider_failures(monkeypatch, status, body, expected):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "test-key")
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, json=body))
    monkeypatch.setattr(gemini.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(AppError) as error:
        gemini.generate_json("instruction", "evidence", {"type": "OBJECT"})
    assert error.value.code == expected

def test_gemini_timeout_is_safe(monkeypatch):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "test-key")
    real_client = httpx.Client
    def timeout(request):
        raise httpx.ReadTimeout("provider timed out", request=request)
    transport = httpx.MockTransport(timeout)
    monkeypatch.setattr(gemini.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(AppError) as error:
        gemini.generate_json("instruction", "evidence", {"type": "OBJECT"})
    assert error.value.code == "gemini_timeout"

@pytest.mark.parametrize("force_final,require_tool,mode,allowed", [
    (False, True, "ANY", ["search", "read_file", "list_dir", "find_references"]),
    (True, True, "ANY", ["final_answer"]),
])
def test_agent_turn_requires_repository_inspection_before_final(monkeypatch, force_final, require_tool, mode, allowed):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "test-key")
    request_body = {}
    response_body = {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "search", "args": {"query": "localStorage"}}}
    ]}}]}
    real_client = httpx.Client
    def handle(request):
        request_body.update(json.loads(request.content))
        return httpx.Response(200, json=response_body)
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(gemini.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    gemini.generate_tool_turn("instruction", [], [], force_final=force_final, require_tool=require_tool)
    config = request_body["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == mode
    assert config["allowedFunctionNames"] == allowed

def test_worker_dispatch_targets_configured_workflow(monkeypatch):
    monkeypatch.setattr(workflow.settings, "worker_dispatch_repo", "owner/gitlens")
    monkeypatch.setattr(workflow.settings, "worker_dispatch_ref", "main")
    monkeypatch.setattr(workflow.settings, "worker_dispatch_token", "test-token")
    seen = []
    real_client = httpx.Client
    def handle(request):
        seen.append((str(request.url), request.headers["authorization"], request.content))
        return httpx.Response(204)
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(workflow.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    workflow.dispatch_ingestion()
    assert seen[0][0] == "https://api.github.com/repos/owner/gitlens/actions/workflows/ingest.yml/dispatches"
    assert seen[0][1] == "Bearer test-token"
    assert b'"ref":"main"' in seen[0][2]


def test_github_oauth_reads_public_identity_without_retaining_token(monkeypatch):
    monkeypatch.setattr(github_oauth.settings, "github_oauth_client_id", "client-id")
    monkeypatch.setattr(github_oauth.settings, "github_oauth_client_secret", "secret")
    seen = []
    real_client = httpx.Client
    def handle(request):
        seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            assert b"code_verifier=" in request.content
            return httpx.Response(200, json={"access_token": "temporary-token", "scope": "", "token_type": "bearer"})
        assert request.headers["authorization"] == "Bearer temporary-token"
        return httpx.Response(200, json={"id": 12345, "login": "octocat"})
    monkeypatch.setattr(github_oauth.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    assert github_oauth.identify("code", "a" * 43) == ("12345", "octocat")
    assert len(seen) == 2


def test_github_oauth_rejects_unexpected_scope(monkeypatch):
    monkeypatch.setattr(github_oauth.settings, "github_oauth_client_id", "client-id")
    monkeypatch.setattr(github_oauth.settings, "github_oauth_client_secret", "secret")
    real_client = httpx.Client
    monkeypatch.setattr(github_oauth.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"access_token": "temporary-token", "scope": "repo"})), **kwargs))
    with pytest.raises(AppError) as error:
        github_oauth.identify("code", "a" * 43)
    assert error.value.code == "github_signin_failed"
