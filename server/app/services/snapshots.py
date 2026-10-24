from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.config import settings
from app.errors import AppError
from app.models import Repository, Snapshot, Job, now
from app.quotas import consume
from app.adapters.github import parse_repo_url, resolve_repo

def submit(db: Session, url: str, user_id: str) -> Snapshot:
    repo_url = parse_repo_url(url)
    owner, name, branch, sha = resolve_repo(repo_url)
    repo_stmt = insert(Repository).values(owner=owner, name=name, canonical_url=f"https://github.com/{owner}/{name}", default_branch=branch)
    repo_stmt = repo_stmt.on_conflict_do_update(index_elements=[Repository.owner, Repository.name], set_={"default_branch": branch}).returning(Repository.id)
    repo_id = db.execute(repo_stmt).scalar_one()
    snapshot_id = db.execute(insert(Snapshot).values(repository_id=repo_id, sha=sha, status="queued").on_conflict_do_nothing(index_elements=[Snapshot.repository_id, Snapshot.sha]).returning(Snapshot.id)).scalar_one_or_none()
    if snapshot_id:
        consume(db, f"user:{user_id}", "snapshots", settings.daily_user_snapshots)
        consume(db, "global", "snapshots", settings.daily_global_snapshots)
        db.execute(insert(Job).values(snapshot_id=snapshot_id, status="queued").on_conflict_do_nothing(index_elements=[Job.snapshot_id]))
    else:
        snapshot_id = db.execute(select(Snapshot.id).where(Snapshot.repository_id == repo_id, Snapshot.sha == sha)).scalar_one()
    db.commit()
    return db.get(Snapshot, snapshot_id)

def get_snapshot(db: Session, snapshot_id: str) -> Snapshot:
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        raise AppError("snapshot_not_found", "Snapshot not found.", 404)
    return snapshot

def retry_snapshot(db: Session, snapshot_id: str, user_id: str) -> Snapshot:
    snapshot = db.execute(select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()).scalar_one_or_none()
    if not snapshot:
        raise AppError("snapshot_not_found", "Snapshot not found.", 404)
    if snapshot.status != "failed":
        raise AppError("retry_unavailable", "Only failed analyses can be retried.", 409)
    consume(db, f"user:{user_id}", "snapshots", settings.daily_user_snapshots)
    consume(db, "global", "snapshots", settings.daily_global_snapshots)
    job = db.execute(select(Job).where(Job.snapshot_id == snapshot_id).with_for_update()).scalar_one()
    job.attempts = 0
    job.status = "queued"
    job.error_code = None
    job.lease_until = None
    job.lease_token = None
    job.next_attempt_at = now()
    snapshot.status = "queued"
    snapshot.error_code = None
    db.commit()
    return snapshot
