"""AppMeta key-value helpers (worker heartbeat, runtime settings)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.models import AppMeta

# Well-known keys
KEY_WORKER_HEARTBEAT = "worker_heartbeat"  # ISO timestamp, updated by worker every tick
KEY_WORKER_ID = "worker_heartbeat_worker_id"
KEY_OPEN_REGISTRATION = "allow_open_registration"  # "true"/"false"; overrides env when set
KEY_DAILY_CHECKS_DATE = "daily_checks_date"  # YYYY-MM-DD of the last daily-check run


def get_meta(db: Session, key: str) -> Optional[str]:
    row = db.scalar(select(AppMeta).where(AppMeta.key == key))
    return row.value if row is not None else None


def set_meta(db: Session, key: str, value: str) -> None:
    row = db.scalar(select(AppMeta).where(AppMeta.key == key))
    if row is None:
        row = AppMeta(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()
