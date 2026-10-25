"""Evaluate the production retriever against judged JSONL chunk relevance."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import chunk_catalog, load_config, read_jsonl, select_snapshot, validate_profile
from eval.metrics import metrics_at_k, summarize
from eval.retriever import retrieve

CUTOFFS = (1, 3, 5, 10)


def evaluate(testset: list[dict[str, Any]], snapshot_id: str, top_k: int) -> list[dict[str, Any]]:
    """Retrieve for every question and attach per-question relevance metrics."""
    evaluated = []
    for index, item in enumerate(testset, 1):
        retrieved = retrieve(item["question"], top_k, snapshot_id)
        ids = [chunk["id"] for chunk in retrieved]
        relevant = set(item["relevant_chunk_ids"])
        per_k = {str(k): metrics_at_k(ids, relevant, k) for k in CUTOFFS}
        first_rank = next((rank for rank, chunk_id in enumerate(ids, 1) if chunk_id in relevant), None)
        evaluated.append({"question": item["question"], "reference_answer": item["reference_answer"],
            "relevant_chunk_ids": item["relevant_chunk_ids"], "retrieved": retrieved,
            "retrieved_ids": ids, "first_relevant_rank": first_rank,
            "metrics": per_k, "mrr": 1 / first_rank if first_rank else 0.0})
        print(f"[{index}/{len(testset)}] rank={first_rank or 'miss'} {item['question'][:100]}")
    return evaluated


def save_results(rows: list[dict[str, Any]], summary: dict[str, Any], output_dir: Path,
                 run_name: str = "") -> tuple[Path, Path]:
    """Write question-level CSV and aggregate/failure JSON under one timestamp."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}_{run_name}" if run_name else stamp
    csv_path, json_path = output_dir / f"{stem}.csv", output_dir / f"{stem}_summary.json"
    fields = ["question", "relevant_chunk_ids", "retrieved_chunk_ids", "retrieved_scores",
              "first_relevant_rank", "mrr"]
    for k in CUTOFFS:
        fields.extend(f"{metric}@{k}" for metric in ("hit_rate", "recall", "precision", "ndcg"))
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            record = {"question": row["question"], "relevant_chunk_ids": json.dumps(row["relevant_chunk_ids"]),
                "retrieved_chunk_ids": json.dumps(row["retrieved_ids"]),
                "retrieved_scores": json.dumps([chunk["score"] for chunk in row["retrieved"]]),
                "first_relevant_rank": row["first_relevant_rank"] or "", "mrr": row["mrr"]}
            for k in CUTOFFS:
                record.update({f"{metric}@{k}": row["metrics"][str(k)][metric]
                               for metric in ("hit_rate", "recall", "precision", "ndcg")})
            writer.writerow(record)
    worst = sorted(rows, key=lambda row: (row["mrr"], row["metrics"]["10"]["ndcg"],
        row["metrics"]["10"]["recall"], row["question"]))[:10]
    ids = {chunk_id for row in worst for chunk_id in (*row["relevant_chunk_ids"], *row["retrieved_ids"])}
    catalog = chunk_catalog(summary["snapshot_id"], ids)
    failures = [{"question": row["question"], "expected_chunks": [
        {"id": chunk_id, **catalog.get(chunk_id, {})} for chunk_id in row["relevant_chunk_ids"]],
        "retrieved_chunks": [{**chunk, "rank": rank} for rank, chunk in enumerate(row["retrieved"], 1)],
        "first_relevant_rank": row["first_relevant_rank"], "mrr": row["mrr"],
        "ndcg@10": row["metrics"]["10"]["ndcg"], "recall@10": row["metrics"]["10"]["recall"]}
        for row in worst]
    payload = {**summary, "metrics": summarize(rows), "question_count": len(rows),
               "results_csv": str(csv_path), "worst_questions": failures}
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return csv_path, json_path


def print_summary(summary: dict[str, float]) -> None:
    """Print a compact aligned metric table."""
    print("\nIR evaluation summary")
    print(f"{'Metric':<18} {'Score':>8}")
    print("-" * 28)
    for name, value in summary.items():
        print(f"{name:<18} {value:>8.4f}")


def run(config: dict[str, Any], snapshot_id: str, testset_path: Path,
        output_dir: Path, top_k: int, run_name: str = "") -> dict[str, Any]:
    """Run one validated experiment and persist detailed plus aggregate results."""
    profile = validate_profile(config, snapshot_id)
    questions = read_jsonl(testset_path)
    rows = evaluate(questions, snapshot_id, top_k)
    summary = {"snapshot_id": snapshot_id, "top_k": top_k, "index_profile": profile,
               "experiment": run_name or "default"}
    csv_path, json_path = save_results(rows, summary, output_dir, run_name)
    metrics = summarize(rows, CUTOFFS)
    print_summary(metrics)
    print(f"\nPer-question results: {csv_path}\nSummary and worst cases: {json_path}")
    return {**summary, "metrics": metrics, "results_csv": str(csv_path), "summary_json": str(json_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "eval/config.yaml")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--testset", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--top-k", type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    snapshot_id = select_snapshot(config, args.snapshot_id)
    path = args.testset or ROOT / config.get("testset_path", "eval/ir_testset.jsonl")
    output = args.output_dir or ROOT / config.get("results_dir", "eval/results")
    run(config, snapshot_id, path, output, args.top_k or int(config.get("top_k", 10)))


if __name__ == "__main__":
    main()
