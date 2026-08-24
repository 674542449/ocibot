"""写尝试日志失败，绝不能把调用方的事务一起带走。

``Worker._log_attempt`` 原来是「一个 try + log.exception」。在 PostgreSQL 上这远
远不够：一条语句失败之后整个事务进入 aborted 状态，后面每一条语句都抛
``InFailedSqlTransaction`` / ``PendingRollbackError``。也就是说被吞掉的那个异常
会以 ``_handle_capacity_error`` / ``_notify_capacity_end`` 崩掉的形式重新出现，
而同一个事务里的 ``attempts += 1`` 也一起回滚 —— ``max_attempts`` 那道上限从此
永远够不到，租约一过期任务就重新认领、再发一次 ``LaunchInstance``。这个文件为了
合规而维护的尝试次数上限，就是这样被一条日志写入绕过的。

真实触发器：``availability_domains`` 里一个 200 字符的 AD。Oracle 拒绝这个 AD
（正常），然后 worker 把它原样写进 ``CapacityAttempt.availability_domain``
``String(128)``，flush 抛 ``DataError``。

所以两道防线都要在：按列宽截断（下面第一组用例，SQLite 上也能验），以及用
SAVEPOINT 隔离这次写入（第二组，任何 DB 错误都只回滚它自己）。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_attemptlog_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "attempt-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "attempt-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.worker import Worker  # noqa: E402


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


def _seed(db) -> CapacityJob:
    user = User(username="alog", password_hash="x")
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
        status="running",
        launch_payload={"display_name": "i", "shape": "VM.Standard.A1.Flex"},
        interval_sec=180,
        max_attempts=200,
        attempts=0,
    )
    db.add(job)
    db.commit()
    return job


def _width(column: str) -> int:
    return int(CapacityAttempt.__table__.c[column].type.length)


def test_attempt_log_fields_are_written_within_their_column_widths():
    """断言写进去的**值**符合列宽，而不是「插入没报错」。

    SQLite 不检查 varchar 宽度，所以「插入成功」这种断言在这台机器上永远是绿的，
    哪怕 PostgreSQL 上同一行正在抛 DataError。
    """
    ad_width, label_width = _width("availability_domain"), _width("config_label")
    with SessionLocal() as db:
        job = _seed(db)
        job.attempts = 1
        Worker._log_attempt(
            Worker.__new__(Worker),
            db,
            job,
            ok=False,
            message="Out of host capacity",
            ad="A" * (ad_width + 100),
            config_label="C" * (label_width + 100),
        )
        db.commit()
        row = db.query(CapacityAttempt).filter(CapacityAttempt.job_id == job.id).one()
        assert len(row.availability_domain) <= ad_width, (
            f"availability_domain 写了 {len(row.availability_domain)} 字符，"
            f"列只有 {ad_width} —— PostgreSQL 上这一 flush 就把事务打成 aborted"
        )
        assert len(row.config_label) <= label_width


def test_a_failed_attempt_log_does_not_roll_back_the_attempt_counter():
    """日志写挂了，attempts 和后续的状态写入都必须活下来。

    ``attempts`` 被回滚 = ``max_attempts`` 永远够不到 = 任务无限重发
    ``LaunchInstance``，正好抵消掉整个文件为合规做的努力。
    """
    import web.backend.worker as worker_mod

    real_attempt = worker_mod.CapacityAttempt

    def poisoned(**kwargs):
        # 外键指向一个不存在的任务 -> flush 时 IntegrityError。
        # 站位于 PostgreSQL 上那条 200 字符 AD 触发的 DataError：都是「flush 炸了，
        # 事务进入不可用状态」这同一类故障。
        kwargs["job_id"] = "no-such-job"
        return real_attempt(**kwargs)

    with SessionLocal() as db:
        job = _seed(db)
        job_id = job.id
        # 模拟 _run_capacity_once 已经记了这一次尝试。
        job.attempts = 7
        job.last_attempt_at = datetime.now(timezone.utc)

        worker_mod.CapacityAttempt = poisoned
        try:
            # attempts 远小于 MAX_ATTEMPT_ROWS_PER_JOB，所以不会走到那条用
            # CapacityAttempt 做条件的 delete()——上面这个替身只负责构造。
            Worker._log_attempt(
                Worker.__new__(Worker),
                db,
                job,
                ok=False,
                message="Out of host capacity",
                ad="AD-1",
                config_label="2C/12G",
            )
        finally:
            worker_mod.CapacityAttempt = real_attempt

        # 事务必须还能继续用：这几行就是 _handle_capacity_error 会写的东西。
        job.last_error = "Out of host capacity"
        job.status = "idle"
        db.commit()

    with SessionLocal() as db:
        saved = db.get(CapacityJob, job_id)
        assert saved.attempts == 7, (
            f"attempts 回到了 {saved.attempts} —— 日志写失败把 attempts += 1 一起"
            "回滚了，max_attempts 从此永远够不到"
        )
        assert saved.last_error == "Out of host capacity", "日志失败之后事务已不可用"
        assert saved.status == "idle"
        assert db.query(CapacityAttempt).count() == 0
