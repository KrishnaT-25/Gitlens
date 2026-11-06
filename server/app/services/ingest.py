import io
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from collections import Counter
import httpx
from app.config import settings
from app.errors import AppError
from app.adapters.github import _headers, _check

SKIP_DIRS = {".git", ".svn", ".hg", ".next", ".nuxt", ".angular", ".parcel-cache", ".turbo", ".cache", ".terraform", ".gradle", ".pytest_cache", ".mypy_cache", "build", "dist", "coverage", "htmlcov", "out", "target", "storybook-static", "node_modules", "bower_components", "vendor", "third_party", "venv", ".venv", "__pycache__", "site-packages", "pods", "carthage"}
SKIP_SUFFIXES = {".bin", ".class", ".dll", ".exe", ".gif", ".ico", ".jar", ".jpeg", ".jpg", ".map", ".min.js", ".pdf", ".png", ".pyc", ".woff", ".woff2", ".zip", ".tar", ".gz", ".7z", ".mp3", ".mp4", ".sqlite", ".db"}
SECRET_NAME_RE = re.compile(r"(?:^|[_\W])(?:api[_-]?key|access[_-]?token|auth|authorization|auth[_-]?token|client[_-]?secret|password|passwd|secret|secret[_-]?(?:key|token)|token|private[_-]?key|credential|database[_-]?url|db[_-]?url|connection[_-]?string|jwt[_-]?secret|signing[_-]?key)(?:$|[_\W])", re.I)
QUOTED_VALUE = r'"""[\s\S]*?"""' + "|'''[\\s\\S]*?'''|" + r'"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\''
KEY_PATTERN = r"[\"']?[A-Za-z_][A-Za-z0-9_.-]*[\"']?"
CONFIG_VALUE_RE = re.compile(r"(?P<prefix>(?P<key>" + KEY_PATTERN + r")[ \t]*[:=][ \t]*)(?P<value>" + QUOTED_VALUE + r"|[^\r\n,;{}#]+)")
LINE_CONFIG_VALUE_RE = re.compile(r"(?m)^(?P<prefix>[ \t]*(?:export[ \t]+)?(?P<key>" + KEY_PATTERN + r")[ \t]*[:=][ \t]*)(?P<value>" + QUOTED_VALUE + r"|[^\r\n]+)")
SOURCE_LITERAL_RE = re.compile(r"(?P<key>" + KEY_PATTERN + r")(?:[ \t]*:[ \t]*[^=,;\r\n]+)?[ \t]*[:=][ \t]*(?:[rubRUB]{0,2})(?P<value>" + QUOTED_VALUE + ")")
STRING_BODY_RE = re.compile(r"(?is)^(?:[rub]{0,2})(?P<quote>\"\"\"|'''|[\"'`])(?P<body>.*)(?P=quote)$")
CONFIG_SUFFIXES = {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties"}
AUTH_HEADER_RE = re.compile(r"(?im)(\b(?:authorization|proxy-authorization)\s*:\s*)(?:bearer|basic)\s+[^\s,;]+")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{24,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,})\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.DOTALL)
EXTENSION_LANGUAGE = {".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "c_sharp", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".sh": "bash", ".bash": "bash", ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss", ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml", ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".tf": "hcl", ".hcl": "hcl", ".vue": "vue", ".svelte": "svelte", ".ex": "elixir", ".exs": "elixir"}
DEFINITION_NODES = {"function_definition", "function_declaration", "generator_function_declaration", "method_definition", "method_declaration", "singleton_method", "method", "class", "class_definition", "class_declaration", "interface_declaration", "struct_item", "struct_specifier", "enum_item", "enum_specifier", "trait_item", "impl_item", "function_item", "constructor_declaration", "type_alias_declaration", "type_declaration", "type_spec", "type_definition", "namespace_definition", "module", "module_definition", "record_declaration", "object_declaration"}

@dataclass(frozen=True)
class IndexedChunk:
    path: str
    line_start: int
    line_end: int
    text: str
    language: str | None = None
    symbol_path: str | None = None
    syntax_kind: str = "text"

@dataclass(frozen=True)
class IndexedFile:
    path: str
    content: str
    language: str | None
    summary: str
    line_count: int
    byte_size: int

@dataclass(frozen=True)
class SymbolOccurrence:
    name: str
    path: str
    line: int
    symbol_path: str | None
    is_definition: bool

@dataclass(frozen=True)
class ParsedRepository:
    files: list[IndexedFile]
    chunks: list[IndexedChunk]
    symbols: list[SymbolOccurrence]
    omitted: dict

def download_archive(owner: str, name: str, sha: str) -> bytes:
    url = f"https://api.github.com/repos/{owner}/{name}/zipball/{sha}"
    try:
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), headers=_headers(), follow_redirects=False) as client:
            for redirect_count in range(2):
                with client.stream("GET", url) as stream:
                    if stream.status_code in (301, 302, 303, 307, 308) and redirect_count == 0:
                        target = stream.headers.get("location", "")
                        parsed = urlsplit(target)
                        if parsed.scheme != "https" or parsed.hostname not in {"codeload.github.com", "api.github.com"} or parsed.port or parsed.username or parsed.password:
                            raise AppError("unsafe_archive_url", "GitHub archive redirect was rejected.", 502)
                        url = target
                        continue
                    _check(stream)
                    buffer = io.BytesIO()
                    for piece in stream.iter_bytes(65536):
                        if time.monotonic() > deadline:
                            raise AppError("github_timeout", "GitHub archive timed out.", 503)
                        if buffer.tell() + len(piece) > settings.archive_bytes:
                            raise AppError("archive_too_large", "Repository archive is too large.", 422)
                        buffer.write(piece)
                    return buffer.getvalue()
            raise AppError("unsafe_archive_url", "GitHub archive redirect was rejected.", 502)
    except httpx.TimeoutException:
        raise AppError("github_timeout", "GitHub archive timed out.", 503)
    except httpx.RequestError:
        raise AppError("github_unavailable", "GitHub archive is unavailable.", 503)

def _eligible(path: str) -> bool:
    low = path.lower()
    parts = PurePosixPath(low).parts
    if any(p in SKIP_DIRS for p in parts) or any(low.endswith(s) for s in SKIP_SUFFIXES):
        return False
    return True

def _redact(content: str, path: str) -> str:
    """Redact values before indexing, preserving delimiters and every line break."""
    if path.lower().endswith((".pem", ".key", ".p12", ".pfx")):
        return _mask_lines(content, "[REDACTED SENSITIVE MATERIAL]")
    filename = PurePosixPath(path.lower())
    config = filename.suffix in CONFIG_SUFFIXES or filename.name == ".env" or filename.name.startswith(".env.") or filename.name in {".npmrc", ".pypirc"}
    content = _redact_config(content, filename.suffix) if config else _redact_source(content, path)
    content = PRIVATE_KEY_RE.sub(lambda match: _mask_lines(match.group(0), "[REDACTED PRIVATE KEY]"), content)
    # Incomplete markers must not erase the surrounding assignment or quotes.
    content = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", content)
    content = re.sub(r"(?i)(://[A-Za-z0-9_.%+-]+:)[^@/\s\"'`{}<>\\]+(@)", r"\1[REDACTED]\2", content)
    if config or filename.suffix in {".md", ".rst", ".txt", ".http"}:
        content = AUTH_HEADER_RE.sub(r"\1[REDACTED]", content)
    return TOKEN_RE.sub("[REDACTED TOKEN]", content)


def _mask_lines(value: str, marker: str = "[REDACTED]") -> str:
    """Preserve CRLF/LF and escaped line continuations inside quoted strings."""
    def replace(match):
        continuation = re.search(r"\\+$", match.group(0))
        followed_by_newline = match.end() < len(value) and value[match.end()] in "\r\n"
        indent = re.match(r"[ \t]*", match.group(0)).group(0)
        return indent + marker + (continuation.group(0) if followed_by_newline and continuation else "")
    return re.sub(r"[^\r\n]+", replace, value)


def _secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)))


def _redact_config(content: str, suffix: str) -> str:
    """Handle configuration scalars, including unquoted values and YAML blocks."""
    structured = suffix in {".json", ".jsonc", ".toml", ".yaml", ".yml"}
    def replace(match):
        value = match.group("value")
        if (not _secret_name(match.group("key")) or value.lstrip().startswith(("$", "{{"))
                or (structured and value.lstrip().startswith("["))):
            return match.group(0)
        literal = STRING_BODY_RE.fullmatch(value)
        if literal:
            start, end = literal.span("body")
            return match.group("prefix") + value[:start] + _mask_lines(value[start:end]) + value[end:]
        comment = re.search(r"[ \t]+[#;]", value) if not structured else None
        scalar = value[:comment.start()] if comment else value
        stripped = scalar.rstrip(" \t")
        if not stripped or re.fullmatch(r"[|>][+-]?\d?", stripped):
            return match.group(0)
        marker = '"[REDACTED]"' if structured else "[REDACTED]"
        return match.group("prefix") + marker + value[len(stripped):]

    content = (CONFIG_VALUE_RE if structured else LINE_CONFIG_VALUE_RE).sub(replace, content)
    if suffix in {".yaml", ".yml"}:
        lines = content.splitlines(keepends=True)
        block_indent = None
        for index, line in enumerate(lines):
            indent = len(line) - len(line.lstrip(" \t"))
            if block_indent is not None and line.strip():
                if indent > block_indent:
                    lines[index] = line[:indent] + _mask_lines(line[indent:])
                    continue
                block_indent = None
            block = re.match(r"^[ \t]*(" + KEY_PATTERN + r"):[ \t]*[|>][+-]?\d?[ \t]*(?:#.*)?(?:\r?\n|$)", line)
            if block and _secret_name(block.group(1)):
                block_indent = indent
        content = "".join(lines)
    return content


def _redact_source(content: str, path: str) -> str:
    """Mask literal bodies bound to sensitive names; never consume code expressions."""
    language = EXTENSION_LANGUAGE.get(PurePosixPath(path).suffix.lower())
    try:
        from tree_sitter_language_pack import get_parser
        encoded = content.encode("utf-8")
        tree = get_parser(language).parse(encoded) if language and language not in {"markdown", "rst"} else None
    except Exception:
        tree = None
    if tree is None:
        # Conservative fallback when no grammar exists: quoted literals only.
        def replace(match):
            if not _secret_name(match.group("key")):
                return match.group(0)
            value = match.group("value")
            literal = STRING_BODY_RE.fullmatch(value)
            if not literal:
                return match.group(0)
            start, end = literal.span("body")
            masked = value[:start] + _mask_lines(value[start:end]) + value[end:]
            return match.group(0)[:match.start("value") - match.start()] + masked
        return SOURCE_LITERAL_RE.sub(replace, content)

    spans = {}
    literals = {"string", "string_literal", "interpreted_string_literal", "raw_string_literal", "template_string", "attribute_value", "AttValue"}
    skip_values = {"call", "call_expression", "function_call", "function_call_expression", "dictionary", "object", "array", "list", "map_literal"}

    def literal_spans(node):
        values = [node]
        while values:
            current = values.pop()
            if current.type in skip_values:
                continue
            if current.type in literals:
                # Interpolation is executable syntax, not a credential literal.
                if any(child.type in {"interpolation", "template_substitution"} for child in current.named_children):
                    continue
                raw = encoded[current.start_byte:current.end_byte].decode("utf-8")
                literal = STRING_BODY_RE.fullmatch(raw)
                if literal:
                    start, end = literal.span("body")
                    begin = current.start_byte + len(raw[:start].encode("utf-8"))
                    finish = current.start_byte + len(raw[:end].encode("utf-8"))
                    spans[(begin, finish)] = _mask_lines(raw[start:end]).encode("utf-8")
                continue
            values.extend(current.named_children)

    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        key = next((node.child_by_field_name(field) for field in ("name", "left", "key", "pattern", "declarator") if node.child_by_field_name(field)), None)
        value = node.child_by_field_name("value") or node.child_by_field_name("right")
        if node.type == "attribute" and len(node.named_children) == 2:
            key, value = node.named_children
        if node.type not in {"type_alias_declaration", "type_definition", "type_spec", "type_declaration"} and key and value and _secret_name(encoded[key.start_byte:key.end_byte].decode("utf-8")):
            literal_spans(value)
        pending.extend(node.named_children)
    pieces, cursor = [], 0
    for (start, end), replacement in sorted(spans.items()):
        pieces.extend((encoded[cursor:start], replacement))
        cursor = end
    pieces.append(encoded[cursor:])
    return b"".join(pieces).decode("utf-8")

def _line_chunks(path: str, content: str, language: str | None, start_line: int = 1, symbol: str | None = None, kind: str = "text") -> list[IndexedChunk]:
    lines, chunks = content.splitlines(), []
    start = 0
    while start < len(lines):
        end, length = start, 0
        while end < min(start + 80, len(lines)) and length + len(lines[end]) + 1 <= 6000:
            length += len(lines[end]) + 1
            end += 1
        if end == start:
            end = start + 1
        piece = "\n".join(lines[start:end])[:6000]
        if piece.strip():
            chunks.append(IndexedChunk(path, start_line + start, start_line + end - 1, piece, language, symbol, kind))
        start = end
    return chunks

def _parse_code(path: str, content: str, language: str | None) -> tuple[list[IndexedChunk], list[SymbolOccurrence]]:
    if language in {"markdown", "rst"}:
        return _parse_markdown(path, content, language), []
    if language in {"json", "jsonc", "yaml", "toml", "ini"}:
        return _parse_structured(path, content, language), []
    if not language:
        return _line_chunks(path, content, language), []
    try:
        from tree_sitter_language_pack import get_parser
        encoded = content.encode("utf-8")
        tree = get_parser(language).parse(encoded)
    except Exception:
        return _line_chunks(path, content, language), []
    lines, candidates, symbols, covered = content.splitlines(), [], [], set()

    def node_text(node):
        return encoded[node.start_byte:node.end_byte].decode("utf-8", "ignore")

    def visit(node, parents=()):
        if node.type in DEFINITION_NODES and not (node.type == "module" and language != "ruby"):
            name_node = node.child_by_field_name("name")
            name = node_text(name_node) if name_node else node.type
            current = (*parents, name)
            start = node.start_point[0]
            symbols.append(SymbolOccurrence(name, path, start + 1, ".".join(current), True))
            nested = []
            def find_nested(child):
                for descendant in child.children:
                    if descendant.type in DEFINITION_NODES:
                        nested.append(descendant)
                    else:
                        find_nested(descendant)
            find_nested(node)
            end = node.end_point[0] + (1 if node.end_point[1] else 0)
            if nested:
                for child in nested:
                    visit(child, current)
                # Preserve a class/module header as its own small, symbol-aware chunk.
                for line in range(start, min(end, start + 2, len(lines))):
                    if line not in covered:
                        candidates.append((line, line + 1, ".".join(current), node.type))
                        covered.add(line)
            else:
                if end > start:
                    candidates.append((start, min(end, len(lines)), ".".join(current), node.type))
                    covered.update(range(start, min(end, len(lines))))
            return
        for child in node.children:
            visit(child, parents)

    def collect_identifiers(node, scope=None):
        for child in node.children:
            child_scope = scope
            if child.type in DEFINITION_NODES:
                name_node = child.child_by_field_name("name")
                if name_node:
                    child_scope = node_text(name_node)
            if child.type in {"identifier", "type_identifier", "property_identifier", "field_identifier", "constant"}:
                name = node_text(child)
                if name and len(name) <= 255:
                    symbols.append(SymbolOccurrence(name, path, child.start_point[0] + 1, child_scope, False))
            collect_identifiers(child, child_scope)
    try:
        visit(tree.root_node)
        collect_identifiers(tree.root_node)
    except RecursionError:
        return _line_chunks(path, content, language), []

    chunks = []
    for start, end, symbol, kind in sorted(candidates):
        body = "\n".join(lines[start:end])
        chunks.extend(_line_chunks(path, body, language, start + 1, symbol, kind) if len(body) > 6000 or end - start > 160 else [IndexedChunk(path, start + 1, end, body, language, symbol, kind)])
    # Gaps retain imports, comments, top-level expressions and malformed syntax.
    gap_start = None
    for line in range(len(lines) + 1):
        gap = line < len(lines) and line not in covered
        if gap and gap_start is None:
            gap_start = line
        elif not gap and gap_start is not None:
            chunks.extend(_line_chunks(path, "\n".join(lines[gap_start:line]), language, gap_start + 1, None, "module"))
            gap_start = None
    return sorted(chunks, key=lambda chunk: (chunk.line_start, chunk.line_end)), symbols

def _parse_markdown(path: str, content: str, language: str) -> list[IndexedChunk]:
    lines = content.splitlines()
    sections, current, start = [], "Document", 0
    for index, line in enumerate(lines):
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            if index > start:
                sections.append((current, start, index))
            current, start = heading.group(2).strip()[:240], index
    if lines and start < len(lines):
        sections.append((current, start, len(lines)))
    chunks = []
    for heading, begin, end in sections:
        chunks.extend(_line_chunks(path, "\n".join(lines[begin:end]), language, begin + 1, heading, "section"))
    return chunks or _line_chunks(path, content, language)

def _parse_structured(path: str, content: str, language: str) -> list[IndexedChunk]:
    lines = content.splitlines()
    groups, current, start = [], "root", 0
    for index, line in enumerate(lines):
        if language == "yaml":
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*.*$", line)
            key = match.group(1) if match else None
        elif language in {"toml", "ini"}:
            match = re.match(r"^\s*(\[[^\]]+\]|[A-Za-z0-9_.-]+)\s*(?:=\s*.*)?$", line) if language == "toml" else re.match(r"^\s*(\[[^\]]+\])\s*$", line)
            key = match.group(1) if match else None
        else:
            match = re.match(r'^\s{0,2}"([^"\\]+)"\s*:\s*', line)
            key = match.group(1) if match and index > 0 else None
        if key:
            if index > start:
                groups.append((current, start, index))
            current, start = key[:240], index
    if lines and start < len(lines):
        groups.append((current, start, len(lines)))
    chunks = []
    for key, begin, end in groups:
        chunks.extend(_line_chunks(path, "\n".join(lines[begin:end]), language, begin + 1, key, "config_section"))
    return chunks or _line_chunks(path, content, language)

def parse_repository(data: bytes) -> ParsedRepository:
    chunks, files, symbols = [], [], []
    omitted, omitted_samples = Counter(), []
    total_text = total_expanded = file_count = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
        if len(infos) > settings.archive_entries:
            raise AppError("repository_too_large", "Repository has too many archive entries.", 422)
        root_name = None
        for info in infos:
            name = info.filename
            if name.startswith("/") or "\\" in name or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/")):
                raise AppError("unsafe_archive", "Repository archive contains unsafe paths.", 422)
            parts = name.rstrip("/").split("/")
            if root_name is None:
                root_name = parts[0]
            elif parts[0] != root_name:
                raise AppError("unsafe_archive", "Repository archive contains multiple roots.", 422)
            if len(parts) < 2 or info.is_dir():
                continue
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                omitted["symlink"] += 1
                if len(omitted_samples) < 100: omitted_samples.append({"path": "/".join(parts[1:]), "reason": "symlink"})
                continue
            path = "/".join(parts[1:])
            total_expanded += info.file_size
            if total_expanded > settings.max_text_bytes * 10:
                raise AppError("repository_too_large", "Repository archive expands beyond the limit.", 422)
            if not _eligible(path):
                omitted["dependency_or_build"] += 1
                if len(omitted_samples) < 100: omitted_samples.append({"path": path, "reason": "dependency_or_build"})
                continue
            if info.file_size > settings.max_file_bytes:
                omitted["file_too_large"] += 1
                if len(omitted_samples) < 100: omitted_samples.append({"path": path, "reason": "file_too_large"})
                continue
            file_count += 1
            if file_count > settings.max_files:
                raise AppError("repository_too_large", "Repository has too many eligible files.", 422)
            with archive.open(info) as source:
                raw = source.read(settings.max_file_bytes + 1)
            if len(raw) > settings.max_file_bytes or b"\0" in raw:
                omitted["binary_or_oversized"] += 1
                if len(omitted_samples) < 100: omitted_samples.append({"path": path, "reason": "binary_or_oversized"})
                continue
            try:
                original = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                omitted["non_utf8"] += 1
                if len(omitted_samples) < 100: omitted_samples.append({"path": path, "reason": "non_utf8"})
                continue
            total_text += len(raw)
            if total_text > settings.max_text_bytes:
                raise AppError("repository_too_large", "Repository has too much source text.", 422)
            content = _redact(original, path)
            if not content.splitlines():
                continue
            language = EXTENSION_LANGUAGE.get(PurePosixPath(path).suffix.lower())
            file_chunks, file_symbols = _parse_code(path, content, language)
            symbol_names = list(dict.fromkeys(s.name for s in file_symbols if s.is_definition))
            line_count = len(content.splitlines())
            summary = f"{language or 'text'} file ({line_count} lines)" + (f"; declarations: {', '.join(symbol_names[:40])}" if symbol_names else "")
            files.append(IndexedFile(path, content, language, summary, line_count, len(raw)))
            chunks.extend(file_chunks)
            remaining_symbols = max(0, settings.max_symbol_references - len(symbols))
            if len(file_symbols) > remaining_symbols:
                omitted["symbol_reference_limit"] += len(file_symbols) - remaining_symbols
                file_symbols = file_symbols[:remaining_symbols]
            symbols.extend(file_symbols)
            if len(chunks) > settings.max_chunks_per_snapshot:
                raise AppError("repository_too_large", "Repository has too many index chunks.", 422)
        if not chunks:
            raise AppError("empty_repository", "No supported source text was found.", 422)
        return ParsedRepository(files, chunks, symbols, {"counts": dict(omitted), "samples": omitted_samples})
    except (zipfile.BadZipFile, RuntimeError):
        raise AppError("invalid_archive", "GitHub returned an unreadable archive.", 502)

def parse_archive(data: bytes) -> list[IndexedChunk]:
    return parse_repository(data).chunks
