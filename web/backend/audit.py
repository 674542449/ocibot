"""Audit log helpers."""

from __future__ import annotations

from typing import Any, Optional

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
