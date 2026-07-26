"""A schedule must fire exactly once per due window, even with two workers.

The claim used to be a bare `db.flush()`, which is invisible to other
connections until commit — so two worker processes ticking in the same minute
both saw the job as un-run and issued the power action twice.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Must be set before importing web.backend.db (engine is built at import time).
_TMP = tempfile.mkdtemp(prefix="ocibot_sched_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 's.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "sched-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "sched-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import (  # noqa: E402
    CapacityJob,
    ScheduleJobRow,
    ScheduleRun,
    Tenant,
    User,
)
from web.backend.worker import Worker  # noqa: E402


class _Result:
    def __init__(self, ok=True, message=""):
        self.ok = ok
        self.message = message
        self.data = {}
        self.work_request_id = ""


class _CountingSession:
    """Records every power action so double-fires are visible."""

    def __init__(self, calls: list[tuple[str, str]]):
        self.calls = calls

    def instance_action(self, instance_id: str, action: str):
        self.calls.append((instance_id, action))
        return _Result(True, "ok")

    def list_instances_tree(self, resolve_ips: bool = False):
        return []


class _CountingSessions:
    def __init__(self, calls: list[tuple[str, str]]):
        self.calls = calls

    def get(self, _cfg):
        return _CountingSession(self.calls)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(ScheduleRun).delete()
        db.query(ScheduleJobRow).delete()
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _seed_tenant(db) -> tuple[str, str]:
    user = User(username="stest", password_hash="x", is_admin=True)
    db.add(user)
    db.flush()
    tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
    db.add(tenant)
    db.flush()
    return user.id, tenant.id


def _tick(calls: list[tuple[str, str]], worker_id: str) -> None:
    worker = Worker()
    worker.worker_id = worker_id
    worker.sessions = _CountingSessions(calls)
    with SessionLocal() as db:
        worker.tick_schedules(db)
        db.commit()


def test_weekly_schedule_fires_once_across_two_workers():
    now_local = datetime.now().astimezone()
    with SessionLocal() as db:
        owner_id, tenant_id = _seed_tenant(db)
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
                instance_ids=["ocid1.instance.oc1..a"],
            )
        )
        db.commit()

    calls: list[tuple[str, str]] = []
    _tick(calls, "worker-1")
    _tick(calls, "worker-2")  # second worker, same due minute

    assert calls == [("ocid1.instance.oc1..a", "SOFTSTOP")], (
        f"expected exactly one power action, got {calls}"
    )
    with SessionLocal() as db:
        assert db.query(ScheduleRun).count() == 1


def test_second_worker_ticking_mid_fire_does_not_double_fire():
    """The discriminating case: worker B ticks while A is between claim and action.

    A sequential two-tick test passes either way (each tick commits at the end).
    This reproduces the actual race by running B's tick from inside A's
    _fire_schedule — exactly the window where A had only flushed its claim. With
    a flush-only claim B cannot see it and fires the same action a second time.
    """
    now_local = datetime.now().astimezone()
    with SessionLocal() as db:
        owner_id, tenant_id = _seed_tenant(db)
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
                instance_ids=["ocid1.instance.oc1..a"],
            )
        )
        db.commit()

    calls: list[tuple[str, str]] = []

    worker_a = Worker()
    worker_a.worker_id = "worker-a"
    worker_a.sessions = _CountingSessions(calls)

    original_fire = worker_a._fire_schedule
    reentered = {"done": False}

    def fire_then_let_b_tick(db, job):
        # A has claimed the job but has not finished its action yet.
        if not reentered["done"]:
            reentered["done"] = True
            worker_b = Worker()
            worker_b.worker_id = "worker-b"
            worker_b.sessions = _CountingSessions(calls)
            with SessionLocal() as db_b:
                worker_b.tick_schedules(db_b)
                db_b.commit()
        return original_fire(db, job)

    worker_a._fire_schedule = fire_then_let_b_tick
    with SessionLocal() as db_a:
        worker_a.tick_schedules(db_a)
        db_a.commit()

    assert reentered["done"], "test did not exercise the interleaving"
    # Without the committed claim this fires twice on PostgreSQL, and on SQLite
    # worker B's blocked write makes the schedule fail to fire at all — so assert
    # on the exact single action rather than just "not two".
    assert calls == [("ocid1.instance.oc1..a", "SOFTSTOP")], (
        f"expected exactly one power action across the two workers, got {calls}"
    )


def test_once_schedule_fires_once_and_disables():
    with SessionLocal() as db:
        owner_id, tenant_id = _seed_tenant(db)
        db.add(
            ScheduleJobRow(
                owner_id=owner_id,
                tenant_id=tenant_id,
                name="one-shot",
                enabled=True,
                kind="once",
                run_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                action="START",
                instance_ids=["ocid1.instance.oc1..b"],
            )
        )
        db.commit()

    calls: list[tuple[str, str]] = []
    _tick(calls, "worker-1")
    _tick(calls, "worker-2")

    assert calls == [("ocid1.instance.oc1..b", "START")]
    with SessionLocal() as db:
        row = db.query(ScheduleJobRow).one()
        assert row.enabled is False


def test_future_once_schedule_does_not_fire():
    with SessionLocal() as db:
        owner_id, tenant_id = _seed_tenant(db)
        db.add(
            ScheduleJobRow(
                owner_id=owner_id,
                tenant_id=tenant_id,
                name="later",
                enabled=True,
                kind="once",
                run_at=datetime.now(timezone.utc) + timedelta(hours=1),
                action="START",
                instance_ids=["ocid1.instance.oc1..c"],
            )
        )
        db.commit()

    calls: list[tuple[str, str]] = []
    _tick(calls, "worker-1")
    assert calls == []


def test_schedule_ownership_mismatch_is_disabled_without_action():
    """A job pointing at someone else's tenant must never touch OCI."""
    with SessionLocal() as db:
        owner_id, tenant_id = _seed_tenant(db)
        other = User(username="other", password_hash="x")
        db.add(other)
        db.flush()
        db.add(
            ScheduleJobRow(
                owner_id=other.id,  # tenant belongs to owner_id, not other
                tenant_id=tenant_id,
                name="evil",
                enabled=True,
                kind="once",
                run_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                action="STOP",
                instance_ids=["ocid1.instance.oc1..d"],
            )
        )
        db.commit()

    calls: list[tuple[str, str]] = []
    _tick(calls, "worker-1")
    assert calls == []
