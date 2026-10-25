"""Small, dependency-free metrics for ranked chunk retrieval."""

from math import log2
from typing import Iterable, Sequence


def metrics_at_k(retrieved_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> dict[str, float]:
    """Calculate binary relevance metrics for one ranked result list at ``k``."""
    if k < 1:
        raise ValueError("k must be positive")
    relevant = set(relevant_ids)
    ranked = list(retrieved_ids[:k])
    hits = [chunk_id in relevant for chunk_id in ranked]
    hit_count = sum(hits)
    first_rank = next((rank for rank, hit in enumerate(hits, 1) if hit), None)
    ideal_hits = min(len(relevant), k)
    dcg = sum(1 / log2(rank + 1) for rank, hit in enumerate(hits, 1) if hit)
    ideal_dcg = sum(1 / log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return {
        "hit_rate": float(hit_count > 0),
        "recall": hit_count / len(relevant) if relevant else 0.0,
        "precision": hit_count / k,
        "mrr": 1 / first_rank if first_rank else 0.0,
        "ndcg": dcg / ideal_dcg if ideal_dcg else 0.0,
    }


def file_hit_rate_at_k(retrieved_paths: Sequence[str], relevant_paths: Iterable[str], k: int) -> float:
    """Return 1 when any of the expected source files appears in the top ``k`` chunks."""
    if k < 1:
        raise ValueError("k must be positive")
    relevant = set(relevant_paths)
    return float(bool(relevant.intersection(retrieved_paths[:k])))


def summarize(rows: Sequence[dict], cutoffs: Sequence[int] = (1, 3, 5, 10)) -> dict[str, float]:
    """Macro-average per-question metrics and report MRR over each retrieved list."""
    if not rows:
        raise ValueError("Cannot summarize an empty evaluation set")
    summary = {}
    for k in cutoffs:
        for metric in ("hit_rate", "recall", "precision", "ndcg"):
            key = f"{metric}@{k}"
            summary[key] = sum(row["metrics"][str(k)][metric] for row in rows) / len(rows)
        key = f"file_hit_rate@{k}"
        summary[key] = sum(row["file_hit_rate"][str(k)] for row in rows) / len(rows)
    summary["mrr"] = sum(row["mrr"] for row in rows) / len(rows)
    return summary
