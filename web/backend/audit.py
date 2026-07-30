"""Audit log helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from web.backend.models import AuditLog


def write_audit(
    db: Session,
    *,
    owner_id: Optional[str],
    action: str,
    target: str = "",
    detail: str | dict[str, Any] = "",
) -> None:
    """Persist an audit row; never raises to callers (best-effort)."""
    try:
        if isinstance(detail, dict):
            import json

            detail_s = json.dumps(detail, ensure_ascii=False)[:4000]
        else:
            detail_s = str(detail or "")[:4000]
        row = AuditLog(
            owner_id=owner_id,
            action=(action or "")[:64],
            target=(target or "")[:256],
            detail=detail_s,
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def prune_audit_log(db: Session, *, retention_days: int, max_rows: int) -> int:
    """Delete audit rows past the retention window / row cap. Returns rows deleted.

    Login attempts are recorded, including failures against usernames that do not
    exist — which means unauthenticated traffic writes rows, and how many is the
    attacker's choice. Without a ceiling a credential-stuffing run against an
    internet-exposed panel grows the table until the disk fills, taking the whole
    stack (Postgres included) down with it.

    Both limits are applied because either alone leaves a gap: the window does not
    bound a burst that happens *inside* it, and the row cap alone would keep
    ancient rows forever on a quiet install.
    """
    deleted = 0
    if retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        # created_at is stored naive-UTC on SQLite, so compare naive.
        result = db.execute(
            delete(AuditLog).where(AuditLog.created_at < cutoff.replace(tzinfo=None))
        )
        deleted += int(result.rowcount or 0)
    if max_rows > 0:
        total = int(db.scalar(select(func.count()).select_from(AuditLog)) or 0)
        if total > max_rows:
            # Find the timestamp of the oldest row worth keeping and delete past
            # it. Portable across SQLite and Postgres, unlike a DELETE with an
            # ORDER BY / LIMIT, and avoids an IN list of 50k ids.
            boundary = db.scalar(
                select(AuditLog.created_at)
                .order_by(AuditLog.created_at.desc())
                .offset(max_rows - 1)
                .limit(1)
            )
            if boundary is not None:
                result = db.execute(delete(AuditLog).where(AuditLog.created_at < boundary))
                deleted += int(result.rowcount or 0)
    if deleted:
        db.commit()
    return deleted
