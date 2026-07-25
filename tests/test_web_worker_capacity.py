"""Integration tests for the web capacity-retry worker (compliance-critical).

Drives Worker.tick_capacity against a temp SQLite DB with a fake OCI session, so
we verify the durable lease, per-attempt commit, 60s interval floor, 429 backoff
and max-attempts stop without any live OCI call.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Must be set before importing web.backend.db (engine is built at import time).
_TMP = tempfile.mkdtemp(prefix="ocibot_worker_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'w.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "worker-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "worker-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")  # web deps

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.worker import Worker  # noqa: E402


class _Result:
    def __init__(self, ok, message, data=None):
        self.ok = ok
        self.message = message
        self.data = data or {}
        self.work_request_id = ""


class _FakeSession:
    def __init__(self, result: _Result):
        self._result = result

    def launch_from_payload(self, payload, custom_user_data=""):
        return self._result

    def get_free_quota_usage(self, free_only_mode: bool = True):
        # Plenty of free remaining so the worker quota guard does not block tests.
        return _Result(
            True,
            "",
            {
                "account_tier": "free",
                "usage": {
                    "a1_ocpu": 0.0,
                    "a1_memory_gb": 0.0,
                    "e2_micro_count": 0,
                    "block_storage_gb": 0.0,
                },
                "remaining": {
                    "a1_ocpu": 4.0,
                    "a1_memory_gb": 24.0,
                    "e2_micro_count": 2,
                    "block_storage_gb": 200.0,
                },
            },
        )


class _FakeSessions:
    def __init__(self, result: _Result):
        self._result = result

    def get(self, _cfg):
        return _FakeSession(self._result)


def _seed(db) -> tuple[str, str]:
    user = User(username="wtest", password_hash="x", is_admin=True)
    db.add(user)
    db.flush()
    tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
    db.add(tenant)
    db.flush()
    return user.id, tenant.id


def _make_job(db, owner_id, tenant_id, **over) -> str:
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    job = CapacityJob(
        owner_id=owner_id,
        tenant_id=tenant_id,
        name="retry",
        enabled=True,
        status="idle",
        launch_payload={
            "display_name": "i",
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 2,
            "memory_in_gbs": 12,
            "boot_volume_size_in_gbs": 50,
            "nsg_ids": ["nsg1"],
        },
        interval_sec=180,
        max_attempts=200,
        attempts=0,
        next_run_at=past,
    )
    for k, v in over.items():
        setattr(job, k, v)
    db.add(job)
    db.commit()
    return job.id


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        # clean slate between tests
        db.query(CapacityAttempt).delete()
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _run(result: _Result, **job_over) -> CapacityJob:
    with SessionLocal() as db:
        owner_id, tenant_id = _seed(db)
        db.commit()
        job_id = _make_job(db, owner_id, tenant_id, **job_over)
    worker = Worker()
    worker.sessions = _FakeSessions(result)  # bypass real OCI client
    with SessionLocal() as db:
        worker.tick_capacity(db)
    with SessionLocal() as db:
        return db.get(CapacityJob, job_id)


def test_capacity_miss_reschedules_beyond_60s_floor_and_releases_lease():
    job = _run(_Result(False, "Out of host capacity"))
    assert job.attempts == 1
    assert job.status == "idle"  # keeps retrying
    assert job.enabled is True
    # Interval floor honored: next attempt is >= 60s out (here ~180s).
    delay = (job.next_run_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert delay >= 60, f"next_run_at only {delay:.0f}s out — violates 60s floor"
    # Lease released, durably.
    assert job.locked_by is None and job.locked_until is None
    with SessionLocal() as db:
        n = db.query(CapacityAttempt).filter(CapacityAttempt.job_id == job.id).count()
        assert n == 1


def test_rate_limit_sets_cooldown_and_keeps_idle():
    job = _run(_Result(False, "TooManyRequests: 429 rate limited"))
    assert job.attempts == 1
    assert job.status == "idle"
    assert int(job.consecutive_rate_limits) == 1
    assert job.cooldown_until is not None
    # cooldown pushes next_run_at into the future
    assert job.next_run_at is not None


def test_max_attempts_stops_before_calling_api():
    # attempts already at the cap -> job must stop, not launch.
    job = _run(_Result(True, "should not be used"), attempts=200)
    assert job.enabled is False
    assert job.status == "failed"
    assert job.attempts == 200  # unchanged; no extra attempt made


def test_success_marks_job_done():
    job = _run(_Result(True, "created ocid1.instance.oc1..abc", data={"instance_id": "ocid1.instance.oc1..abc"}))
    assert job.status == "success"
    assert job.enabled is False
    assert job.success_instance_id.startswith("ocid1.instance.")
    assert job.next_run_at is None
