"""Durable admission, replay, and expiry for client-keyed chats."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
import logging

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import get_db
from app.deadlines import chat_deadline, check_deadline, remaining_seconds, timeout_error
from app.errors import AppError
from app.models import ChatRequestRecord, now
from app.quotas import consume
from app.services.auth import authenticate
from app.services.rag import AnswerLength, answer
from app.services.snapshots import get_snapshot

logger = logging.getLogger(__name__)
RETENTION = timedelta(days=1)


def admit(snapshot_id: str, request_id: str, question: str, history: list[dict],
          length: AnswerLength, token: str | None) -> tuple[dict | None, tuple[str, datetime] | None]:
    """Atomically reserve generation and charge quota once, or replay its status."""
    fingerprint = sha256(json.dumps([snapshot_id, question, history, length]).encode()).hexdigest()
    with contextmanager(get_db)() as db:
        user = authenticate(db, token)
        deadline = now() + timedelta(seconds=remaining_seconds() or settings.chat_timeout_seconds)
        claim = db.scalar(insert(ChatRequestRecord).values(
            user_id=user.id, request_id=request_id, fingerprint=fingerprint, deadline_at=deadline,
        ).on_conflict_do_nothing().returning(ChatRequestRecord.request_id))
        if claim is not None:
            snapshot = get_snapshot(db, snapshot_id)
            if snapshot.status != "ready":
                raise AppError("not_ready", "Repository analysis is not ready.", 409)
            consume(db, f"user:{user.id}", "chats", settings.daily_user_chats)
            consume(db, "global", "chats", settings.daily_global_chats)
            db.execute(delete(ChatRequestRecord).where(ChatRequestRecord.deadline_at < now() - RETENTION))
            check_deadline()
            db.commit()
            return None, (user.id, deadline)
        record = db.get(ChatRequestRecord, (user.id, request_id))
        if record.fingerprint != fingerprint:
            raise AppError("chat_request_conflict", "This request ID belongs to a different question.", 409)
        if record.result is not None:
            return record.result, None
        if record.deadline_at <= now():
            return _error(timeout_error()), None
        return None, None


def run_reserved(user_id: str, deadline: datetime, request_id: str, snapshot_id: str,
                 question: str, history: list[dict], length: AnswerLength) -> None:
    """Use the admission deadline and record the terminal result outside that budget."""
    try:
        with chat_deadline((deadline - now()).total_seconds()), contextmanager(get_db)() as db:
            snapshot = get_snapshot(db, snapshot_id)
            result = {"answer": answer(db, snapshot, question, history, answer_length=length)}
            check_deadline()
    except AppError as exc:
        result = _error(exc)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014" or deadline <= now():
            result = _error(timeout_error())
        else:
            logger.exception("Reserved chat database failure")
            result = _error(AppError("internal_error", "Unexpected server error.", 500))
    except Exception:
        logger.exception("Reserved chat failed")
        result = _error(AppError("internal_error", "Unexpected server error.", 500))

    with contextmanager(get_db)() as db:
        record = db.get(ChatRequestRecord, (user_id, request_id))
        if record is not None:
            record.result = _error(timeout_error()) if deadline <= now() else result
            db.commit()


def _error(exc: AppError) -> dict:
    return {"error": {"code": exc.code, "message": exc.message, "terminal": True}, "status": exc.status}
