"""Audit log listing."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from web.backend.audit import iso_utc
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import AuditLog, User

router = APIRouter(prefix="/audit", tags=["audit"])

# A login attempt against a username that does not exist has no owner, so the
# owner_id match filtered those rows out: the panel recorded every
# credential-stuffing attempt and then displayed none of them. Admins see them now.
_AUTH_ACTIONS = (
    "auth.login",
    "auth.login_failed",
    "auth.login_blocked",
    "auth.login_disabled",
    "auth.totp_failed",
    "auth.logout_all",
)


@router.get("")
def list_audit(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(100, ge=1, le=500),
    auth_only: bool = Query(False, description="only return login-related events"),
) -> list[dict[str, Any]]:
    stmt = select(AuditLog)
    if user.is_admin:
        # NULL owner = anonymous event (failed login for an unknown username, or a
        # rate-limit block). Only an admin has a reason to see other accounts'.
        stmt = stmt.where(or_(AuditLog.owner_id == user.id, AuditLog.owner_id.is_(None)))
    else:
        stmt = stmt.where(AuditLog.owner_id == user.id)
    if auth_only:
        stmt = stmt.where(AuditLog.action.in_(_AUTH_ACTIONS))
    rows = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "created_at": iso_utc(r.created_at),
        }
        for r in rows
    ]
