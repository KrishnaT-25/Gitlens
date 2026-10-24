import logging
import uuid
from datetime import timedelta
from sqlalchemy import select, delete, text
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import Job, Snapshot, Repository, Chunk, SnapshotFile, SnapshotNode, SymbolReference, ChunkEmbedding, now
from app.errors import AppError
from app.adapters.github import RepoURL
from app.adapters.workflow import dispatch_ingestion
from app.config import settings
from app.services.ingest import download_archive, parse_repository
from app.services.rag import make_summary, structural_summary, summarize_repository_nodes
from app.adapters.embeddings import embed_texts

logger = logging.getLogger(__name__)
PERMANENT = {"archive_too_large", "repository_too_large", "empty_repository", "unsafe_archive", "unsupported_repository"}

def kick(db: Session, snapshot_id: str) -> None:
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

def claim(db: Session) -> tuple[str, str] | None:
    exhausted = db.execute(select(Job).where(
        Job.status == "running", Job.attempts >= 3, Job.lease_until < now(),
    ).order_by(Job.lease_until, Job.id).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if exhausted:
        exhausted.status = "failed"
        exhausted.error_code = exhausted.error_code or "processing_failed"
        exhausted.lease_until = None
        exhausted.lease_token = None
        exhausted_snapshot = db.get(Snapshot, exhausted.snapshot_id)
        exhausted_snapshot.status = "failed"
        exhausted_snapshot.error_code = exhausted.error_code
        db.commit()
    job = db.execute(select(Job).where(
        Job.attempts < 3,
        ((Job.status == "queued") & (Job.next_attempt_at <= now())) | ((Job.status == "running") & (Job.lease_until < now())),
    ).order_by(Job.next_attempt_at, Job.id).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
    if not job:
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_until = now() + timedelta(minutes=15)
    job.lease_token = str(uuid.uuid4())
    snapshot = db.get(Snapshot, job.snapshot_id)
    if snapshot.status != "summarizing" and not (snapshot.status == "ready" and snapshot.index_version < settings.index_schema_version):
        snapshot.status = "indexing"
    db.commit()
    return job.id, job.lease_token

def process(job_id: str, lease_token: str):
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            snapshot = db.get(Snapshot, job.snapshot_id)
            repository = db.get(Repository, snapshot.repository_id)
            target_version = settings.index_schema_version
            staged = db.execute(select(Chunk.id).where(Chunk.snapshot_id == snapshot.id, Chunk.index_version == target_version).limit(1)).scalar_one_or_none()
            if not staged:
                archive = download_archive(repository.owner, repository.name, snapshot.sha)
                parsed = parse_repository(archive)
                current = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
                db.refresh(current)
                if current.lease_token != lease_token or current.status != "running":
                    db.rollback()
                    return
                keyword_search = "bm25" if _pg_search_available(db) else "postgres_fts"
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
                snapshot.status = "indexing"
                db.commit()
            semantic = _embed_staged_chunks(db, snapshot.id, target_version, job_id, lease_token)
            if semantic is None:
                return
            summary_calls = summarize_repository_nodes(db, snapshot.id, target_version)
            try:
                summary = make_summary(db, snapshot.id, target_version)
                summary_mode = "gemini"
            except AppError as exc:
                if not exc.code.startswith("gemini_"):
                    raise
                summary = structural_summary(db, snapshot.id, target_version)
                summary_mode = "structural"
            current = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            db.refresh(current)
            if current.lease_token != lease_token or current.status != "running":
                db.rollback()
                return
            snapshot.summary = summary
            snapshot.index_version = target_version
            root_node = db.execute(select(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot.id,
                SnapshotNode.index_version == target_version, SnapshotNode.path == "", SnapshotNode.node_type == "directory")).scalar_one()
            staged_report = (root_node.metadata_json or {}).get("stagedIndexReport", {})
            snapshot.index_report = {**staged_report, "semantic": "ready" if semantic else "bm25_only",
                                     "directorySummaryCalls": summary_calls, "repositorySummary": summary_mode}
            snapshot.status = "ready"
            snapshot.error_code = None
            job.status = "done"
            job.lease_until = None
            job.lease_token = None
            _remove_old_index(db, snapshot.id, target_version)
            db.commit()
    except AppError as exc:
        _fail(job_id, lease_token, exc.code, exc.code in PERMANENT)
    except Exception:
        logger.exception("Unexpected ingestion failure for job %s", job_id)
        _fail(job_id, lease_token, "processing_failed", False)

def _fail(job_id: str, lease_token: str, code: str, permanent: bool):
    with SessionLocal() as db:
        job = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
        if job.lease_token != lease_token or job.status != "running":
            return
        snapshot = db.get(Snapshot, job.snapshot_id)
        job.error_code = code
        job.lease_until = None
        job.lease_token = None
        snapshot.error_code = code
        has_usable_index = snapshot.status == "ready" and snapshot.summary is not None and snapshot.index_version < settings.index_schema_version
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

def _embed_staged_chunks(db: Session, snapshot_id: str, version: int, job_id: str, lease_token: str) -> bool | None:
    if not settings.gemini_api_key:
        return False
    chunks = db.execute(select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)
                        .order_by(Chunk.path, Chunk.line_start)).scalars().all()
    embedded = set(db.execute(select(ChunkEmbedding.chunk_id).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                              .where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)).scalars())
    for start in range(0, len(chunks), settings.embedding_batch_size):
        batch = [chunk for chunk in chunks[start:start + settings.embedding_batch_size] if chunk.id not in embedded]
        if not batch:
            continue
        try:
            vectors = embed_texts([f"{c.path} {c.symbol_path or ''}\n{c.text}" for c in batch], "RETRIEVAL_DOCUMENT")
        except AppError as exc:
            if exc.code.startswith("embedding_"):
                return False
            raise
        current = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
        if current.status != "running" or current.lease_token != lease_token:
            db.rollback()
            return None
        current.lease_until = now() + timedelta(minutes=15)
        db.add_all(ChunkEmbedding(chunk_id=chunk.id, model=settings.gemini_embedding_model, values=vector)
                   for chunk, vector in zip(batch, vectors))
        db.commit()
    total_count = db.execute(select(Chunk.id).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)).scalars().all()
    final_count = db.execute(select(ChunkEmbedding.chunk_id).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                             .where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == version)).scalars().all()
    return bool(total_count) and len(final_count) == len(total_count)

def _remove_old_index(db: Session, snapshot_id: str, version: int) -> None:
    db.execute(delete(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version != version))
    db.execute(delete(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id, SnapshotFile.index_version != version))
    db.execute(delete(SnapshotNode).where(SnapshotNode.snapshot_id == snapshot_id, SnapshotNode.index_version != version))
    db.execute(delete(SymbolReference).where(SymbolReference.snapshot_id == snapshot_id, SymbolReference.index_version != version))

def _pg_search_available(db: Session) -> bool:
    try:
        return bool(db.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_search'")).first())
    except Exception:
        db.rollback()
        return False
