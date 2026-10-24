import time
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.errors import AppError

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=2, connect_args={"connect_timeout": settings.db_connect_timeout_seconds})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db():
    db = None
    for attempt in range(settings.db_connect_attempts):
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            break
        except OperationalError as exc:
            db.close()
            db = None
            if attempt + 1 == settings.db_connect_attempts:
                raise AppError("database_unavailable", "Database is temporarily unavailable. Try again shortly.", 503) from exc
            time.sleep(settings.db_connect_backoff_seconds * (2 ** attempt))
    try:
        yield db
    finally:
        if db is not None:
            db.close()
