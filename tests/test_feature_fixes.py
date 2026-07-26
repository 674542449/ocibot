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
    ScheduleJobRow,
    ScheduleRun,
    Tenant,
    User,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(ScheduleRun).delete()
        db.query(ScheduleJobRow).delete()
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
# Schedules: reject a time that can never match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["24:00", "99:99", "7:00", "07:60", "0700", "aa:bb"])
def test_weekly_schedule_rejects_unmatchable_time(bad):
    """The worker matches strftime('%H:%M'); anything else never fires."""
    from web.backend.routers.jobs import create_schedule
    from web.backend.schemas import ScheduleJobCreate

    owner_id, tenant_id = _seed()
    body = ScheduleJobCreate(
        tenant_id=tenant_id, name="s", kind="weekly", time_of_day=bad, weekdays=[0]
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            create_schedule(body, user, db)
    assert exc.value.status_code == 400
    assert "HH:MM" in exc.value.detail


@pytest.mark.parametrize("good", ["00:00", "07:05", "23:59", "12:30"])
def test_weekly_schedule_accepts_valid_times(good):
    from web.backend.routers.jobs import create_schedule
    from web.backend.schemas import ScheduleJobCreate

    owner_id, tenant_id = _seed()
    body = ScheduleJobCreate(
        tenant_id=tenant_id, name="s", kind="weekly", time_of_day=good, weekdays=[0]
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        out = create_schedule(body, user, db)
    assert out.time_of_day == good


def test_failed_schedule_fire_is_recorded():
    """A raising fire left no history row, so the run vanished from 运行历史."""
    from web.backend.worker import Worker

    owner_id, tenant_id = _seed()
    now_local = datetime.now().astimezone()
    with SessionLocal() as db:
        db.add(
            ScheduleJobRow(
                owner_id=owner_id,
                tenant_id=tenant_id,
                name="nightly",
                enabled=True,
                kind="weekly",
                time_of_day=now_local.strftime("%H:%M"),
                weekdays=[now_local.weekday()],
                action="SOFTSTOP",
                instance_ids=["i1"],
            )
        )
        db.commit()

    worker = Worker()

    def boom(db, job):
        raise RuntimeError("OCI 认证失败")

    worker._fire_schedule = boom
    with SessionLocal() as db:
        worker.tick_schedules(db)
        db.commit()

    with SessionLocal() as db:
        runs = db.query(ScheduleRun).all()
        assert len(runs) == 1, "a failed fire must still leave a history row"
        assert runs[0].ok is False
        assert "OCI" in runs[0].message


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
    notify_mod.notify_user(_DB([_Row(["schedule"])]), "o", "capacity", "t", "b")
    assert sent == []


# ---------------------------------------------------------------------------
# Backup: budget survives a round trip
# ---------------------------------------------------------------------------


def test_backup_round_trip_preserves_budget():
    """budget_monthly_usd was absent from the export, so restores lost budget alerts.

    Driven through the real ASGI stack: export returns a StreamingResponse, and the
    import path is a multipart upload.
    """
    import io
    import json

    pyzipper = pytest.importorskip("pyzipper")
    from fastapi.testclient import TestClient

    from web.backend.auth import hash_password
    from web.backend.crypto_util import encrypt_text
    from web.backend.main import app

    # A real key is required: the import validates via TenantConfig and skips a
    # tenant without one.
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
        "-----END PRIVATE KEY-----"
    )

    with SessionLocal() as db:
        user = User(username="bkfeat", password_hash=hash_password("supersecret123"))
        db.add(user)
        db.flush()
        db.add(
            Tenant(
                owner_id=user.id,
                name="T-budget",
                region="ap-tokyo-1",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(pem),
                budget_monthly_usd=42.5,
            )
        )
        db.commit()

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "bkfeat", "password": "supersecret123"})
        assert r.status_code == 200, r.text
        r = c.post("/api/backup/export", json={"password": "hunter22"})
        assert r.status_code == 200, r.text
        blob = r.content

        with pyzipper.AESZipFile(io.BytesIO(blob), "r") as zf:
            zf.setpassword(b"hunter22")
            payload = json.loads(zf.read("tenants.json"))
        exported = payload["tenants"][0]
        assert exported["budget_monthly_usd"] == 42.5, exported

        # And a hostile value in the archive must be coerced, not crash the import.
        exported["budget_monthly_usd"] = "not-a-number"
        exported["name"] = "T-restored"
        buf = io.BytesIO()
        with pyzipper.AESZipFile(
            buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(b"hunter22")
            zf.writestr("tenants.json", json.dumps({"version": 1, "tenants": [exported]}))
        r = c.post(
            "/api/backup/import",
            data={"password": "hunter22"},
            files={"file": ("b.zip", buf.getvalue(), "application/zip")},
        )
        assert r.status_code == 200, r.text

    with SessionLocal() as db:
        restored = db.query(Tenant).filter(Tenant.name == "T-restored").one_or_none()
        assert restored is not None, "hostile budget must not skip the tenant"
        assert restored.budget_monthly_usd == 0.0


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
