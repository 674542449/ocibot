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

    def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
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


# ---------------------------------------------------------------------------
# 多台抢机：开出一台不能让任务收工
# ---------------------------------------------------------------------------


class _RecordingSession(_FakeSession):
    """每次开机都成功，并记下这次的名字和幂等 token。"""

    def __init__(self, calls: list, on_launch=None):
        super().__init__(_Result(True, ""))
        self._calls = calls
        self._on_launch = on_launch

    def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
        self._calls.append((payload.get("display_name"), idempotency_key))
        if self._on_launch:
            self._on_launch()
        n = len(self._calls)
        return _Result(True, "创建成功", data={"instance_id": f"ocid1.instance.oc1..n{n}"})


def _tick(sessions, job_id, *, rewind: bool = False) -> CapacityJob:
    """跑一轮 worker。rewind=True 时先把间隔拨过去，模拟「等了一个间隔」。"""
    if rewind:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        with SessionLocal() as db:
            row = db.get(CapacityJob, job_id)
            row.next_run_at = long_ago
            row.last_attempt_at = long_ago
            db.commit()
    worker = Worker()
    worker.sessions = sessions
    with SessionLocal() as db:
        worker.tick_capacity(db)
    with SessionLocal() as db:
        return db.get(CapacityJob, job_id)


def test_a_two_instance_job_keeps_going_after_the_first_success(monkeypatch):
    """用户报的 bug：开 2 台 AMD，抢到 1 台任务就结束了。"""
    import web.backend.worker as worker_mod

    notes: list[str] = []
    monkeypatch.setattr(worker_mod, "notify_user", lambda db, uid, ev, title, body: notes.append(title + "\n" + body) or [])

    calls: list = []

    class _Sessions:
        def get(self, _cfg):
            return _RecordingSession(calls)

    with SessionLocal() as db:
        owner_id, tenant_id = _seed(db)
        db.commit()
        job_id = _make_job(db, owner_id, tenant_id, target_count=2, created_count=0)

    job = _tick(_Sessions(), job_id)
    assert job.created_count == 1
    assert job.enabled is True, "开出第 1 台后任务被停掉了"
    assert job.status == "idle"
    # 下一台要隔一个完整间隔，不能在下个轮询周期就冲出去。
    assert job.next_run_at is not None
    wait = (job.next_run_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert wait > 150, wait
    assert "1/2" in notes[-1]

    # 间隔没到：再跑一轮什么也不发生。
    _tick(_Sessions(), job_id)
    assert len(calls) == 1

    job = _tick(_Sessions(), job_id, rewind=True)
    assert job.created_count == 2
    assert job.status == "success"
    assert job.enabled is False
    assert job.next_run_at is None
    assert job.success_instance_id == "ocid1.instance.oc1..n2"
    assert "2/2" in notes[-1]

    # 和直接批量创建同样的命名；每台一个不同的幂等 token，不会被 Oracle 当成重放。
    assert [name for name, _ in calls] == ["i-1", "i-2"]
    assert calls[0][1] != calls[1][1]
    with SessionLocal() as db:
        assert db.get(CapacityJob, job_id).launch_payload["display_name"] == "i"

    # 开够了就不再动。
    _tick(_Sessions(), job_id, rewind=True)
    assert len(calls) == 2


def test_a_single_instance_job_keeps_its_exact_name():
    calls: list = []

    class _Sessions:
        def get(self, _cfg):
            return _RecordingSession(calls)

    with SessionLocal() as db:
        owner_id, tenant_id = _seed(db)
        db.commit()
        job_id = _make_job(db, owner_id, tenant_id)

    job = _tick(_Sessions(), job_id)
    assert (job.status, job.created_count, job.target_count) == ("success", 1, 1)
    assert calls[0][0] == "i"


def test_stop_during_a_partial_success_stays_stopped():
    """第 1/2 台开出来的同时用户点了「停止」：这台要记上，任务不能被写回「等待中」。"""
    calls: list = []
    holder: dict = {}

    def _user_presses_stop():
        with SessionLocal() as other:
            row = other.get(CapacityJob, holder["id"])
            row.enabled = False
            row.status = "stopped"
            other.commit()

    class _Sessions:
        def get(self, _cfg):
            return _RecordingSession(calls, on_launch=_user_presses_stop)

    with SessionLocal() as db:
        owner_id, tenant_id = _seed(db)
        db.commit()
        holder["id"] = _make_job(db, owner_id, tenant_id, target_count=2)

    job = _tick(_Sessions(), holder["id"])
    assert job.created_count == 1
    assert job.enabled is False
    assert job.status == "stopped"
    assert job.next_run_at is None


# ---------------------------------------------------------------------------
# 多台 + 密码模式：自动生成的密码每台一个
# ---------------------------------------------------------------------------


class _PasswordSession(_FakeSession):
    """按脚本返回成功 / 容量不足，并记下每次开机用的 root 密码。"""

    def __init__(self, calls: list, script: list):
        super().__init__(_Result(True, ""))
        self._calls = calls
        self._script = script

    def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
        self._calls.append(root_password)
        if self._script.pop(0):
            return _Result(True, "创建成功", data={"instance_id": f"ocid1.instance.oc1..p{len(self._calls)}"})
        return _Result(False, "Out of host capacity.")


def _password_job(*, generated: bool) -> str:
    from web.backend.crypto_util import encrypt_text

    with SessionLocal() as db:
        owner_id, tenant_id = _seed(db)
        db.commit()
        return _make_job(
            db,
            owner_id,
            tenant_id,
            target_count=2,
            launch_payload={
                "display_name": "pw",
                "shape": "VM.Standard.A1.Flex",
                "ocpus": 1,
                "memory_in_gbs": 6,
                "boot_volume_size_in_gbs": 50,
                "nsg_ids": ["nsg1"],
                "auth_mode": "password",
            },
            root_password_encrypted=encrypt_text("First-Pass-2345"),
            root_password_generated=generated,
        )


def test_generated_passwords_differ_per_instance_and_stay_stable_across_attempts():
    """一台的密码泄露不该连带另一台；而同一台的多次尝试必须用同一个密码 ——
    否则崩溃后用同一个 retry token 重放时，请求体对不上。"""
    from web.backend.crypto_util import decrypt_text

    calls: list = []
    # 第 1 台成功 → 第 2 台容量不足一次 → 第 2 台成功
    script = [True, False, True]

    class _Sessions:
        def get(self, _cfg):
            return _PasswordSession(calls, script)

    job_id = _password_job(generated=True)
    _tick(_Sessions(), job_id)
    with SessionLocal() as db:
        next_pw = decrypt_text(db.get(CapacityJob, job_id).root_password_encrypted)
    _tick(_Sessions(), job_id, rewind=True)
    job = _tick(_Sessions(), job_id, rewind=True)

    assert job.status == "success" and job.created_count == 2
    first, miss, second = calls
    assert first == "First-Pass-2345"
    assert second != first, "两台用了同一个自动生成的密码"
    assert miss == second == next_pw, "同一台的几次尝试用了不同的密码"
    assert len(second) >= 12


def test_an_operator_supplied_password_is_shared_by_every_instance():
    calls: list = []
    script = [True, True]

    class _Sessions:
        def get(self, _cfg):
            return _PasswordSession(calls, script)

    job_id = _password_job(generated=False)
    _tick(_Sessions(), job_id)
    _tick(_Sessions(), job_id, rewind=True)
    assert calls == ["First-Pass-2345", "First-Pass-2345"]
