"""Shared configuration, snapshot, and JSONL helpers for evaluation scripts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.models import Chunk, Snapshot  # noqa: E402
from app.config import settings  # noqa: E402
from eval.retriever import snapshot_embedding_models  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def load_config(path: Path) -> dict[str, Any]:
    """Read YAML settings and resolve repository-relative paths."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Evaluation YAML must contain a mapping")
    return data


def select_snapshot(config: dict[str, Any], override: str | None = None) -> str:
    """Resolve the immutable evaluation snapshot from CLI, YAML, or environment."""
    snapshot_id = override or config.get("snapshot_id") or os.environ.get("GITLENS_EVAL_SNAPSHOT_ID")
    if not snapshot_id:
        raise ValueError("Set snapshot_id in YAML, pass --snapshot-id, or set GITLENS_EVAL_SNAPSHOT_ID")
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, snapshot_id)
        if snapshot is None or snapshot.status != "ready":
            raise ValueError(f"Snapshot {snapshot_id!r} is missing or not ready")
    return snapshot_id


def validate_profile(config: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    """Reject indexing settings that cannot describe the selected stored snapshot."""
    chunk_size = int(config.get("chunk_size_lines", 80))
    overlap = int(config.get("chunk_overlap_lines", 0))
    model = str(config.get("embedding_model", "gemini-embedding-2"))
    # These are the current fallback chunking defaults; AST-aware chunks may be larger.
    if chunk_size != 80 or overlap != 0:
        raise ValueError("Chunk-size/overlap experiments require a reindexed corpus; this evaluator does not rewrite the production index")
    stored_models = snapshot_embedding_models(snapshot_id)
    if stored_models and stored_models != [model]:
        raise ValueError(f"Snapshot vectors use {stored_models}, not configured embedding_model={model!r}; reindex before comparing models")
    if not stored_models and model != settings.gemini_embedding_model:
        raise ValueError(f"No vectors are stored for this snapshot; configured embedding_model must match server setting {settings.gemini_embedding_model!r}")
    return {"chunk_size_lines": chunk_size, "chunk_overlap_lines": overlap,
            "embedding_model": model, "stored_embedding_models": stored_models,
            "semantic_vectors": bool(stored_models)}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load and validate question records from a JSON Lines test set."""
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on {path}:{line_no}") from exc
        if not isinstance(record.get("question"), str) or not record["question"].strip():
            raise ValueError(f"Missing question on {path}:{line_no}")
        relevant = record.get("relevant_chunk_ids")
        if not isinstance(relevant, list) or not relevant or not all(isinstance(item, str) for item in relevant):
            raise ValueError(f"relevant_chunk_ids must be a non-empty string list on {path}:{line_no}")
        if not isinstance(record.get("reference_answer"), str):
            raise ValueError(f"Missing reference_answer on {path}:{line_no}")
        rows.append(record)
    if not rows:
        raise ValueError(f"No evaluation questions found in {path}")
    return rows


def chunk_catalog(snapshot_id: str, chunk_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Resolve expected and retrieved IDs to source locations for failure analysis."""
    if not chunk_ids:
        return {}
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, snapshot_id)
        rows = db.execute(select(Chunk).where(Chunk.snapshot_id == snapshot_id,
            Chunk.index_version == snapshot.index_version, Chunk.id.in_(chunk_ids))).scalars()
        return {row.id: {"path": row.path, "line_start": row.line_start,
                         "line_end": row.line_end, "text": row.text} for row in rows}
