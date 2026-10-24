import hashlib
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from app.models import Quota
from app.errors import AppError

def consume(db: Session, scope: str, action: str, limit: int, period: str | None = None):
    period = period or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stmt = insert(Quota).values(scope=scope, action=action, period=period, count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Quota.scope, Quota.action, Quota.period],
        set_={"count": Quota.count + 1},
        where=Quota.count < limit,
    ).returning(Quota.id)
    if db.execute(stmt).scalar_one_or_none() is None:
        raise AppError("quota_exceeded", "Usage limit reached. Try again later.", 429)

def ip_scope(ip: str, pepper: str) -> str:
    return "ip:" + hashlib.sha256((pepper + ip).encode()).hexdigest()[:24]
