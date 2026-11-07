import json
from dataclasses import dataclass
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import settings
from app.errors import AppError
from app.deadlines import check_deadline
from app.models import Chunk, Snapshot, SnapshotFile, SnapshotNode, SymbolReference
from app.services.retrieval import hybrid_search

TOOL_DECLARATIONS = [
    {"name": "search", "description": "Search indexed evidence in this snapshot. Optionally restrict to a file or directory path and a literal, case-insensitive symbol substring.", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}, "path": {"type": "STRING"}, "symbol": {"type": "STRING"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read a bounded line range from an indexed file in this snapshot.", "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}, "start_line": {"type": "INTEGER"}, "end_line": {"type": "INTEGER"}}, "required": ["path"]}},
    {"name": "list_dir", "description": "List indexed immediate children of a repository directory.", "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}}},
    {"name": "find_references", "description": "Find indexed definitions and references to an exact symbol name.", "parameters": {"type": "OBJECT", "properties": {"symbol": {"type": "STRING"}}, "required": ["symbol"]}},
    {"name": "final_answer", "description": "Finish with a grounded answer and IDs of supporting chunks.", "parameters": {"type": "OBJECT", "properties": {"answer": {"type": "STRING"}, "citedChunkIds": {"type": "ARRAY", "items": {"type": "STRING"}}, "insufficientEvidence": {"type": "BOOLEAN"}}, "required": ["answer", "citedChunkIds", "insufficientEvidence"]}},
]

def safe_path(value: str | None) -> str:
    if value is not None and not isinstance(value, str):
        raise AppError("invalid_tool_path", "Repository path is invalid.", 400)
    path = (value or "").replace("\\", "/").strip(" /")
    if path and (any(part in {".", ".."} for part in path.split("/")) or "\x00" in path):
        raise AppError("invalid_tool_path", "Repository path is invalid.", 400)
    return path

@dataclass(frozen=True)
class Excerpt:
    id: str
    path: str
    line_start: int
    line_end: int
    text: str

    def record(self) -> dict:
        return {"chunkId": self.id, "path": self.path, "lineStart": self.line_start,
                "lineEnd": self.line_end, "text": self.text}


def _bounded(payload: dict, max_chars: int | None = None) -> dict:
    budget = settings.max_tool_output_chars if max_chars is None else max_chars
    if len(json.dumps(payload, ensure_ascii=False)) <= budget:
        return payload
    for key, entries in payload.items():
        if not isinstance(entries, list):
            continue
        result = {**payload, key: [], "truncated": True}
        used = len(json.dumps(result, ensure_ascii=False))
        if used > budget:
            break
        for entry in entries:
            cost = len(json.dumps(entry, ensure_ascii=False)) + (2 if result[key] else 0)
            if used + cost <= budget:
                result[key].append(entry)
                used += cost
        return result
    return {"truncated": True} if budget >= 19 else {}


def _excerpt(chunk: Chunk, start: int, end: int, source: str) -> Excerpt:
    full_chunk = (start, end, source) == (chunk.line_start, chunk.line_end, chunk.text)
    excerpt_id = chunk.id if full_chunk else f"{chunk.id}:L{start}-L{end}"
    return Excerpt(excerpt_id, chunk.path, start, end, source)


def _deliver(key: str, rows: list[tuple[Excerpt, dict]], evidence: dict[str, Excerpt],
             budget: int | None) -> dict:
    output = _bounded({key: [{**excerpt.record(), **metadata} for excerpt, metadata in rows]}, budget)
    delivered = {item["chunkId"] for item in output.get(key, [])}
    evidence.update((excerpt.id, excerpt) for excerpt, _ in rows if excerpt.id in delivered)
    return output

def _line_window(content: str, start_value, end_value, max_lines: int) -> tuple[list[str], int, int]:
    lines = content.splitlines()
    if not lines:
        return lines, 0, 0
    try:
        start = 1 if start_value is None else int(start_value)
        end = start + max_lines - 1 if end_value is None else int(end_value)
        if not 1 <= start <= len(lines) or end < start:
            raise ValueError("Range is outside the file")
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppError("invalid_tool_range", "Repository line range is invalid.", 400) from exc
    end = min(end, len(lines), start + max_lines - 1)
    return lines, start, end

def execute_tool(db: Session, snapshot: Snapshot, name: str, args: dict, evidence: dict[str, Excerpt], *,
                 max_chars: int | None = None) -> dict:
    check_deadline()
    version = snapshot.index_version
    if name == "search":
        query = str(args.get("query", "")).strip()[:settings.max_question_chars]
        path = safe_path(args.get("path"))
        symbol = str(args.get("symbol") or "").strip()[:255]
        if "\x00" in symbol:
            raise AppError("invalid_tool_symbol", "Repository symbol is invalid.", 400)
        if not query:
            return {"results": []}
        chunks = hybrid_search(db, snapshot.id, query, 8, path=path, symbol=symbol)
        return _deliver("results", [
            (_excerpt(c, c.line_start, c.line_end, c.text), {"symbol": c.symbol_path}) for c in chunks
        ], evidence, max_chars)
    if name == "read_file":
        path = safe_path(args.get("path"))
        if not path:
            return {"error": "path_required"}
        file = db.execute(select(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot.id,
            SnapshotFile.index_version == version, SnapshotFile.path == path)).scalar_one_or_none()
        if not file:
            return {"error": "file_not_indexed", "path": path}
        lines, start, end = _line_window(file.content, args.get("start_line"), args.get("end_line"), settings.max_tool_file_lines)
        if not lines:
            return {"evidence": []}
        excerpts = db.execute(select(Chunk).where(Chunk.snapshot_id == snapshot.id,
            Chunk.index_version == version, Chunk.path == path, Chunk.line_start <= end, Chunk.line_end >= start)
            .order_by(Chunk.line_start)).scalars().all()
        rows = []
        for chunk in excerpts:
            lo, hi = max(start, chunk.line_start), min(end, chunk.line_end)
            rows.append((_excerpt(chunk, lo, hi, "\n".join(lines[lo - 1:hi])), {}))
        return _deliver("evidence", rows, evidence, max_chars)
    if name == "list_dir":
        path = safe_path(args.get("path"))
        rows = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot.id,
            SnapshotNode.index_version == version, SnapshotNode.parent_path == path)
            .order_by(SnapshotNode.node_type, SnapshotNode.path).limit(100)).scalars().all()
        return _bounded({"path": path, "children": [{"name": n.path.rsplit("/", 1)[-1], "path": n.path,
            "type": n.node_type, "summary": n.summary[:500]} for n in rows]}, max_chars)
    if name == "find_references":
        symbol = str(args.get("symbol", "")).strip()[:255]
        if not symbol:
            return {"references": []}
        rows = db.execute(select(SymbolReference, Chunk).join(Chunk, Chunk.id == SymbolReference.chunk_id)
            .where(SymbolReference.snapshot_id == snapshot.id, SymbolReference.index_version == version,
                   Chunk.snapshot_id == snapshot.id,
                   func.lower(SymbolReference.name) == symbol.lower(), Chunk.index_version == version)
            .order_by(SymbolReference.is_definition.desc(), SymbolReference.path, SymbolReference.line).limit(100)).all()
        excerpts = []
        for ref, chunk in rows:
            lines = chunk.text.splitlines()
            offset = ref.line - chunk.line_start
            if len(lines) == chunk.line_end - chunk.line_start + 1 and 0 <= offset < len(lines):
                excerpts.append((_excerpt(chunk, ref.line, ref.line, lines[offset]), {"definition": ref.is_definition}))
        return _deliver("references", excerpts, evidence, max_chars)
    raise AppError("unknown_tool", "The requested repository tool is unavailable.", 400)

