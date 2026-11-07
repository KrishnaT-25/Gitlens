"""One monotonic processing budget propagated through a chat request's work."""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from app.errors import AppError

_expires_at: ContextVar[float | None] = ContextVar("chat_expires_at", default=None)


def timeout_error() -> AppError:
    return AppError("chat_timeout", "Repository analysis timed out. Please try a more focused question.", 504)


def remaining_seconds() -> float | None:
    """Return remaining chat time, raising before further work after expiry."""
    expires = _expires_at.get()
    if expires is None:
        return None
    remaining = expires - time.monotonic()
    if remaining <= 0:
        raise timeout_error()
    return remaining


def check_deadline() -> None:
    remaining_seconds()


def bounded_seconds(seconds: float) -> float:
    """Clamp an operation's own timeout to the remaining shared budget."""
    remaining = remaining_seconds()
    return seconds if remaining is None else min(seconds, remaining)


def budgeted_sleep(seconds: float) -> None:
    """Retries consume the original budget instead of starting a new one."""
    if bounded_seconds(seconds) < seconds:
        raise timeout_error()
    time.sleep(seconds)
    check_deadline()


@contextmanager
def chat_deadline(seconds: float) -> Iterator[None]:
    """Nested calls inherit the earlier deadline; contexts remain request-local."""
    expires = time.monotonic() + seconds
    parent = _expires_at.get()
    token = _expires_at.set(min(parent, expires) if parent is not None else expires)
    try:
        check_deadline()
        yield
    finally:
        _expires_at.reset(token)
