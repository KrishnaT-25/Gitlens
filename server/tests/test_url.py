import pytest
from app.adapters.github import parse_repo_url
from app.errors import AppError

@pytest.mark.parametrize("url,sha", [
    ("https://github.com/openai/openai-python", None),
    ("https://github.com/openai/openai-python/blob/main/src/file.py?x=1", None),
    ("https://github.com/openai/openai-python/commit/" + "a" * 40, "a" * 40),
])
def test_supported_url(url, sha):
    parsed = parse_repo_url(url)
    assert (parsed.owner, parsed.name, parsed.explicit_sha) == ("openai", "openai-python", sha)

@pytest.mark.parametrize("url", [
    "https://github.com.evil.test/a/b", "http://github.com/a/b", "https://github.com/search?q=a",
    "https://github.com/a/b/settings", "https://github.com/a", "https://user@github.com/a/b",
])
def test_unsupported_url(url):
    with pytest.raises(AppError):
        parse_repo_url(url)
