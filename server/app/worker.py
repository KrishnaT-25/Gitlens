import logging
import sys
import time
from app.db import SessionLocal
from app.services.jobs import claim, process

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    drain = "--drain" in sys.argv
    deadline = time.monotonic() + 15 * 60 if drain else None
    processed = 0
    while True:
        try:
            if drain and (processed >= 8 or time.monotonic() >= deadline):
                break
            with SessionLocal() as db:
                claimed = claim(db)
            if claimed:
                process(*claimed)
                processed += 1
            else:
                if drain:
                    break
                time.sleep(3)
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Worker loop failed")
            if drain:
                raise
            time.sleep(5)

if __name__ == "__main__":
    main()
