import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, Index, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def now():
    return datetime.now(timezone.utc)

def uid():
    return str(uuid.uuid4())

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    github_id: Mapped[str | None] = mapped_column(String(40), unique=True, index=True, nullable=True)
    github_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("email IS NOT NULL OR github_id IS NOT NULL", name="ck_user_identity"),)

class CodeChallenge(Base):
    __tablename__ = "code_challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class SessionToken(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Quota(Base):
    __tablename__ = "quotas"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    scope: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("scope", "action", "period"), CheckConstraint("count >= 0", name="ck_quota_nonnegative"))

class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    canonical_url: Mapped[str] = mapped_column(String(500))
    default_branch: Mapped[str] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("owner", "name"),)

class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("repository_id", "sha"), Index("ix_snapshot_status", "status"), CheckConstraint("status IN ('queued', 'indexing', 'summarizing', 'ready', 'failed', 'unsupported')", name="ck_snapshot_status"), CheckConstraint("status <> 'ready' OR summary IS NOT NULL", name="ck_ready_has_summary"))

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_job_claim", "status", "next_attempt_at"), CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_job_status"), CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_job_attempts"))

class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint("line_start > 0 AND line_end >= line_start", name="ck_chunk_lines"),)
