"""Run a top-k experiment matrix and write one aggregate comparison CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import load_config, select_snapshot
from eval.run_ir_eval import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "eval/config.yaml")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--testset", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    experiments = config.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        parser.error("YAML must define a non-empty experiments list")
    snapshot_id = select_snapshot(config, args.snapshot_id)
    testset = args.testset or ROOT / config.get("testset_path", "eval/ir_testset.jsonl")
    output_dir = args.output_dir or ROOT / config.get("results_dir", "eval/results")
    comparisons = []
    for experiment in experiments:
        if not isinstance(experiment, dict) or not experiment.get("name"):
            raise ValueError("Each experiment needs a name and top_k")
        merged = {**config, **experiment}
        top_k = int(experiment.get("top_k", 10))
        if not 1 <= top_k <= 100:
            raise ValueError(f"Invalid top_k for experiment {experiment['name']!r}")
        result = run(merged, snapshot_id, testset, output_dir, top_k,
                     run_name="".join(c if c.isalnum() or c in "-_" else "-" for c in experiment["name"]))
        comparisons.append({"name": experiment["name"], "top_k": top_k,
            "chunk_size_lines": merged.get("chunk_size_lines", 80),
            "chunk_overlap_lines": merged.get("chunk_overlap_lines", 0),
            "embedding_model": merged.get("embedding_model", "gemini-embedding-2"),
            **result["metrics"]})
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    table_path = output_dir / f"{stamp}_comparison.csv"
    fields = list(comparisons[0])
    with table_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(comparisons)
    print("\nExperiment comparison")
    print(f"{'name':<14} {'top_k':>5} {'hit@10':>8} {'recall@10':>10} {'precision@10':>13} {'mrr':>8} {'ndcg@10':>9}")
    print("-" * 75)
    for row in comparisons:
        print(f"{row['name']:<14} {row['top_k']:>5} {row['hit_rate@10']:>8.4f} "
              f"{row['recall@10']:>10.4f} {row['precision@10']:>13.4f} "
              f"{row['mrr']:>8.4f} {row['ndcg@10']:>9.4f}")
    print(f"\nComparison table: {table_path}")


if __name__ == "__main__":
    main()
