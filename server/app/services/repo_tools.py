import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.errors import AppError
from app.models import Chunk, Snapshot, SnapshotFile, SnapshotNode, SymbolReference
from app.services.retrieval import hybrid_search

TOOL_DECLARATIONS = [
    {"name": "search", "description": "Search indexed evidence in this exact repository snapshot.", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}, "path": {"type": "STRING"}, "symbol": {"type": "STRING"}}, "required": ["query"]}},
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

def _bounded(payload: dict) -> dict:
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > settings.max_tool_output_chars:
        size = settings.max_tool_output_chars - 32
        while size > 0:
            result = {"truncated": True, "output": encoded[:size]}
            if len(json.dumps(result, ensure_ascii=False)) <= settings.max_tool_output_chars:
                return result
            size //= 2
        return {"truncated": True}
    return payload

def _line_window(content: str, start_value, end_value, max_lines: int) -> tuple[list[str], int, int]:
    lines = content.splitlines()
    if not lines:
        return lines, 0, 0
    try:
        start = min(len(lines), max(1, int(start_value or 1)))
        end = min(len(lines), max(start, int(end_value or start + max_lines - 1)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppError("invalid_tool_range", "Repository line range is invalid.", 400) from exc
    end = min(end, start + max_lines - 1)
    return lines, start, end

def execute_tool(db: Session, snapshot: Snapshot, name: str, args: dict, evidence: dict[str, Chunk]) -> dict:
    version = snapshot.index_version
    if name == "search":
        query = str(args.get("query", "")).strip()[:settings.max_question_chars]
        path = safe_path(args.get("path"))
        symbol = str(args.get("symbol", "")).strip()[:255]
        if not query:
            return {"results": []}
        chunks = hybrid_search(db, snapshot.id, query, 8)
        if path:
            chunks = [c for c in chunks if c.path == path or c.path.startswith(path + "/")]
        if symbol:
            chunks = [c for c in chunks if c.symbol_path and symbol.lower() in c.symbol_path.lower()]
        for chunk in chunks:
            evidence[chunk.id] = chunk
        return _bounded({"results": [{"chunkId": c.id, "path": c.path, "lineStart": c.line_start,
                                      "lineEnd": c.line_end, "symbol": c.symbol_path, "text": c.text}
                                     for c in chunks]})
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
            return {"path": path, "lineStart": 0, "lineEnd": 0, "text": "", "evidence": []}
        excerpts = db.execute(select(Chunk).where(Chunk.snapshot_id == snapshot.id,
            Chunk.index_version == version, Chunk.path == path, Chunk.line_start <= end, Chunk.line_end >= start)
            .order_by(Chunk.line_start)).scalars().all()
        for chunk in excerpts:
            evidence[chunk.id] = chunk
        return _bounded({"path": path, "lineStart": start, "lineEnd": end,
                         "text": "\n".join(f"{n}: {lines[n-1]}" for n in range(start, end + 1)),
                         "evidence": [{"chunkId": c.id, "lineStart": c.line_start, "lineEnd": c.line_end} for c in excerpts]})
    if name == "list_dir":
        path = safe_path(args.get("path"))
        rows = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot.id,
            SnapshotNode.index_version == version, SnapshotNode.parent_path == path)
            .order_by(SnapshotNode.node_type, SnapshotNode.path).limit(100)).scalars().all()
        return _bounded({"path": path, "children": [{"name": n.path.rsplit("/", 1)[-1], "path": n.path,
            "type": n.node_type, "summary": n.summary[:500]} for n in rows]})
    if name == "find_references":
        symbol = str(args.get("symbol", "")).strip()[:255]
        if not symbol:
            return {"references": []}
        rows = db.execute(select(SymbolReference, Chunk).join(Chunk, Chunk.id == SymbolReference.chunk_id)
            .where(SymbolReference.snapshot_id == snapshot.id, SymbolReference.index_version == version,
                   Chunk.snapshot_id == snapshot.id,
                   SymbolReference.name.ilike(symbol), Chunk.index_version == version)
            .order_by(SymbolReference.is_definition.desc(), SymbolReference.path, SymbolReference.line).limit(100)).all()
        for ref, chunk in rows:
            evidence[chunk.id] = chunk
        return _bounded({"references": [{"chunkId": chunk.id, "path": ref.path, "line": ref.line,
            "definition": ref.is_definition, "text": chunk.text[:1500]} for ref, chunk in rows]})
    raise AppError("unknown_tool", "The requested repository tool is unavailable.", 400)

