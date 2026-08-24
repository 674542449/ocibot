"""停用的容量重试任务原来可以无限地堆。

``POST /jobs/capacity`` 的「一个租户只能有一个在跑的任务」检查只在
``body.enabled`` 为真时才做（见 routers/jobs.py 里那段注释）。带
``"enabled": false`` 提交，这道检查整个跳过，于是行数唯一的天花板是 32MB 的请求
体上限。每一行都拖着一份 ``launch_payload`` JSON，而 ``GET /jobs/capacity`` 是全
量返回的 —— 不需要任何 Oracle 调用就能把库和列表接口一起撑爆。

顺带钉住第二件事：行数上限必须排在 ``enforce_launch_quota`` **之前**。那一步是
一整轮租户枚举，花的是 Oracle 的速率预算，而抢机循环跟它抢的是同一个额度。任何
不需要 Oracle 就能判定的拒绝，都不该让这笔开销先花出去。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_rowcap_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'r.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "rowcap-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "rowcap-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.routers import jobs as jobs_router  # noqa: E402
from web.backend.schemas import CapacityJobCreate  # noqa: E402

_PAYLOAD = {
    "display_name": "i",
    "compartment_id": "ocid1.compartment.oc1..c",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.oc1..i",
    "subnet_id": "ocid1.subnet.oc1..s",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfakekeymaterial",
    "ocpus": 1,
    "memory_in_gbs": 6,
    "boot_volume_size_in_gbs": 50,
}


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


@pytest.fixture(autouse=True)
def _no_oci(monkeypatch):
    monkeypatch.setattr(jobs_router, "get_session_for_row", lambda _row: object())
    monkeypatch.setattr(jobs_router, "enforce_launch_quota", lambda *a, **k: None)
    monkeypatch.setattr(jobs_router, "enforce_secondary_region", lambda *a, **k: False)
    monkeypatch.setattr(jobs_router, "free_only_for_tenant", lambda _t: True)
    monkeypatch.setattr(jobs_router, "tenant_is_secondary", lambda _t: False)


def _seed() -> tuple[str, str]:
    with SessionLocal() as db:
        user = User(username="rowcap", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.commit()
        return tenant.id, user.id


def _fill(owner_id: str, tenant_id: str, n: int) -> None:
    """直接建行，绕开路由 —— 这里要造的是「历史堆积」这个状态。"""
    with SessionLocal() as db:
        for i in range(n):
            db.add(
                CapacityJob(
                    owner_id=owner_id,
                    tenant_id=tenant_id,
                    name=f"old-{i}",
                    enabled=False,
                    status="stopped",
                    launch_payload=dict(_PAYLOAD),
                )
            )
        db.commit()


def test_disabled_jobs_cannot_be_stacked_without_limit():
    tenant_id, owner_id = _seed()
    _fill(owner_id, tenant_id, jobs_router.MAX_CAPACITY_JOBS_PER_USER)

    body = CapacityJobCreate(
        tenant_id=tenant_id, launch_payload=dict(_PAYLOAD), enabled=False
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException) as exc:
            jobs_router.create_capacity_job(body, user, db)
    assert exc.value.status_code == 409

    with SessionLocal() as db:
        assert db.query(CapacityJob).count() == jobs_router.MAX_CAPACITY_JOBS_PER_USER


def test_the_row_cap_is_checked_before_the_oracle_quota_enumeration(monkeypatch):
    """被行数上限拒掉的请求，不能已经花掉一轮租户枚举。"""
    tenant_id, owner_id = _seed()
    _fill(owner_id, tenant_id, jobs_router.MAX_CAPACITY_JOBS_PER_USER)

    enumerations: list[int] = []
    monkeypatch.setattr(
        jobs_router, "enforce_launch_quota", lambda *a, **k: enumerations.append(1)
    )

    body = CapacityJobCreate(
        tenant_id=tenant_id, launch_payload=dict(_PAYLOAD), enabled=False
    )
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        with pytest.raises(HTTPException):
            jobs_router.create_capacity_job(body, user, db)

    assert enumerations == [], "行数上限排在了 Oracle 额度枚举后面，速率预算白花了"


def test_a_user_below_the_cap_can_still_create_a_job():
    """回归护栏：上限不能挡住正常使用。"""
    tenant_id, owner_id = _seed()
    body = CapacityJobCreate(tenant_id=tenant_id, launch_payload=dict(_PAYLOAD), enabled=False)
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        out = jobs_router.create_capacity_job(body, user, db)
    assert out.id


def test_the_cap_is_per_owner_not_global():
    """另一个用户堆满了，不能把我也一起锁死。"""
    tenant_id, owner_id = _seed()
    _fill(owner_id, tenant_id, jobs_router.MAX_CAPACITY_JOBS_PER_USER)

    with SessionLocal() as db:
        other = User(username="rowcap-other", password_hash="x")
        db.add(other)
        db.flush()
        other_tenant = Tenant(
            owner_id=other.id, name="T2", region="ap-tokyo-1", private_key_encrypted=""
        )
        db.add(other_tenant)
        db.commit()
        other_id, other_tenant_id = other.id, other_tenant.id

    body = CapacityJobCreate(
        tenant_id=other_tenant_id, launch_payload=dict(_PAYLOAD), enabled=False
    )
    with SessionLocal() as db:
        user = db.get(User, other_id)
        assert jobs_router.create_capacity_job(body, user, db).id
