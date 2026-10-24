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
SECRET_VALUE_RE = re.compile(r"(?i)(\b[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|auth(?:orization)?[_-]?(?:token)?|client[_-]?(?:secret|id)|password|passwd|secret(?:[_-]?(?:key|token))?|token|private[_-]?key|credential|database[_-]?url|db[_-]?url|connection[_-]?string|jwt[_-]?secret|signing[_-]?key)[A-Za-z0-9_.-]*\b\s*[:=]\s*)(?:([\"'])([^\"'\r\n]*)\2|([^\r\n,;}]+))")
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
    if path.lower().endswith((".pem", ".key", ".p12", ".pfx")):
        return "\n".join("[REDACTED SENSITIVE MATERIAL]" if line.strip() else "" for line in content.splitlines())
    content = PRIVATE_KEY_RE.sub(lambda match: "\n".join("[REDACTED PRIVATE KEY]" if line.strip() else "" for line in match.group(0).splitlines()), content)
    content = re.sub(r"(?m)^.*-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*$", "[REDACTED PRIVATE KEY]", content)
    content = re.sub(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)", r"\1[REDACTED]\2", content)
    content = AUTH_HEADER_RE.sub(r"\1[REDACTED]", content)
    content = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2) or ''}[REDACTED]{match.group(2) or ''}", content)
    return TOKEN_RE.sub("[REDACTED TOKEN]", content)

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
            summary = f"{language or 'text'} file ({content.count(chr(10)) + 1} lines)" + (f"; declarations: {', '.join(symbol_names[:40])}" if symbol_names else "")
            files.append(IndexedFile(path, content, language, summary, content.count("\n") + 1, len(raw)))
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
