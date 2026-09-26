"""0.4.124 后端检查修掉的几处问题，各钉一条。

1. 抢机尝试进行中用户点了「停止」，尝试失败后 worker 不能把状态写回「等待中」；
2. POST /jobs/capacity 不能接受注定失败的 root + 密码模式；
3. 粘贴导入不再在服务端做一遍结果被丢掉的连接测试；
4. 批量删除的审计记录再大也是合法 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_review_fixes_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'r.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "review-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "review-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tests._keys import TEST_PEM  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import AuditLog, CapacityJob, Tenant, User  # noqa: E402

_PW = "supersecret123"
_USER = "review-fixes-user"


def _payload(**over) -> dict:
    base = {
        "display_name": "arm",
        "compartment_id": "ocid1.compartment.oc1..c",
        "availability_domain": "AD-1",
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "subnet_id": "ocid1.subnet.oc1..s",
        "auth_mode": "key",
        "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample key",
        "ocpus": 1,
        "memory_in_gbs": 6,
    }
    base.update(over)
    return base


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


@pytest.fixture(scope="module")
def ids(_db):
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == _USER).one_or_none()
        if user is None:
            user = User(username=_USER, password_hash=hash_password(_PW))
            db.add(user)
            db.flush()
        tenant = Tenant(
            owner_id=user.id,
            name="RF",
            region="ap-tokyo-1",
            user_ocid="ocid1.user.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(TEST_PEM),
        )
        db.add(tenant)
        db.commit()
        return user.id, tenant.id


@pytest.fixture(scope="module")
def client(ids):
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": _USER, "password": _PW})
        assert r.status_code == 200, r.text
        yield c


# ------------------------------------------------------------------ 1. 尝试中被停止


class _CapacityMiss:
    ok = False
    message = "Out of host capacity."
    data = {"capacity": True}
    work_request_id = ""


def test_a_stop_during_an_attempt_is_not_overwritten_by_the_worker(ids):
    from web.backend.worker import Worker

    user_id, tenant_id = ids
    with SessionLocal() as db:
        job = CapacityJob(
            owner_id=user_id,
            tenant_id=tenant_id,
            name="stop-mid-attempt",
            enabled=True,
            status="idle",
            launch_payload={**_payload(), "boot_volume_vpus_per_gb": 10, "nsg_ids": ["nsg1"]},
            interval_sec=180,
            max_attempts=200,
            attempts=0,
            # 远在过去，确保这一轮 tick 一定选中它。
            next_run_at=datetime.now(timezone.utc) - timedelta(days=365),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    class _Session:
        def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
            # 模拟用户在 LaunchInstance 还没返回时点了「停止」（jobs.stop_capacity_job 的写法）。
            with SessionLocal() as other:
                row = other.get(CapacityJob, job_id)
                row.enabled = False
                row.status = "stopped"
                row.locked_by = None
                row.locked_until = None
                other.commit()
            return _CapacityMiss()

        def get_free_quota_usage(self, free_only_mode: bool = True):
            from app.oci_client import OperationResult

            return OperationResult(
                ok=True,
                message="",
                data={
                    "account_tier": "free",
                    "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
                    "remaining": {"a1_ocpu": 4.0, "a1_memory_gb": 24.0, "e2_micro_count": 2, "block_storage_gb": 200.0},
                },
            )

    class _Sessions:
        def get(self, _cfg):
            return _Session()

    worker = Worker()
    worker.sessions = _Sessions()
    with SessionLocal() as db:
        worker.tick_capacity(db)

    with SessionLocal() as db:
        row = db.get(CapacityJob, job_id)
        assert row.enabled is False
        assert row.status == "stopped", "用户的「停止」被 worker 写回了「等待中」"
        assert row.next_run_at is None


# ------------------------------------------------------------------ 2. 接口拒绝密码模式


def test_the_capacity_api_refuses_password_mode(client, ids):
    _user_id, tenant_id = ids
    r = client.post(
        "/api/jobs/capacity",
        json={"tenant_id": tenant_id, "launch_payload": _payload(auth_mode="password", ssh_public_key="")},
    )
    assert r.status_code == 400, r.text
    assert "密码" in r.json()["detail"]


# ------------------------------------------------------------------ 3. 粘贴导入不测连接


def test_paste_import_does_not_spend_an_oracle_call(client, monkeypatch):
    import web.backend.routers.tenants as tenants_router

    calls: list = []
    monkeypatch.setattr(tenants_router, "get_session_for_row", lambda row: calls.append(row) or None)
    config = (
        "[DEFAULT]\n"
        "user=ocid1.user.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh\n"
        "fingerprint=11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00\n"
        "tenancy=ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh\n"
        "region=ap-osaka-1\n"
    )
    r = client.post(
        "/api/tenants/import",
        json={"api_text": config, "private_key_pem": TEST_PEM, "name": "pasted", "test_connection": True},
    )
    assert r.status_code == 201, r.text
    assert calls == [], "导入时又在服务端测了一遍连接，结果还被丢掉"


# ------------------------------------------------------------------ 4. 审计 JSON 合法


def test_a_large_batch_delete_still_writes_valid_audit_json(client, ids):
    user_id, _tenant_id = ids
    with SessionLocal() as db:
        new_ids = []
        for i in range(80):
            row = Tenant(
                owner_id=user_id,
                # 名字尽量长，逼近 write_audit 的 4000 字符截断。
                name=f"批量删除用的一个名字很长很长的租户-{i:03d}-" + "长" * 60,
                region="ap-tokyo-1",
                private_key_encrypted=encrypt_text(TEST_PEM),
            )
            db.add(row)
            db.flush()
            new_ids.append(row.id)
        db.commit()

    r = client.post("/api/tenants/batch-delete", json={"ids": new_ids + ["missing-1", "missing-2"]})
    assert r.status_code == 200, r.text
    assert len(r.json()["deleted"]) == 80

    with SessionLocal() as db:
        rec = (
            db.query(AuditLog)
            .filter(AuditLog.owner_id == user_id, AuditLog.action == "tenant.batch_delete")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
    detail = json.loads(rec.detail)  # 截断的 JSON 会在这里抛
    assert detail["deleted_count"] == 80
    assert detail["skipped_count"] == 2
