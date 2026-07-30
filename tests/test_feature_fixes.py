"""Regressions for the per-feature verification pass."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_feat_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'f.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "feat-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "feat-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import (  # noqa: E402
    CapacityJob,
    NotificationChannel,
    Tenant,
    User,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(CapacityJob).delete()
        db.query(NotificationChannel).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _seed() -> tuple[str, str]:
    with SessionLocal() as db:
        user = User(username="feat", password_hash="x", is_admin=True)
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.commit()
        return user.id, tenant.id


# ---------------------------------------------------------------------------
# Admin: never offer / accept a self password reset
# ---------------------------------------------------------------------------


def test_admin_cannot_reset_own_password():
    """It revoked the session and cleared 2FA while returning the only copy of the
    new password — a lone admin who missed it had no recovery path."""
    from web.backend.routers.admin import reset_password

    owner_id, _ = _seed()
    with SessionLocal() as db:
        admin = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            reset_password(admin.id, admin, db)
    assert exc.value.status_code == 400
    assert "设置" in exc.value.detail


def test_admin_can_still_reset_another_user():
    from web.backend.routers.admin import reset_password

    owner_id, _ = _seed()
    with SessionLocal() as db:
        other = User(username="victim", password_hash="x")
        db.add(other)
        db.commit()
        other_id = other.id
    with SessionLocal() as db:
        admin = db.get(User, owner_id)
        out = reset_password(other_id, admin, db)
    assert out["new_password"]
    assert out["username"] == "victim"


# ---------------------------------------------------------------------------
# Capacity retry: resume must not silently no-op
# ---------------------------------------------------------------------------


def _make_job(owner_id: str, tenant_id: str, **over) -> str:
    with SessionLocal() as db:
        job = CapacityJob(
            owner_id=owner_id,
            tenant_id=tenant_id,
            name="retry",
            enabled=False,
            status="stopped",
            launch_payload={"shape": "VM.Standard.A1.Flex"},
            interval_sec=180,
            max_attempts=200,
            attempts=0,
        )
        for k, v in over.items():
            setattr(job, k, v)
        db.add(job)
        db.commit()
        return job.id


def test_resume_rejects_an_exhausted_job():
    """Resuming at attempts >= max is re-failed by the worker instantly, so the
    button reported success and changed nothing."""
    from web.backend.routers.jobs import resume_capacity_job

    owner_id, tenant_id = _seed()
    job_id = _make_job(owner_id, tenant_id, attempts=200, max_attempts=200, status="failed")
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            resume_capacity_job(job_id, user, db)
    assert exc.value.status_code == 400
    assert "最大重试次数" in exc.value.detail


def test_resume_works_for_a_normal_stopped_job():
    """The common case must still work — a guard that rejects it would be worse."""
    from web.backend.routers.jobs import resume_capacity_job

    owner_id, tenant_id = _seed()
    job_id = _make_job(owner_id, tenant_id, attempts=5)
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        out = resume_capacity_job(job_id, user, db)
    assert out.enabled is True
    assert out.status == "idle"


# ---------------------------------------------------------------------------
# Notifications: an empty event selection means none
# ---------------------------------------------------------------------------


def test_empty_event_selection_is_preserved():
    from web.backend.routers.notifications import _clean_events

    assert _clean_events([]) == []
    assert _clean_events(["bogus"]) == []
    assert _clean_events(["capacity"]) == ["capacity"]


def test_notify_respects_an_empty_event_list():
    import web.backend.notify as notify_mod

    sent: list[str] = []

    class _Row:
        def __init__(self, events):
            self.name = "ch"
            self.kind = "webhook"
            self.events = events
            self.config_encrypted = ""

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self, _stmt):
            class _R:
                def all(inner):
                    return self._rows

            return _R()

    notify_mod.send_to_channel = lambda kind, cfg, t, b: (sent.append(t), (True, "sent"))[1]

    # Explicitly empty -> nothing sent.
    sent.clear()
    notify_mod.notify_user(_DB([_Row([])]), "o", "capacity", "t", "b")
    assert sent == [], "an empty selection must mean no notifications"

    # Legacy NULL (column added later) -> still receives everything.
    sent.clear()
    notify_mod.notify_user(_DB([_Row(None)]), "o", "capacity", "t", "b")
    assert len(sent) == 1, "rows predating the events column must keep working"

    # Subscribed -> sent; not subscribed -> skipped.
    sent.clear()
    notify_mod.notify_user(_DB([_Row(["capacity"])]), "o", "capacity", "t", "b")
    assert len(sent) == 1
    sent.clear()
    notify_mod.notify_user(_DB([_Row(["some-other-event"])]), "o", "capacity", "t", "b")
    assert sent == []




# ---------------------------------------------------------------------------
# Admin list timestamps
# ---------------------------------------------------------------------------


def test_admin_user_list_timestamps_carry_utc():
    from web.backend.routers.admin import AdminUserOut

    out = AdminUserOut(
        id="u",
        username="x",
        is_active=True,
        is_admin=True,
        totp_enabled=False,
        tenant_count=0,
        created_at=datetime(2026, 7, 27, 12, 0, 0),  # naive, as SQLite returns
    )
    assert out.created_at is not None
    assert out.created_at.utcoffset() == timedelta(0)
