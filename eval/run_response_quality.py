"""Run a pinned, multi-repository evidence benchmark on a disposable PostgreSQL DB."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path

from eval.retriever import ROOT
from app.config import settings
from app.db import engine
from app.errors import AppError
from app.models import Chunk, Repository, Snapshot, SnapshotFile, SymbolReference
from app.services.ingest import download_archive, parse_repository
from app.services.jobs import _persist_map
from app.services.rag import answer
from app.services.retrieval import hybrid_search
from sqlalchemy.orm import Session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", action="store_true", help="Also call Gemini and save answers for human review.")
    parser.add_argument("--repository", help="Run only one repository, as owner/name.")
    parser.add_argument("--limit", type=int, help="Limit the number of selected prompts.")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/response_quality.json")
    args = parser.parse_args()
    if not os.getenv("GITLENS_EVAL_DATABASE_URL") or settings.database_url != os.getenv("GITLENS_EVAL_DATABASE_URL"):
        parser.error("Point DATABASE_URL and GITLENS_EVAL_DATABASE_URL at the same disposable, migrated database.")
    if args.answers and not settings.gemini_api_key:
        parser.error("Set GEMINI_API_KEY to run live answer evaluation.")
    cases = [json.loads(line) for line in (ROOT / "eval/response_quality_cases.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.repository:
        cases = [case for case in cases if case["repository"] == args.repository]
    cases = cases[:args.limit]
    if not cases:
        parser.error("No cases selected.")
    groups = {}
    for case in cases:
        groups.setdefault((case["repository"], case["commit"]), []).append(case)
    results = []
    for (name, commit), questions in groups.items():
        owner, repository_name = name.split("/")
        try:
            parsed = parse_repository(download_archive(owner, repository_name, commit))
        except AppError as exc:
            results.extend(dict(case, outcome="index_error", error=exc.code) for case in questions)
            print(f"{name}: index error ({exc.code})", flush=True)
            continue
        print(f"{name}: indexed {len(parsed.chunks)} chunks for {len(questions)} cases", flush=True)
        indexed_paths = {chunk.path for chunk in parsed.chunks}
        with engine.connect() as connection, connection.begin() as transaction:
            with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                repository = Repository(owner=owner, name=repository_name, canonical_url=f"https://github.com/{name}", default_branch="main")
                db.add(repository)
                db.flush()
                snapshot = Snapshot(repository_id=repository.id, sha=commit, status="ready", summary={},
                                    index_version=2, index_report={"keywordSearch": "postgres_fts"})
                db.add(snapshot)
                db.flush()
                chunks_by_path = {}
                for source in parsed.chunks:
                    chunk = Chunk(snapshot_id=snapshot.id, index_version=2, **vars(source))
                    db.add(chunk)
                    chunks_by_path.setdefault(chunk.path, []).append(chunk)
                db.flush()
                if args.answers:
                    db.add_all(SnapshotFile(snapshot_id=snapshot.id, index_version=2, **vars(file)) for file in parsed.files)
                    _persist_map(db, snapshot.id, 2, parsed.files)
                for symbol in parsed.symbols:
                    enclosing = next((chunk for chunk in chunks_by_path.get(symbol.path, ())
                                      if chunk.line_start <= symbol.line <= chunk.line_end), None)
                    if enclosing:
                        db.add(SymbolReference(snapshot_id=snapshot.id, index_version=2, chunk_id=enclosing.id,
                                               path=symbol.path, line=symbol.line, name=symbol.name,
                                               is_definition=symbol.is_definition))
                db.flush()
                for case in questions:
                    expected = set(case["expected_paths"])
                    if expected - indexed_paths:
                        results.append(dict(case, outcome="label_not_indexed", missing=sorted(expected - indexed_paths)))
                        continue
                    chunks = hybrid_search(db, snapshot.id, case["question"], 8)
                    retrieved = {chunk.path for chunk in chunks}
                    outcome = "negative_control" if not expected else (
                        "complete" if expected <= retrieved else "partial" if expected & retrieved else "miss"
                    )
                    row = dict(case, outcome=outcome, retrieved=[
                        {"path": chunk.path, "start": chunk.line_start, "end": chunk.line_end} for chunk in chunks
                    ], missing=sorted(expected - retrieved))
                    if args.answers:
                        try:
                            row["answer_result"] = answer(db, snapshot, case["question"], [], answer_length="detailed")
                        except AppError as exc:
                            row["answer_error"] = exc.code
                    results.append(row)
            transaction.rollback()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name, _commit in groups:
        counts = Counter(row["outcome"] for row in results if row["repository"] == name)
        print(f"{name}: {dict(counts)}")
    print(f"Overall: {dict(Counter(row['outcome'] for row in results))}")


if __name__ == "__main__":
    main()
