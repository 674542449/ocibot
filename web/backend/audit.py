"""Audit log helpers (plus the UTC timestamp serializer routes share)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from web.backend.models import AuditLog
from web.backend.schemas import UtcDatetime

# Routes that hand-build a response dict cannot lean on a response_model, and three
# of them shipped a bare .isoformat(). SQLite returns DateTime(timezone=True) values
# with tzinfo stripped, so that emitted "2026-08-23T14:23:48" with no offset, and per
# ES2015 the SPA's `new Date(v)` reads an offset-less date-time as LOCAL — a UTC+8
# operator saw every audit event eight hours early. Going through the same
# UtcDatetime the response schemas use keeps hand-built dicts byte-identical to what
# UserOut/TenantOut put on the wire, and there is now one place to change.
_UTC_ISO = TypeAdapter(UtcDatetime)


def iso_utc(value: Any) -> str:
    """Serialize a stored datetime exactly as a UtcDatetime schema field would."""
    if value is None:
        return ""
    return str(_UTC_ISO.dump_python(_UTC_ISO.validate_python(value), mode="json"))


# The only actions an *unauthenticated* request can mint: every login attempt
# writes login_failed, and tripping the rate limiter writes login_blocked — the
# limiter returns before anything else runs, so nothing bounds how many of these
# one IP produces. Every other action in this table needed a valid session.
#
# The row cap must evict these first. Otherwise the disk-fill guard doubles as a
# delete primitive for the very attacker who triggers it: 50 000 throwaway rows
# from a login flood push out the tenant/instance/2FA history that the operator
# would investigate the flood with — including the login_failed rows of the real
# break-in the flood was covering for.
_UNAUTHENTICATED_ACTIONS = ("auth.login_failed", "auth.login_blocked")


def retention_cutoff(retention_days: int) -> datetime:
    """UTC-aware cutoff for the retention window.

    Deliberately aware. On Postgres `created_at` is `timestamptz`, and a naive
    literal is cast using the session TimeZone GUC — on a server left at, say,
    Asia/Shanghai the 180-day window silently became 180 days minus 8 hours.
    SQLite's DateTime bind processor formats year..microsecond and ignores tzinfo
    entirely, so the emitted literal there is unchanged by carrying the offset.
    """
    return datetime.now(timezone.utc) - timedelta(days=retention_days)


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


def _trim_oldest(db: Session, *, keep: int, only_actions: Optional[tuple[str, ...]]) -> int:
    """Delete the oldest rows of the selected set, keeping the newest `keep`.

    Works off the timestamp of the oldest row worth keeping rather than a DELETE
    with ORDER BY / LIMIT (not portable to Postgres) or an IN list of 50k ids.
    """
    action_filter = None if only_actions is None else AuditLog.action.in_(only_actions)
    if keep <= 0:
        stmt = delete(AuditLog)
        if action_filter is not None:
            stmt = stmt.where(action_filter)
        return int(db.execute(stmt).rowcount or 0)
    sel = select(AuditLog.created_at)
    if action_filter is not None:
        sel = sel.where(action_filter)
    boundary = sel.order_by(AuditLog.created_at.desc()).offset(keep - 1).limit(1)
    cut = db.scalar(boundary)
    if cut is None:
        return 0
    stmt = delete(AuditLog).where(AuditLog.created_at < cut)
    if action_filter is not None:
        stmt = stmt.where(action_filter)
    return int(db.execute(stmt).rowcount or 0)


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

    The row cap spends its eviction budget on `_UNAUTHENTICATED_ACTIONS` first.
    Trimming strictly by age let anyone who can reach /auth/login choose what this
    table forgets — flood past the cap and the security history scrolls out from
    under the operator. Real history is only touched once there is no login noise
    left to reclaim, because at that point the disk is the bigger problem.
    """
    deleted = 0
    if retention_days > 0:
        result = db.execute(
            delete(AuditLog).where(AuditLog.created_at < retention_cutoff(retention_days))
        )
        deleted += int(result.rowcount or 0)
    if max_rows > 0:
        total = int(db.scalar(select(func.count()).select_from(AuditLog)) or 0)
        excess = total - max_rows
        if excess > 0:
            noise = int(
                db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action.in_(_UNAUTHENTICATED_ACTIONS))
                )
                or 0
            )
            if noise > 0:
                removed = _trim_oldest(
                    db, keep=max(0, noise - excess), only_actions=_UNAUTHENTICATED_ACTIONS
                )
                deleted += removed
                excess -= removed
            if excess > 0:
                deleted += _trim_oldest(db, keep=max_rows, only_actions=None)
    if deleted:
        db.commit()
    return deleted
