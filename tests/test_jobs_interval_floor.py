"""「继续」不能把 LaunchInstance 的间隔下限清零 —— API 和 worker 两头都要有地板。

``app/scheduler.py::MIN_RETRY_INTERVAL_SEC`` 写死 60 秒，那个模块存在的全部理由
就是这条线：自动化的 LaunchInstance 必须留在 OCI 的限流指引之内，超了是运营者
自己担责。

原来这条线只有一处「执行者」：worker 候选条件里的 ``next_run_at <= now``。全文
件从来没有拿 ``last_attempt_at`` 比过一次。而
``POST /jobs/capacity/{id}/resume`` 无条件把 ``next_run_at`` 写成「现在」——
面板上 停止/继续 是同一个格子里轮换的两个按钮，又没有「立即重试」，所以
「停止 → 继续」就是用户表达「现在就再试一次」的自然手势。连点几下，
LaunchInstance 就以一个轮询周期（5 秒）的间距发出去，约 12 次/分钟。

429 冷却（``cooldown_until``）一直是生效的；没有任何兜底的恰恰是普通间隔。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_floor_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'f.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "floor-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "floor-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.routers import jobs as jobs_router  # noqa: E402
from web.backend.worker import Worker  # noqa: E402

INTERVAL = 180

_PAYLOAD = {
    "display_name": "i",
    "shape": "VM.Standard.A1.Flex",
    "ocpus": 2,
    "memory_in_gbs": 12,
    "boot_volume_size_in_gbs": 50,
    # 带上 nsg_ids，worker 就不会走 prepare_launch_network（会打 Oracle）。
    "nsg_ids": ["nsg1"],
}


class _Result:
    def __init__(self, ok=False, message="Out of host capacity"):
        self.ok = ok
        self.message = message
        self.data = {}
        self.work_request_id = ""


class _FakeSession:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def launch_from_payload(self, payload, custom_user_data="", idempotency_key=""):
        self.calls.append(str(payload.get("availability_domain") or ""))
        return _Result()

    def get_free_quota_usage(self, free_only_mode: bool = True):
        # 额度读得完整，否则 worker 会走「推迟本次尝试」的分支而不是真的去抢。
        result = _Result(True, "")
        result.data = {
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
        }
        return result


class _FakeSessions:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def get(self, _cfg):
        return _FakeSession(self.calls)


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


def _seed(db, **over) -> tuple[str, str]:
    user = User(username="floor", password_hash="x")
    db.add(user)
    db.flush()
    tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
    db.add(tenant)
    db.flush()
    job = CapacityJob(
        owner_id=user.id,
        tenant_id=tenant.id,
        name="retry",
        enabled=True,
        status="idle",
        launch_payload=dict(_PAYLOAD),
        interval_sec=INTERVAL,
        max_attempts=200,
        attempts=1,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    for key, value in over.items():
        setattr(job, key, value)
    db.add(job)
    db.commit()
    return job.id, user.id


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---- POST /jobs/capacity/{id}/resume ----


def test_resume_does_not_schedule_inside_the_interval_floor():
    last = datetime.now(timezone.utc) - timedelta(seconds=5)
    with SessionLocal() as db:
        job_id, user_id = _seed(
            db, enabled=False, status="stopped", last_attempt_at=last, next_run_at=None
        )

    with SessionLocal() as db:
        user = db.get(User, user_id)
        out = jobs_router.resume_capacity_job(job_id, user, db)

    assert out.next_run_at is not None
    scheduled = _aware(out.next_run_at)
    gap = (scheduled - last).total_seconds()
    assert gap >= INTERVAL, (
        f"继续之后下一次 LaunchInstance 距上一次只有 {gap:.0f}s，低于任务自己的 "
        f"{INTERVAL}s 间隔（合规下限 60s）"
    )


def test_resume_runs_immediately_when_the_interval_has_already_elapsed():
    """别矫枉过正：间隔早就过了的任务，「继续」必须立刻排上。"""
    last = datetime.now(timezone.utc) - timedelta(seconds=INTERVAL * 3)
    with SessionLocal() as db:
        job_id, user_id = _seed(
            db, enabled=False, status="stopped", last_attempt_at=last, next_run_at=None
        )

    with SessionLocal() as db:
        user = db.get(User, user_id)
        out = jobs_router.resume_capacity_job(job_id, user, db)

    delay = (_aware(out.next_run_at) - datetime.now(timezone.utc)).total_seconds()
    assert delay <= 2, f"间隔已过却还要再等 {delay:.0f}s"


def test_resume_of_a_never_attempted_job_runs_immediately():
    with SessionLocal() as db:
        job_id, user_id = _seed(
            db, enabled=False, status="stopped", attempts=0, last_attempt_at=None, next_run_at=None
        )

    with SessionLocal() as db:
        user = db.get(User, user_id)
        out = jobs_router.resume_capacity_job(job_id, user, db)

    delay = (_aware(out.next_run_at) - datetime.now(timezone.utc)).total_seconds()
    assert delay <= 2


# ---- worker 侧的同一道地板 ----


def _tick(job_over: dict) -> tuple[list[str], CapacityJob]:
    with SessionLocal() as db:
        job_id, _ = _seed(db, **job_over)
    calls: list[str] = []
    worker = Worker()
    worker.sessions = _FakeSessions(calls)
    with SessionLocal() as db:
        worker.tick_capacity(db)
    with SessionLocal() as db:
        return calls, db.get(CapacityJob, job_id)


def test_worker_refuses_to_launch_inside_the_interval_floor():
    """即使 next_run_at 已经到期，距上次尝试不足一个间隔就不许发。

    这是绕开 API 的兜底：任何路径（现在的 resume、以后可能新增的「立即重试」、
    直接改库）把 next_run_at 写成「现在」，都不能把 LaunchInstance 的频率抬到
    合规线以上。
    """
    last = datetime.now(timezone.utc) - timedelta(seconds=5)
    calls, job = _tick(
        {"last_attempt_at": last, "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )

    assert calls == [], "worker 在距上次尝试 5 秒时又发了一次 LaunchInstance"
    assert job.attempts == 1, "被地板拦下的一轮不该计入尝试次数"
    gap = (_aware(job.next_run_at) - last).total_seconds()
    assert gap >= INTERVAL, f"next_run_at 没有被推回合规位置（只有 {gap:.0f}s）"


def test_worker_still_launches_once_the_interval_has_elapsed():
    """回归护栏：地板不能把正常的抢机循环卡死。"""
    last = datetime.now(timezone.utc) - timedelta(seconds=INTERVAL * 2)
    calls, job = _tick(
        {"last_attempt_at": last, "next_run_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )

    assert len(calls) == 1, "间隔已过，这一轮本该真的去抢一次"
    assert job.attempts == 2


def test_worker_launches_a_job_that_has_never_been_attempted():
    calls, job = _tick({"attempts": 0, "last_attempt_at": None})
    assert len(calls) == 1
    assert job.attempts == 1
