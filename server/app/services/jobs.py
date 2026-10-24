import logging
import uuid
from datetime import timedelta
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import Job, Snapshot, Repository, Chunk, now
from app.errors import AppError
from app.adapters.github import RepoURL
from app.adapters.workflow import dispatch_ingestion
from app.config import settings
from app.services.ingest import download_archive, parse_archive
from app.services.rag import make_summary

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
    if snapshot.status != "summarizing":
        snapshot.status = "indexing"
    db.commit()
    return job.id, job.lease_token

def process(job_id: str, lease_token: str):
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            snapshot = db.get(Snapshot, job.snapshot_id)
            repository = db.get(Repository, snapshot.repository_id)
            existing = db.execute(select(Chunk.id).where(Chunk.snapshot_id == snapshot.id).limit(1)).scalar_one_or_none()
            if not existing:
                archive = download_archive(repository.owner, repository.name, snapshot.sha)
                chunks = parse_archive(archive)
                current = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
                db.refresh(current)
                if current.lease_token != lease_token or current.status != "running":
                    db.rollback()
                    return
                db.execute(delete(Chunk).where(Chunk.snapshot_id == snapshot.id))
                db.add_all(Chunk(snapshot_id=snapshot.id, path=c.path, line_start=c.line_start, line_end=c.line_end, text=c.text) for c in chunks)
                snapshot.status = "summarizing"
                db.commit()
            summary = make_summary(db, snapshot.id)
            current = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            db.refresh(current)
            if current.lease_token != lease_token or current.status != "running":
                db.rollback()
                return
            snapshot.summary = summary
            snapshot.status = "ready"
            snapshot.error_code = None
            job.status = "done"
            job.lease_until = None
            job.lease_token = None
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
        if permanent:
            job.status = "failed"
            snapshot.status = "unsupported"
        elif job.attempts >= 3:
            job.status = "failed"
            snapshot.status = "failed"
        else:
            job.status = "queued"
            job.next_attempt_at = now() + timedelta(seconds=2 ** job.attempts * 10)
            if snapshot.status != "summarizing":
                snapshot.status = "queued"
        db.commit()
