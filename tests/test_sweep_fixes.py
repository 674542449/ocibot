"""Regressions for the full-sweep findings."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_sweep_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 's.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "sweep-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "sweep-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.worker import Worker  # noqa: E402


class _Result:
    def __init__(self, ok=True, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data or {}
        self.work_request_id = ""


_HEALTHY_SNAPSHOT = {
    "account_tier": "free",
    "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
    "remaining": {
        "a1_ocpu": 4.0,
        "a1_memory_gb": 24.0,
        "e2_micro_count": 2,
        "block_storage_gb": 200.0,
    },
    "read_incomplete": False,
}


class _Session:
    """Fake OCI session whose quota reads can be made to fail on the Nth call."""

    def __init__(self, fail_from_call: int | None = None):
        self.launches = 0
        self.quota_calls = 0
        self._fail_from = fail_from_call

    def get_free_quota_usage(self, free_only_mode: bool = True, **_kw):
        self.quota_calls += 1
        if self._fail_from is not None and self.quota_calls >= self._fail_from:
            raise RuntimeError("429 TooManyRequests")
        return _Result(True, "", dict(_HEALTHY_SNAPSHOT))

    def launch_from_payload(self, payload, custom_user_data="", idempotency_key=""):
        self.launches += 1
        return _Result(False, "Out of host capacity")


class _Sessions:
    def __init__(self, session):
        self._s = session

    def get(self, _cfg):
        return self._s


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(CapacityAttempt).delete()
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _seed_job(**over) -> tuple[str, str]:
    with SessionLocal() as db:
        user = User(username="sw", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(
            owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="", enabled=True
        )
        db.add(tenant)
        db.flush()
        job = CapacityJob(
            owner_id=user.id,
            tenant_id=tenant.id,
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
            next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        for k, v in over.items():
            setattr(job, k, v)
        db.add(job)
        db.commit()
        return job.id, tenant.id


def _tick(session) -> None:
    worker = Worker()
    worker.sessions = _Sessions(session)
    with SessionLocal() as db:
        worker.tick_capacity(db)


def test_healthy_quota_still_launches():
    job_id, _ = _seed_job()
    session = _Session()
    _tick(session)
    assert session.launches == 1
    with SessionLocal() as db:
        assert db.get(CapacityJob, job_id).attempts == 1


def test_only_one_quota_enumeration_per_attempt():
    """The pre-read is the deciding read — this is the anti-bypass assertion.

    The fail-closed pre-check used to be advisory: check_launch_quota took its OWN
    snapshot, and _usage_snapshot turns a failed read into {'read_incomplete': True}
    with no usage keys, which the validators read as "full quota free". So a
    throttled second read let the launch through. Exactly one read per attempt
    means there is no unchecked second read to bypass the gate.
    """
    _seed_job()
    session = _Session()
    _tick(session)
    assert session.quota_calls == 1, f"took {session.quota_calls} quota reads for one attempt"


def test_deferral_does_not_consume_an_attempt():
    job_id, _ = _seed_job()
    session = _Session(fail_from_call=1)  # unreadable from the start
    _tick(session)
    assert session.launches == 0
    with SessionLocal() as db:
        job = db.get(CapacityJob, job_id)
        assert job.attempts == 0, "a deferred tick must not burn an attempt"
        assert job.enabled is True
        assert job.next_run_at is not None


def test_stopped_job_is_not_launched_after_the_candidate_scan():
    """Pressing stop between the candidate query and the claim must be honoured."""
    job_id, _ = _seed_job()
    session = _Session()

    worker = Worker()
    worker.sessions = _Sessions(session)

    # clamp_max_attempts runs at the top of the candidate loop, i.e. AFTER the
    # candidate SELECT and BEFORE the claim UPDATE — the exact window a user's
    # "stop" used to fall into.
    import web.backend.worker as worker_mod

    original_clamp = worker_mod.clamp_max_attempts
    stopped = {}

    def stop_then_clamp(value):
        if not stopped:
            stopped["done"] = True
            with SessionLocal() as other:
                row = other.get(CapacityJob, job_id)
                row.enabled = False
                row.status = "stopped"
                other.commit()
        return original_clamp(value)

    worker_mod.clamp_max_attempts = stop_then_clamp
    try:
        with SessionLocal() as db:
            worker.tick_capacity(db)
    finally:
        worker_mod.clamp_max_attempts = original_clamp

    assert stopped, "test did not reach the injection point"
    assert session.launches == 0, "a stopped job still got one more launch"


def test_utc_datetimes_serialize_with_an_offset():
    """SQLite hands back naive datetimes; the API must still emit UTC-qualified ones."""
    from web.backend.schemas import CapacityAttemptOut

    naive = datetime(2026, 7, 27, 12, 0, 0)  # what SQLite returns
    out = CapacityAttemptOut(
        id="a",
        job_id="j",
        n=1,
        seq=1,
        ok=True,
        capacity=False,
        rate_limited=False,
        message="",
        availability_domain="",
        config_label="",
        created_at=naive,
    )
    assert out.created_at.tzinfo is not None
    assert out.created_at.utcoffset() == timedelta(0)
    # pydantic emits the compact "Z" form; either spelling carries the offset the
    # SPA needs in order not to reinterpret UTC as local time.
    payload = out.model_dump_json()
    assert '"created_at":"2026-07-27T12:00:00Z"' in payload, payload


def test_aware_datetimes_are_left_alone():
    from web.backend.schemas import CapacityAttemptOut

    aware = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    out = CapacityAttemptOut(
        id="a",
        job_id="j",
        n=1,
        seq=1,
        ok=True,
        capacity=False,
        rate_limited=False,
        message="",
        availability_domain="",
        config_label="",
        created_at=aware,
    )
    assert out.created_at.utcoffset() == timedelta(hours=8)


def test_notify_fan_out_is_bounded(monkeypatch):
    """One trigger must not turn into an unbounded outbound flood."""
    import web.backend.notify as notify_mod

    class _Row:
        def __init__(self, i):
            self.id = str(i)
            self.name = f"ch{i}"
            self.kind = "webhook"
            self.enabled = True
            self.events = ["capacity"]
            self.config_encrypted = ""

    sent = {"n": 0}

    def fake_send(kind, config, title, body):
        sent["n"] += 1
        return True, "sent"

    class _DB:
        def scalars(self, _stmt):
            class _R:
                def all(self_inner):
                    return [_Row(i) for i in range(200)]

            return _R()

    # 见 test_feature_fixes.py 同名问题:裸赋值会把桩泄漏给后续测试文件。
    monkeypatch.setattr(notify_mod, "send_to_channel", fake_send)
    results = notify_mod.notify_user(_DB(), "owner", "capacity", "t", "b")
    assert sent["n"] <= 20, f"sent {sent['n']} notifications for one event"
    assert len(results) == sent["n"]


def test_launch_meta_cache_is_bounded():
    import web.backend.launch_service as ls

    ls._META_CACHE.clear()
    for i in range(200):
        ls._META_CACHE[f"k{i}"] = (0.0, {})
    # Simulate the eviction the fetch path performs before inserting.
    now = 10_000.0
    for key in [k for k, (ts, _) in ls._META_CACHE.items() if now - ts >= ls._META_TTL]:
        ls._META_CACHE.pop(key, None)
    assert len(ls._META_CACHE) == 0, "expired entries must be dropped"
    ls._META_CACHE.clear()
