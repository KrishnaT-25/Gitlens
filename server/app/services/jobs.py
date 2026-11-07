import logging
import uuid
from datetime import timedelta
from sqlalchemy import select, delete, func, literal, text, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from app.db import SessionLocal
from app.models import Job, Snapshot, Repository, Chunk, SnapshotFile, SnapshotNode, SymbolReference, ChunkEmbedding, now
from app.errors import AppError
from app.adapters.github import RepoURL
from app.adapters.workflow import dispatch_ingestion
from app.config import settings
from app.services.ingest import download_archive, parse_repository
from app.services.rag import make_summary, structural_summary, summarize_repository_nodes
from app.adapters.embeddings import embed_texts, embedding_profile, validate_vectors

logger = logging.getLogger(__name__)
PERMANENT = {"archive_too_large", "repository_too_large", "empty_repository", "unsafe_archive", "unsupported_repository", "index_schema_downgrade"}
LEASE_DURATION = timedelta(minutes=15)


class LeaseLost(Exception):
    """The worker may no longer write results for this job."""


def _lock_lease(db: Session, job_id: str, lease_token: str) -> Job:
    job = db.get(Job, job_id, populate_existing=True, with_for_update=True)
    if (job is None or job.status != "running" or job.lease_token != lease_token
            or job.lease_until is None or job.lease_until <= now()):
        raise LeaseLost
    return job


def _renew_lease(db: Session, job_id: str, lease_token: str) -> Job:
    job = _lock_lease(db, job_id, lease_token)
    job.lease_until = now() + LEASE_DURATION
    return job

def kick(db: Session, snapshot_id: str) -> None:
    _queue_embedding_rebuild(db, snapshot_id)
    if settings.worker_mode == "inprocess":
        from app.services.worker_signal import wake_worker
        wake_worker()
        return
    if settings.worker_mode != "actions" or not settings.worker_dispatch_repo:
        return
    job = db.execute(select(Job).where(Job.snapshot_id == snapshot_id).with_for_update(skip_locked=True)).scalar_one_or_none()
    current_time = now()
    if not job or job.status != "queued" or job.next_attempt_at > current_time or (job.dispatched_at and job.dispatched_at > current_time - timedelta(minutes=1)):
        return
    job.dispatched_at = current_time
    db.commit()
    try:
        dispatch_ingestion()
    except AppError as exc:
        logger.warning("Worker dispatch failed for job %s: %s", job.id, exc.code)


def _queue_embedding_rebuild(db: Session, snapshot_id: str) -> None:
    """Lazily schedule one repair per space on access after a configuration change.

    Do not repeatedly spend quota on a failed provider. A changed fingerprint
    permits a new repair; quota-only retries can be explicitly requeued.
    """
    snapshot = db.get(Snapshot, snapshot_id)
    profile = embedding_profile()
    if not snapshot or snapshot.status != "ready" or (snapshot.index_report or {}).get("embedding") == profile:
        db.commit()
        return
    report = snapshot.index_report or {}
    if report.get("embeddingRepairAttempt") == profile["fingerprint"]:
        db.commit()
        return
    db.execute(insert(Job).values(snapshot_id=snapshot_id, status="queued")
               .on_conflict_do_nothing(index_elements=[Job.snapshot_id]))
    # Match the worker's job-before-snapshot lock order to avoid deadlocks.
    job = db.execute(select(Job).where(Job.snapshot_id == snapshot_id).with_for_update()
                     .execution_options(populate_existing=True)).scalar_one()
    db.refresh(snapshot)
    report = snapshot.index_report or {}
    if report.get("embeddingRepairAttempt") == profile["fingerprint"]:
        db.commit()
        return
    if job.status in {"done", "failed"}:
        job.status, job.attempts, job.next_attempt_at = "queued", 0, now()
        job.error_code = job.lease_until = job.lease_token = job.dispatched_at = None
    snapshot.index_report = {**report, "embeddingRepairAttempt": profile["fingerprint"]}
    db.commit()


def claim(db: Session) -> tuple[str, str] | None:
    exhausted = db.execute(select(Job).where(
        Job.status == "running", Job.attempts >= 3, Job.lease_until < now(),
    ).order_by(Job.lease_until, Job.id).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True).limit(1)).scalar_one_or_none()
    if exhausted:
        exhausted.status = "failed"
        exhausted.error_code = exhausted.error_code or "processing_failed"
        exhausted.lease_until = None
        exhausted.lease_token = None
        exhausted_snapshot = db.get(Snapshot, exhausted.snapshot_id, populate_existing=True)
        if exhausted_snapshot.summary is None:
            exhausted_snapshot.status = "failed"
            exhausted_snapshot.error_code = exhausted.error_code
        else:
            exhausted_snapshot.status = "ready"
            exhausted_snapshot.error_code = None
        db.commit()
    job = db.execute(select(Job).where(
        Job.attempts < 3,
        ((Job.status == "queued") & (Job.next_attempt_at <= now())) | ((Job.status == "running") & (Job.lease_until < now())),
    ).order_by(Job.next_attempt_at, Job.id).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True).limit(1)).scalar_one_or_none()
    if not job:
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_until = now() + LEASE_DURATION
    job.lease_token = str(uuid.uuid4())
    snapshot = db.get(Snapshot, job.snapshot_id, populate_existing=True)
    if snapshot.summary is not None:
        snapshot.status = "ready"
        snapshot.error_code = None
    elif snapshot.status != "summarizing":
        snapshot.status = "indexing"
    db.commit()
    return job.id, job.lease_token

def process(job_id: str, lease_token: str):
    try:
        with SessionLocal() as db:
            job = _renew_lease(db, job_id, lease_token)
            snapshot = db.get(Snapshot, job.snapshot_id)
            repository = db.get(Repository, snapshot.repository_id)
            target_version = settings.index_schema_version
            published_version = snapshot.index_version if snapshot.summary is not None else None
            if published_version is not None and target_version < published_version:
                raise AppError("index_schema_downgrade", "The worker cannot replace a newer published index.", 409)
            vector_repair = published_version == target_version
            stage_version = -target_version if vector_repair else target_version
            staged = db.scalar(select(Chunk.id).where(Chunk.snapshot_id == snapshot.id, Chunk.index_version == stage_version).limit(1))
            if vector_repair and not staged:
                _stage_repair_chunks(db, snapshot.id, target_version, stage_version)
            db.commit()
            if not vector_repair and not staged:
                archive = download_archive(repository.owner, repository.name, snapshot.sha)
                parsed = parse_repository(archive)
                keyword_search = "bm25" if _pg_search_available(db) else "postgres_fts"
                _renew_lease(db, job_id, lease_token)
                _clear_staged(db, snapshot.id, target_version)
                chunk_rows = [Chunk(snapshot_id=snapshot.id, path=c.path, line_start=c.line_start, line_end=c.line_end,
                                    text=c.text, index_version=target_version, language=c.language,
                                    symbol_path=c.symbol_path, syntax_kind=c.syntax_kind) for c in parsed.chunks]
                db.add_all(chunk_rows)
                db.flush()
                db.add_all(SnapshotFile(snapshot_id=snapshot.id, index_version=target_version, path=f.path,
                                        content=f.content, language=f.language, line_count=f.line_count,
                                        byte_size=f.byte_size, summary=f.summary) for f in parsed.files)
                _persist_map(db, snapshot.id, target_version, parsed.files)
                by_path = {}
                for row in chunk_rows:
                    by_path.setdefault(row.path, []).append(row)
                db.add_all(SymbolReference(snapshot_id=snapshot.id, index_version=target_version,
                                           chunk_id=next((c.id for c in by_path.get(s.path, []) if c.line_start <= s.line <= c.line_end), by_path[s.path][0].id),
                                           name=s.name, path=s.path, line=s.line, is_definition=s.is_definition)
                            for s in parsed.symbols if by_path.get(s.path))
                staged_report = {"version": target_version, "omitted": parsed.omitted,
                                 "semantic": "pending", "keywordSearch": keyword_search,
                                 "parserFallbacks": sum(1 for f in parsed.files if f.language and not any(c.path == f.path and c.syntax_kind not in {"text", "section", "config_section"} for c in parsed.chunks))}
                root_node = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot.id,
                    SnapshotNode.index_version == target_version, SnapshotNode.path == "", SnapshotNode.node_type == "directory")).scalar_one()
                root_node.metadata_json = {**(root_node.metadata_json or {}), "stagedIndexReport": staged_report}
                db.commit()
            embedding_report = _embed_staged_chunks(db, snapshot.id, stage_version, job_id, lease_token)
            error = embedding_report["semanticErrorCode"]
            if published_version is not None and error and (vector_repair or error != "embedding_unconfigured"):
                raise AppError(error, "The replacement index could not complete embedding.", 503)
            if vector_repair:
                # Vector-only repairs reuse the exact existing text index/summary.
                summary = snapshot.summary
                summary_calls = (snapshot.index_report or {}).get("directorySummaryCalls", 0)
                summary_mode = (snapshot.index_report or {}).get("repositorySummary", "cached")
            else:
                summary_calls = summarize_repository_nodes(
                    db, snapshot.id, target_version, lambda: _renew_lease(db, job_id, lease_token))
                try:
                    summary = make_summary(db, snapshot.id, target_version)
                    summary_mode = "gemini"
                except AppError as exc:
                    if not exc.code.startswith("gemini_"):
                        raise
                    summary = structural_summary(db, snapshot.id, target_version)
                    summary_mode = "structural"
            job = _lock_lease(db, job_id, lease_token)
            db.refresh(snapshot)
            if vector_repair:
                staged_report = snapshot.index_report or {}
                _publish_repair_vectors(db, snapshot.id, target_version, stage_version)
            else:
                root_node = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot.id,
                    SnapshotNode.index_version == target_version, SnapshotNode.path == "", SnapshotNode.node_type == "directory")).scalar_one()
                staged_report = (root_node.metadata_json or {}).get("stagedIndexReport", {})
                _remove_old_index(db, snapshot.id, target_version, published_version)
            snapshot.summary = summary
            snapshot.index_version = target_version
            snapshot.index_report = {**staged_report, **embedding_report, "version": target_version,
                                     "directorySummaryCalls": summary_calls, "repositorySummary": summary_mode}
            snapshot.status = "ready"
            snapshot.error_code = None
            job.status = "done"
            job.error_code = None
            job.lease_until = None
            job.lease_token = None
            db.commit()
    except LeaseLost:
        return
    except AppError as exc:
        _fail(job_id, lease_token, exc.code, exc.code in PERMANENT)
    except Exception:
        logger.exception("Unexpected ingestion failure for job %s", job_id)
        _fail(job_id, lease_token, "processing_failed", False)

def _fail(job_id: str, lease_token: str, code: str, permanent: bool):
    with SessionLocal() as db:
        try:
            job = _lock_lease(db, job_id, lease_token)
        except LeaseLost:
            return
        snapshot = db.get(Snapshot, job.snapshot_id)
        job.error_code = code
        job.lease_until = None
        job.lease_token = None
        has_usable_index = snapshot.summary is not None
        if has_usable_index:
            snapshot.status = "ready"
            snapshot.error_code = None
        else:
            snapshot.error_code = code
        if permanent:
            job.status = "failed"
            if not has_usable_index:
                snapshot.status = "unsupported"
        elif job.attempts >= 3:
            job.status = "failed"
            if not has_usable_index:
                snapshot.status = "failed"
        else:
            job.status = "queued"
            job.next_attempt_at = now() + timedelta(seconds=2 ** job.attempts * 10)
            if not has_usable_index and snapshot.status != "summarizing":
                snapshot.status = "queued"
        db.commit()

def _clear_staged(db: Session, snapshot_id: str, version: int) -> None:
    db.execute(delete(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version))
    db.execute(delete(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id, SnapshotFile.index_version == version))
    db.execute(delete(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot_id, SnapshotNode.index_version == version))
    db.execute(delete(SymbolReference).where(SymbolReference.snapshot_id == snapshot_id, SymbolReference.index_version == version))

def _persist_map(db: Session, snapshot_id: str, version: int, files: list) -> None:
    directories = {""}
    for file in files:
        parts = file.path.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    for path in sorted(directories, key=lambda item: (item.count("/"), item)):
        parent = path.rpartition("/")[0] if path else ""
        db.add(SnapshotNode(snapshot_id=snapshot_id, index_version=version, path=path, parent_path=parent,
                            node_type="directory", summary="", metadata_json={"children": []}))
    for file in files:
        parent = file.path.rpartition("/")[0]
        db.add(SnapshotNode(snapshot_id=snapshot_id, index_version=version, path=file.path, parent_path=parent,
                            node_type="file", summary=file.summary,
                            metadata_json={"language": file.language, "lineCount": file.line_count}))
    db.flush()
    nodes = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot_id, SnapshotNode.index_version == version)).scalars().all()
    children_by_parent = {}
    for node in nodes:
        if node.path != node.parent_path:
            children_by_parent.setdefault(node.parent_path, []).append(node.path.rsplit("/", 1)[-1])
    for node in nodes:
        node.metadata_json = {**(node.metadata_json or {}), "children": sorted(children_by_parent.get(node.path, []))}

def _repair_chunk_id():
    return func.md5(literal("gitlens:embedding-repair:") + Chunk.id)


def _stage_repair_chunks(db: Session, snapshot_id: str, version: int, stage_version: int) -> None:
    """Give replacement vectors private chunk IDs while keeping published IDs stable."""
    source = (Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)
    db.execute(insert(Chunk).from_select(
        ["id", "snapshot_id", "index_version", "path", "line_start", "line_end", "text", "language", "symbol_path", "syntax_kind"],
        select(_repair_chunk_id(), Chunk.snapshot_id, literal(stage_version), Chunk.path,
               Chunk.line_start, Chunk.line_end, Chunk.text, Chunk.language, Chunk.symbol_path, Chunk.syntax_kind)
        .where(*source),
    ))
    db.execute(insert(ChunkEmbedding).from_select(
        ["id", "chunk_id", "model", "fingerprint", "dimensions", "format_version", "embedding"],
        select(func.gen_random_uuid().cast(ChunkEmbedding.id.type), _repair_chunk_id(), ChunkEmbedding.model,
               ChunkEmbedding.fingerprint, ChunkEmbedding.dimensions, ChunkEmbedding.format_version, ChunkEmbedding.values)
        .join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
        .where(*source, _compatible_embeddings(embedding_profile())),
    ))


def _publish_repair_vectors(db: Session, snapshot_id: str, version: int, stage_version: int) -> None:
    """Swap vectors in the publication transaction and remove the repair workspace."""
    source = (Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)
    db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(select(Chunk.id).where(*source))))
    db.execute(update(ChunkEmbedding).where(*source, ChunkEmbedding.chunk_id == _repair_chunk_id())
               .values(chunk_id=Chunk.id).execution_options(synchronize_session=False))
    _clear_staged(db, snapshot_id, stage_version)


def _compatible_embeddings(profile: dict):
    return ((ChunkEmbedding.fingerprint == profile["fingerprint"])
            & (ChunkEmbedding.model == profile["model"])
            & (ChunkEmbedding.dimensions == profile["dimensions"])
            & (ChunkEmbedding.format_version == profile["formatVersion"]))


def _embed_staged_chunks(db: Session, snapshot_id: str, version: int, job_id: str, lease_token: str) -> dict:
    """Checkpoint only compatible validated batches, under the current worker lease."""
    profile = embedding_profile()
    _renew_lease(db, job_id, lease_token)

    def report(state: str, reason: str | None = None) -> dict:
        return {"embedding": profile, "embeddingRepairAttempt": profile["fingerprint"],
                "semantic": state, "semanticErrorCode": reason}

    # Includes old vectors with no fingerprint. Never reuse them by chunk ID alone.
    ids = select(Chunk.id).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)
    compatible = _compatible_embeddings(profile)
    db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(ids),
               (ChunkEmbedding.fingerprint.is_(None)) | ~compatible))
    db.commit()
    if not settings.gemini_api_key:
        return report("keyword_only", "embedding_unconfigured")
    chunks = db.execute(select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)
                        .order_by(Chunk.path, Chunk.line_start)).scalars().all()
    embedded = set(db.execute(select(ChunkEmbedding.chunk_id).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                              .where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version, compatible)).scalars())
    for start in range(0, len(chunks), settings.embedding_batch_size):
        batch = [chunk for chunk in chunks[start:start + settings.embedding_batch_size] if chunk.id not in embedded]
        if not batch:
            continue
        try:
            vectors = validate_vectors(embed_texts([c.text for c in batch], "RETRIEVAL_DOCUMENT",
                       titles=[f"{c.path} {c.symbol_path or ''}".strip() for c in batch]), len(batch))
        except AppError as exc:
            if exc.code.startswith("embedding_"):
                # Earlier batches are durable and the next leased run resumes them.
                return report("keyword_only", exc.code)
            raise
        _renew_lease(db, job_id, lease_token)
        db.add_all(ChunkEmbedding(chunk_id=chunk.id, model=profile["model"], fingerprint=profile["fingerprint"],
                                 dimensions=profile["dimensions"], format_version=profile["formatVersion"], values=vector)
                   for chunk, vector in zip(batch, vectors))
        db.commit()
    total_count = db.execute(select(Chunk.id).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)).scalars().all()
    final_count = db.execute(select(ChunkEmbedding.chunk_id).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                             .where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version, compatible)).scalars().all()
    complete = bool(total_count) and len(final_count) == len(total_count)
    return report("ready" if complete else "keyword_only", None if complete else "embedding_incomplete")

def _remove_old_index(db: Session, snapshot_id: str, version: int, previous_version: int | None) -> None:
    # Readers can carry their original index version across chat transactions.
    keep = {version} if previous_version is None else {version, previous_version}
    for model in (Chunk, SnapshotFile, SnapshotNode, SymbolReference):
        db.execute(delete(model).where(model.snapshot_id == snapshot_id, model.index_version.not_in(keep)))

def _pg_search_available(db: Session) -> bool:
    try:
        return bool(db.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_search'")).first())
    except Exception:
        db.rollback()
        return False
