"""Evaluation adapter for the production snapshot-scoped hybrid retriever."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.db import SessionLocal  # noqa: E402
from app.models import Chunk, ChunkEmbedding, Snapshot  # noqa: E402
from app.services.retrieval import hybrid_search_scored  # noqa: E402
from sqlalchemy import select  # noqa: E402


class RetrievedChunk(TypedDict):
    """Minimal, serializable chunk record returned by ``retrieve``."""

    id: str
    score: float
    path: str
    line_start: int
    line_end: int
    text: str


def retrieve(query: str, k: int, snapshot_id: str | None = None) -> list[RetrievedChunk]:
    """Return the top ``k`` chunks for one ready snapshot, with stable IDs and rank scores.

    Select the corpus with ``snapshot_id`` or ``GITLENS_EVAL_SNAPSHOT_ID``. Scores
    are the production retriever's RRF plus exact path/symbol/line rerank score;
    exact file lookups use descending reciprocal ranks.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= k <= 100:
        raise ValueError("k must be between 1 and 100")
    selected_snapshot = snapshot_id or os.environ.get("GITLENS_EVAL_SNAPSHOT_ID")
    if not selected_snapshot:
        raise ValueError("Set GITLENS_EVAL_SNAPSHOT_ID or pass snapshot_id")

    with SessionLocal() as db:
        snapshot = db.get(Snapshot, selected_snapshot)
        if snapshot is None or snapshot.status != "ready":
            raise ValueError(f"Snapshot {selected_snapshot!r} is missing or not ready")
        ranked = hybrid_search_scored(db, snapshot.id, query, k)
        return [{
            "id": chunk.id,
            "score": float(score),
            "path": chunk.path,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "text": chunk.text,
        } for chunk, score in ranked]


def snapshot_embedding_models(snapshot_id: str) -> list[str]:
    """Return the distinct embedding models stored for the active snapshot index."""
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, snapshot_id)
        if snapshot is None:
            raise ValueError(f"Snapshot {snapshot_id!r} does not exist")
        return sorted(set(db.execute(
            select(ChunkEmbedding.model).join(Chunk, ChunkEmbedding.chunk_id == Chunk.id).where(
                Chunk.snapshot_id == snapshot.id,
                Chunk.index_version == snapshot.index_version,
            )
        ).scalars()))
