from sqlalchemy import text
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.errors import AppError
from app.deadlines import remaining_seconds, check_deadline, budgeted_sleep

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=2, connect_args={"connect_timeout": settings.db_connect_timeout_seconds})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "before_cursor_execute")
def _chat_statement_deadline(_connection, cursor, _statement, _parameters, _context, _executemany):
    remaining = remaining_seconds()
    if remaining is not None:
        # Transaction-local: a pooled connection does not retain this chat's
        # deadline after commit/rollback. Re-clamp before each SQL statement.
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(remaining * 1000))),))

def get_db():
    db = None
    for attempt in range(settings.db_connect_attempts):
        check_deadline()
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            break
        except OperationalError as exc:
            db.close()
            db = None
            if attempt + 1 == settings.db_connect_attempts:
                raise AppError("database_unavailable", "Database is temporarily unavailable. Try again shortly.", 503) from exc
            budgeted_sleep(settings.db_connect_backoff_seconds * (2 ** attempt))
        except BaseException:
            # A deadline can expire during pool checkout or before the first SQL
            # statement; the generator has not reached its yield/finally yet.
            db.close()
            raise
    try:
        yield db
    finally:
        if db is not None:
            db.close()
