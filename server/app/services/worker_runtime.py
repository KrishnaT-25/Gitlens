import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Job
from app.services.jobs import claim, process
from app.services.worker_signal import wait_for_work

logger = logging.getLogger(__name__)
MAX_DB_BACKOFF_SECONDS = 60

class InProcessWorker:
    """Single-process worker for free web tiers without a free worker service."""
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="gitlens-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        from app.services.worker_signal import wake_worker
        wake_worker()
        if self._thread:
            self._thread.join(settings.worker_shutdown_timeout_seconds)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    claimed = claim(db)
                failures = 0
                if claimed:
                    process(*claimed)
                    continue
                wait_for_work(self._seconds_until_next_job())
            except Exception:
                failures += 1
                delay = min(2 ** min(failures, 6), MAX_DB_BACKOFF_SECONDS)
                logger.exception("In-process worker cycle failed; retrying in %s seconds", delay)
                wait_for_work(delay)

    @staticmethod
    def _seconds_until_next_job() -> float | None:
        with SessionLocal() as db:
            queued = db.execute(select(func.min(Job.next_attempt_at)).where(Job.status == "queued")).scalar_one()
            leased = db.execute(select(func.min(Job.lease_until)).where(Job.status == "running")).scalar_one()
        candidates = [value for value in (queued, leased) if value is not None]
        if not candidates:
            return None
        due_at = min(candidates)
        return max(0.0, (due_at - datetime.now(timezone.utc)).total_seconds())
