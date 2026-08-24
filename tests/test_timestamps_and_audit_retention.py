"""UTC timestamps on the wire, and what the audit row cap is allowed to evict.

Two defects that both live in how this app treats time.

1. Routes that hand-build a response dict serialized `created_at` with a bare
   `.isoformat()`. SQLite returns `DateTime(timezone=True)` values naive, so the
   string carried no offset, and per ES2015 `new Date(s)` parses an offset-less
   date-time as LOCAL time — a UTC+8 operator read every audit event eight hours
   early, on the one page whose entire purpose is when something happened.
   `schemas.UtcDatetime` already fixed this for the routes with a response_model;
   three hand-built dicts were missed.

2. `prune_audit_log` trimmed strictly by age, and unauthenticated requests can write
   `auth.login_failed` / `auth.login_blocked` rows without limit. So anyone able to
   reach /auth/login could choose what this table forgets: flood past the cap, wait
   for the hourly prune, and the tenant/2FA/login history is gone — including the
   evidence of whatever the flood was covering. Plus the retention cutoff was sent
   naive, which on Postgres `timestamptz` is cast with the session TimeZone GUC.

(Named test_timestamps_* because that is the filename this change is allowed to add.)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_tsaudit_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 't.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "tsaudit-master-key-0123456789abcd")
os.environ.setdefault("OCIBOT_JWT_SECRET", "tsaudit-jwt-secret-0123456789abcd")

pytest.importorskip("fastapi")

from pydantic import BaseModel  # noqa: E402

from web.backend.audit import iso_utc, prune_audit_log, retention_cutoff  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import AuditLog, NotificationChannel, SshHostKey, User  # noqa: E402
from web.backend.schemas import UtcDatetime  # noqa: E402

_NAIVE = datetime(2026, 8, 23, 14, 23, 48, 56754)


def _has_offset(value: str) -> bool:
    """What the SPA needs: anything else is parsed as the viewer's local time."""
    return value.endswith("Z") or value[-6:-3] in ("+0", "-0") or value.endswith("+00:00")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


@pytest.fixture
def user():
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == "ts-audit-user").one_or_none()
        if row is None:
            row = User(username="ts-audit-user", password_hash="x", is_admin=False)
            db.add(row)
            db.commit()
        db.refresh(row)
        db.expunge(row)
        return row


# ---------------------------------------------------------------------------
# 1. Every timestamp on the wire carries an offset
# ---------------------------------------------------------------------------


def test_iso_utc_matches_what_a_schema_field_emits():
    """The three hand-built dicts must not drift from UserOut/TenantOut again."""

    class _M(BaseModel):
        when: UtcDatetime

    assert iso_utc(_NAIVE) == json.loads(_M(when=_NAIVE).model_dump_json())["when"]
    assert _has_offset(iso_utc(_NAIVE))
    assert iso_utc(None) == ""


def test_an_already_aware_timestamp_is_left_alone():
    aware = _NAIVE.replace(tzinfo=timezone(timedelta(hours=8)))
    parsed = datetime.fromisoformat(iso_utc(aware).replace("Z", "+00:00"))
    assert parsed == aware  # same instant, not re-labelled


def test_the_audit_endpoint_timestamps_are_utc(user):
    from web.backend.routers.audit import list_audit

    with SessionLocal() as db:
        db.add(
            AuditLog(
                owner_id=user.id,
                action="auth.login_failed",
                target="ts-marker",
                detail="{}",
                created_at=_NAIVE,
            )
        )
        db.commit()
        rows = list_audit(user=user, db=db, limit=200, auth_only=False)
    marked = [r for r in rows if r["target"] == "ts-marker"]
    assert marked, "seeded row not returned"
    value = marked[0]["created_at"]
    assert _has_offset(value), f"{value!r} is parsed as LOCAL time by the SPA"
    assert datetime.fromisoformat(value.replace("Z", "+00:00")) == _NAIVE.replace(
        tzinfo=timezone.utc
    )


def test_the_notification_channel_timestamp_is_utc():
    from web.backend.routers.notifications import _out

    row = NotificationChannel(
        id="c1",
        owner_id="o1",
        kind="webhook",
        name="ch",
        enabled=True,
        config_encrypted="",
        events=["capacity"],
        created_at=_NAIVE,
    )
    assert _has_offset(_out(row).created_at)


def test_the_host_key_timestamp_is_utc(user, monkeypatch):
    """"First seen" on a pinned host key is forensic evidence of a MITM window."""
    from web.backend.routers import instance_ops

    monkeypatch.setattr(instance_ops, "_row", lambda db, user_id, tenant_id: None)
    with SessionLocal() as db:
        db.query(SshHostKey).filter(SshHostKey.owner_id == user.id).delete()
        db.add(
            SshHostKey(
                owner_id=user.id,
                tenant_id="t1",
                instance_id="ocid1.instance.oc1..ts",
                port=22,
                fingerprint="SHA256:abc",
                key_type="ssh-ed25519",
                last_host="203.0.113.7",
                created_at=_NAIVE,
            )
        )
        db.commit()
        out = instance_ops.get_host_key(
            tenant_id="t1", instance_id="ocid1.instance.oc1..ts", user=user, db=db
        )
    assert _has_offset(out["items"][0]["created_at"])


# ---------------------------------------------------------------------------
# 2. The row cap must not be a delete primitive for the flooder
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_audit():
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()
    yield


def _seed(action: str, count: int, *, age_days: float, target: str) -> None:
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    with SessionLocal() as db:
        for i in range(count):
            db.add(
                AuditLog(
                    owner_id=None,
                    action=action,
                    target=f"{target}-{i}",
                    detail="{}",
                    created_at=base + timedelta(microseconds=i),
                )
            )
        db.commit()


def _actions() -> list[str]:
    with SessionLocal() as db:
        return [r.action for r in db.query(AuditLog).all()]


def test_a_login_flood_cannot_evict_real_history(clean_audit):
    """The attack in order: the history exists first, then the flood arrives. Keeping
    strictly the newest rows meant the flood *was* the newest rows, so the prune
    deleted everything that came before it — 90 unauthenticated requests wrote 90
    rows, so reaching the 50 000 cap is a script, not an effort."""
    _seed("tenant.create", 20, age_days=0.5, target="real")
    _seed("auth.totp_failed", 5, age_days=0.5, target="totp")
    _seed("auth.login_failed", 200, age_days=0.1, target="flood")

    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=50)

    actions = _actions()
    assert actions.count("tenant.create") == 20, "real history was evicted by login noise"
    # The password was already correct for these — the highest-signal row in the log.
    assert actions.count("auth.totp_failed") == 5
    assert len(actions) <= 50


def test_the_cap_is_still_a_disk_guard(clean_audit):
    _seed("auth.login_blocked", 300, age_days=0.2, target="blocked")
    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=40)
    assert len(_actions()) == 40


def test_real_history_is_trimmed_only_once_there_is_no_noise_left(clean_audit):
    """A table genuinely full of real events still cannot fill the disk."""
    _seed("tenant.create", 60, age_days=0.2, target="real")
    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=10)
    assert len(_actions()) == 10


def test_the_newest_login_noise_is_what_survives(clean_audit):
    """Recent failures are the ones an operator investigates."""
    _seed("auth.login_failed", 30, age_days=0.2, target="old")
    _seed("auth.login_failed", 30, age_days=0.1, target="new")
    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=30)
    with SessionLocal() as db:
        targets = {r.target for r in db.query(AuditLog).all()}
    assert all(t.startswith("new-") for t in targets)


def test_retention_cutoff_is_timezone_aware():
    """On Postgres created_at is timestamptz and a naive literal is cast with the
    session TimeZone GUC, so on a server left at Asia/Shanghai the 180-day window
    quietly became 180 days minus eight hours. SQLite's bind processor ignores
    tzinfo, so carrying the offset costs nothing there."""
    cutoff = retention_cutoff(180)
    assert cutoff.tzinfo is not None
    assert cutoff.utcoffset() == timedelta(0)


def test_the_window_still_deletes_old_rows(clean_audit):
    _seed("tenant.create", 4, age_days=400, target="ancient")
    _seed("tenant.create", 3, age_days=1, target="recent")
    with SessionLocal() as db:
        assert prune_audit_log(db, retention_days=180, max_rows=0) == 4
    assert len(_actions()) == 3
