import pytest
import json
from app.errors import AppError
from app.services import repo_tools
from app.services.repo_tools import safe_path

@pytest.mark.parametrize("path", ["../secret", "src/../../secret", "a\\..\\b", "."])
def test_repository_tool_rejects_traversal(path):
    with pytest.raises(AppError) as error:
        safe_path(path)
    assert error.value.code == "invalid_tool_path"

def test_repository_tool_normalizes_slashes():
    assert safe_path("/src\\app.py/") == "src/app.py"

def test_tool_json_output_stays_under_configured_size(monkeypatch):
    monkeypatch.setattr(repo_tools.settings, "max_tool_output_chars", 1000)
    result = repo_tools._bounded({"text": "x" * 5000})
    assert result["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
