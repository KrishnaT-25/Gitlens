import io
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit
import httpx
from app.config import settings
from app.errors import AppError
from app.adapters.github import _headers, _check

SKIP_DIRS = {".git", ".next", "build", "coverage", "dist", "node_modules", "target", "vendor", "venv", ".venv", "__pycache__"}
SKIP_SUFFIXES = {".bin", ".class", ".dll", ".exe", ".gif", ".ico", ".jar", ".jpeg", ".jpg", ".lock", ".map", ".min.js", ".pdf", ".png", ".pyc", ".svg", ".woff", ".woff2", ".zip"}
SECRET_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9_]{30,}\b|\bAIza[A-Za-z0-9_-]{30,}\b")

@dataclass(frozen=True)
class IndexedChunk:
    path: str
    line_start: int
    line_end: int
    text: str

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
    if any(p == ".env" or p.startswith(".env.") or p.endswith((".pem", ".key")) for p in parts):
        return False
    return True

def parse_archive(data: bytes) -> list[IndexedChunk]:
    chunks: list[IndexedChunk] = []
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
            # ZIPs from GitHub include one generated top-level directory.
            parts = name.rstrip("/").split("/")
            if root_name is None:
                root_name = parts[0]
            elif parts[0] != root_name:
                raise AppError("unsafe_archive", "Repository archive contains multiple roots.", 422)
            if len(parts) < 2 or info.is_dir():
                continue
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                continue
            path = "/".join(parts[1:])
            total_expanded += info.file_size
            if total_expanded > settings.max_text_bytes * 10:
                raise AppError("repository_too_large", "Repository archive expands beyond the limit.", 422)
            if not _eligible(path) or info.file_size > settings.max_file_bytes:
                continue
            file_count += 1
            if file_count > settings.max_files:
                raise AppError("repository_too_large", "Repository has too many eligible files.", 422)
            with archive.open(info) as source:
                raw = source.read(settings.max_file_bytes + 1)
            if len(raw) > settings.max_file_bytes:
                continue
            if b"\0" in raw:
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if SECRET_RE.search(content):
                continue
            total_text += len(raw)
            if total_text > settings.max_text_bytes:
                raise AppError("repository_too_large", "Repository has too much source text.", 422)
            lines = content.splitlines()
            if not lines:
                continue
            start = 0
            while start < len(lines):
                end = start
                length = 0
                while end < min(start + 80, len(lines)) and length + len(lines[end]) + 1 <= 6000:
                    length += len(lines[end]) + 1
                    end += 1
                if end == start:
                    end = start + 1
                piece = "\n".join(lines[start:end])[:6000]
                if piece.strip():
                    if len(chunks) >= settings.max_chunks_per_snapshot:
                        raise AppError("repository_too_large", "Repository has too many index chunks.", 422)
                    chunks.append(IndexedChunk(path, start + 1, end, piece))
                if end == len(lines):
                    break
                start = max(start + 1, end - 10)
        if not chunks:
            raise AppError("empty_repository", "No supported source text was found.", 422)
        return chunks
    except (zipfile.BadZipFile, RuntimeError):
        raise AppError("invalid_archive", "GitHub returned an unreadable archive.", 502)
