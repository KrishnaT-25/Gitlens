import io
import ast
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


def test_source_annotations_and_expressions_are_not_credentials():
    source = '''from typing import Literal
def authenticate(token: str, password: str | None = None, auth: bool = False):
    token: str = request.headers.get("Authorization")
    authorization: Bearer | None
    password = config.password
    secret = os.getenv("CLIENT_SECRET")
    token_kind: Literal["bearer"]
    return token, password, secret
'''
    assert ingest._redact(source, "src/auth.py") == source
    ast.parse(ingest._redact(source, "src/auth.py"))


def test_source_literals_keep_quotes_expressions_punctuation_and_line_numbers():
    source = '''greeting = "こんにちは"
def authenticate(token: str = "fixture-param-secret", password: str = "fixture-password"):
    token = "fixture-token" + suffix; keep = call(token)
    headers = {"Authorization": "Bearer fixture-header", "Accept": "application/json"}
    return token, headers
'''
    redacted = ingest._redact(source, "src/auth.py")
    for secret in ("fixture-param-secret", "fixture-password", "fixture-token", "fixture-header"):
        assert secret not in redacted
    assert 'token: str = "[REDACTED]", password: str = "[REDACTED]"' in redacted
    assert 'token = "[REDACTED]" + suffix; keep = call(token)' in redacted
    assert '"Accept": "application/json"' in redacted
    assert 'greeting = "こんにちは"' in redacted
    assert len(redacted.splitlines()) == len(source.splitlines())
    ast.parse(redacted)


@pytest.mark.parametrize("path,source", [
    ("src/auth.ts", 'function login(token: string, password: string): string { return token; }\nconst auth = { token: getToken(), password: "fixture-ts-secret", enabled: true };\n'),
    ("src/auth.js", 'const token = getToken("TOKEN");\nconst headers = {"Authorization": "Bearer fixture-js-secret", "Accept": "application/json"};\n'),
])
def test_javascript_and_typescript_remain_parseable(path, source):
    from tree_sitter_language_pack import get_parser
    redacted = ingest._redact(source, path)
    assert "fixture-" not in redacted
    assert 'getToken(' in redacted
    if path.endswith('.ts'):
        assert 'return token;' in redacted
    else:
        assert 'getToken("TOKEN")' in redacted
    language = "typescript" if path.endswith(".ts") else "javascript"
    assert not get_parser(language).parse(redacted.encode()).root_node.has_error


def test_multiline_strings_and_private_keys_preserve_original_line_breaks():
    source = ('token = """fixture-multiline\r\nsecond-secret-line\r\n"""\r\n'
              'key = """-----BEGIN PRIVATE KEY-----\r\nYWJjZA==\r\n-----END PRIVATE KEY-----"""\r\n'
              'token = "fixture-continued\\\r\nsecret-end"\r\n'
              'after = 42\r\n')
    redacted = ingest._redact(source, "src/keys.py")
    assert "fixture-" not in redacted and "secret-end" not in redacted and "YWJjZA==" not in redacted
    assert redacted.count("\r\n") == source.count("\r\n")
    assert redacted.endswith('after = 42\r\n')
    ast.parse(redacted)


@pytest.mark.parametrize("path,source", [
    (".env", 'TOKEN=fixture-env-secret # retain comment\r\nPORT=8000\r\n'),
    ("settings.ini", '[auth]\npassword=fixture-ini-secret\nport=8000\n'),
    ("config.json", '{"token": "fixture-json-secret", "enabled": true}\n'),
    ("config.yaml", 'auth:\n  token: fixture-yaml-secret # retain comment\nport: 8000\n'),
    ("config.toml", '[auth]\npassword = "fixture-toml-secret" # retain comment\nport = 8000\n'),
])
def test_configuration_values_are_redacted_without_removing_syntax(path, source):
    redacted = ingest._redact(source, path)
    assert "fixture-" not in redacted
    assert "8000" in redacted or '"enabled": true' in redacted
    assert len(redacted.splitlines()) == len(source.splitlines())
    if "# retain comment" in source:
        assert "# retain comment" in redacted
    if path.endswith('.json'):
        import json
        assert json.loads(redacted)["enabled"] is True


def test_redacted_archive_chunks_and_symbols_still_reference_original_lines():
    source = 'token: str\npassword = "fixture-password"\n\ndef authenticate(token: str):\n    return token\n'
    parsed = parse_repository(archive({"root/src/auth.py": source}))
    indexed = parsed.files[0]
    assert indexed.line_count == len(source.splitlines())
    assert "fixture-password" not in indexed.content
    assert any(s.name == "authenticate" and s.is_definition and s.line == 4 for s in parsed.symbols)
    for chunk in parsed.chunks:
        assert chunk.text == "\n".join(indexed.content.splitlines()[chunk.line_start - 1:chunk.line_end])


@pytest.mark.parametrize("secret", ["ghp_" + "A" * 36, "AIza" + "B" * 35, "sk-" + "C" * 32,
                                   "AKIA" + "D" * 16, "xoxb-" + "E" * 30])
def test_recognised_secret_formats_are_removed_without_a_sensitive_binding(secret):
    source = f'example = "{secret}"; after = 42\n# {secret}\n'
    redacted = ingest._redact(source, "src/example.py")
    assert secret not in redacted
    assert redacted == 'example = "[REDACTED TOKEN]"; after = 42\n# [REDACTED TOKEN]\n'
    ast.parse(redacted)


def test_fallback_redacts_literals_but_preserves_type_annotations_and_calls(monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    source = 'token: str\ntoken = getenv("TOKEN")\npassword = "fixture-fallback-secret"; after = 42\n'
    redacted = ingest._redact(source, "src/config.py")
    assert redacted == 'token: str\ntoken = getenv("TOKEN")\npassword = "[REDACTED]"; after = 42\n'
    ast.parse(redacted)


def test_yaml_block_values_and_templates_preserve_mapping_structure():
    import yaml
    source = 'auth:\n  password: |\n    fixture-block-secret\n    second-block-secret\n  token: ${TOKEN}\nport: 8000\n'
    redacted = ingest._redact(source, "config.yaml")
    assert "fixture-block-secret" not in redacted and "second-block-secret" not in redacted
    parsed = yaml.safe_load(redacted)
    assert parsed["port"] == 8000 and parsed["auth"]["token"] == "${TOKEN}"
    assert len(redacted.splitlines()) == len(source.splitlines())


def test_url_templates_and_incomplete_private_key_headers_keep_source_syntax():
    source = ('url = f"postgres://{user}:{password}@host/db"\n'
              'url2 = "postgres://user:fixture-db-secret@host/db"\n'
              'example = "-----BEGIN PRIVATE KEY-----"; after = 42\n')
    redacted = ingest._redact(source, "src/keys.py")
    assert 'f"postgres://{user}:{password}@host/db"' in redacted
    assert '"postgres://user:[REDACTED]@host/db"' in redacted
    assert 'example = "[REDACTED PRIVATE KEY]"; after = 42' in redacted
    ast.parse(redacted)


def test_type_alias_literals_are_not_runtime_credentials():
    source = 'type Token = "bearer";\ninterface Credentials { token: "bearer"; password: string; }\n'
    assert ingest._redact(source, "src/types.ts") == source


def test_typed_assignments_and_adjacent_literals_remain_valid_python():
    source = 'token: str = "fixture-typed-secret"\npassword = ("fixture-left" "fixture-right")\n'
    redacted = ingest._redact(source, "src/auth.py")
    assert redacted == 'token: str = "[REDACTED]"\npassword = ("[REDACTED]" "[REDACTED]")\n'
    ast.parse(redacted)


@pytest.mark.parametrize("source", [r'password = r"fixture\\"', r'password = "fixture\\"', 'password = b"fixture-bytes"'])
def test_raw_byte_and_escaped_string_delimiters_remain_valid(source):
    source += '\n'
    ast.parse(source)
    redacted = ingest._redact(source, "src/config.py")
    assert "fixture" not in redacted
    ast.parse(redacted)


@pytest.mark.parametrize("path", [".env", ".env.production", "settings.ini"])
def test_unquoted_configuration_secrets_can_contain_punctuation(path):
    source = 'PASSWORD=[fixture#suffix,more;end] # retained\r\nPORT=8000\r\n'
    assert ingest._redact(source, path) == 'PASSWORD=[REDACTED] # retained\r\nPORT=8000\r\n'


def test_yaml_multiline_quoted_values_keep_continuation_indentation():
    import yaml
    source = 'password: "fixture-first\n  fixture-second"\nport: 8000\n'
    redacted = ingest._redact(source, "config.yaml")
    assert "fixture-" not in redacted
    assert yaml.safe_load(redacted)["port"] == 8000
    assert redacted.splitlines()[1].startswith("  ")
