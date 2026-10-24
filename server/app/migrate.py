import logging
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import engine

logger = logging.getLogger(__name__)

def main() -> None:
    config = Config("alembic.ini")
    for attempt in range(settings.db_connect_attempts):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            command.upgrade(config, "head")
            return
        except OperationalError:
            if attempt + 1 == settings.db_connect_attempts:
                raise
            delay = settings.db_connect_backoff_seconds * (2 ** attempt)
            logger.warning("Database unavailable during startup; retry %s/%s in %.2fs", attempt + 1, settings.db_connect_attempts - 1, delay)
            time.sleep(delay)
    raise RuntimeError("Database migration retry loop ended unexpectedly")

if __name__ == "__main__":
    main()
