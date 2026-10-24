import io
import sys
import zipfile
import pytest
from app.errors import AppError
from app.services import ingest
from app.services.ingest import parse_archive, parse_repository

def archive(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for path, content in files.items():
            z.writestr(path, content)
    return out.getvalue()

def test_line_mapping_and_filtering():
    text = "\n".join(f"line {n}" for n in range(1, 201))
    result = parse_archive(archive({"root/src/main.py": text, "root/node_modules/x.js": "ignored", "root/.env": "SECRET=abc"}))
    source = [c for c in result if c.path == "src/main.py"]
    assert [(c.line_start, c.line_end) for c in source] == [(1, 80), (81, 160), (161, 200)]
    assert any(c.path == ".env" and "[REDACTED]" in c.text for c in result)
    assert all(c.path != "node_modules/x.js" for c in result)

def test_traversal_rejected():
    with pytest.raises(AppError) as error:
        parse_archive(archive({"root/../evil.py": "bad"}))
    assert error.value.code == "unsafe_archive"

def test_empty_repository_truthful():
    with pytest.raises(AppError) as error:
        parse_archive(archive({"root/logo.png": b"\0\1\2"}))
    assert error.value.code == "empty_repository"

def test_detectable_secret_is_redacted_and_kept():
    result = parse_archive(archive({"root/secret.py": "-----BEGIN PRIVATE KEY-----", "root/app.py": "print('safe')"}))
    secret = next(c for c in result if c.path == "secret.py")
    assert "[REDACTED PRIVATE KEY]" in secret.text

def test_code_chunks_include_symbol_metadata():
    parsed = parse_repository(archive({"root/src/greeter.py": "def greet(name):\n    return f'Hello {name}'\n"}))
    chunk = next(c for c in parsed.chunks if c.symbol_path)
    assert chunk.symbol_path == "greet"
    assert chunk.language == "python"
    assert any(symbol.name == "greet" and symbol.is_definition for symbol in parsed.symbols)

def test_indexed_line_count_does_not_add_phantom_line_for_trailing_newline():
    parsed = parse_repository(archive({"root/README.md": "single line\n"}))
    file = parsed.files[0]
    assert file.line_count == len(file.content.splitlines()) == 1

def test_markdown_sections_are_chunked_with_heading_metadata():
    parsed = parse_repository(archive({"root/README.md": "# Intro\nWelcome\n## Install\nRun setup\n"}))
    sections = [c.symbol_path for c in parsed.chunks]
    assert "Intro" in sections
    assert "Install" in sections

def test_yaml_top_level_keys_are_chunked_by_section():
    parsed = parse_repository(archive({"root/config.yml": "server:\n  port: 8000\ndatabase:\n  url: local\n"}))
    assert {c.symbol_path for c in parsed.chunks} >= {"server", "database"}

def test_missing_tree_sitter_grammar_falls_back_to_line_chunks(monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    chunks, symbols = ingest._parse_code("src/main.py", "def main():\n    return 1\n", "python")
    assert chunks and all(chunk.line_start > 0 for chunk in chunks)
    assert symbols == []

def test_environment_and_connection_secrets_are_redacted():
    parsed = parse_repository(archive({"root/.env": "GEMINI_API_KEY=AIza123456789012345678901234567890\nDATABASE_URL=postgres://user:password@host/db\nPASSWORD=secret phrase with spaces\n"}))
    text = "\n".join(chunk.text for chunk in parsed.chunks)
    assert "AIza123456789012345678901234567890" not in text
    assert "password@host" not in text
    assert "secret phrase" not in text
    assert text.count("[REDACTED]") >= 2
