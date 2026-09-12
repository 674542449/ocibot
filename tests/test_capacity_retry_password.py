"""root + 密码模式也能进容量重试。

## 以前为什么拦

`launch_service.build_launch_request` 和 `sanitize_launch_payload(for_retry=True)`
都拒绝密码模式进重试。理由是对的：**明文密码绝不能进 `launch_payload`** —— 那是
存在库里的明文 JSON，任何能读库的人都能拿到。

但那个理由约束的是「密码不能落在 payload 里」，不是「密码模式不能重试」。
仓库里早有先例：自定义启动脚本（可能含密钥）走 `CapacityJob.user_data_encrypted`，
Fernet 加密存在任务行上，开机那一刻才解密。密码走同一条路。

## 这个文件钉住的事

1. 校验层不再按模式拒绝，但 payload 里混进明文密码仍然会被打回；
2. 建任务时密码**只**落在 `root_password_encrypted`，`launch_payload` 和任务列表
   接口里一个字都不出现；
3. worker 开机时解密并传给 `launch_from_payload` —— 只有这样成功后的实例才会带上
   `ocibot_root_password` 标签，用户才有地方看到密码；
4. 解不开时 **fail closed**：任务标失败、不消耗尝试次数、不去开一台谁都登不进的机器。
   这和启动脚本解不开「照样开机」是刻意相反的 —— 脚本丢了能补跑，密码丢了机器就废了；
5. 老安装升级：`_ensure_schema` 能给已有的 `capacity_jobs` 补上这一列，
   而且带 `DEFAULT ''`（NOT NULL 列没有默认值的话，SQLite 拒绝 ADD COLUMN）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ocibot-retry-pw-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP / 'app.db').as_posix()}")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.oci_client import sanitize_launch_payload  # noqa: E402
from tests._keys import TEST_PEM  # noqa: E402
from web.backend import db as db_module  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import decrypt_text, encrypt_text  # noqa: E402
from web.backend.db import Base, SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User  # noqa: E402
from web.backend.routers import instances as instances_router  # noqa: E402
from web.backend.worker import Worker  # noqa: E402

PASSWORD = "Retry-Pass-2026!x"


# ------------------------------------------------------------------ 校验层


def _payload(auth_mode: str) -> dict:
    return {
        "display_name": "arm",
        "compartment_id": "ocid1.compartment.oc1..c",
        "availability_domain": "AD-1",
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "subnet_id": "ocid1.subnet.oc1..s",
        "auth_mode": auth_mode,
        "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample key",
    }


def test_sanitize_accepts_password_mode_for_retry():
    """这是被拆掉的那道闸。"""
    clean = sanitize_launch_payload(_payload("password"), for_retry=True)
    assert clean["auth_mode"] == "password"


def test_sanitize_still_refuses_a_plaintext_password_in_the_payload():
    """闸门拆了，底线没拆：payload 是明文 JSON，密码永远不能在里面。"""
    bad = {**_payload("password"), "root_password": PASSWORD}
    with pytest.raises(ValueError, match="密码"):
        sanitize_launch_payload(bad, for_retry=True)


def test_the_retry_only_gate_is_gone_from_the_launch_builder():
    src = Path("web/backend/launch_service.py").read_text(encoding="utf-8")
    assert "仅支持 root + SSH 公钥模式" not in src


def test_the_job_row_carries_an_encrypted_password_column_that_defaults_empty():
    col = CapacityJob.__table__.c.root_password_encrypted
    assert col.nullable is False
    # default="" 不是洁癖：_ensure_schema 据它生成 DEFAULT ''，老安装才补得上这一列。
    assert col.default is not None and col.default.arg == ""


# ------------------------------------------------------------------ 路由：建任务


@pytest.fixture(scope="module")
def client():
    init_db()
    username = "retry-pw-user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).one_or_none()
        if tenant is None:
            tenant = Tenant(
                owner_id=user.id,
                name="PW",
                region="ap-tokyo-1",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(TEST_PEM),
            )
            db.add(tenant)
        db.commit()
        tenant_id = tenant.id

    from web.backend.main import create_app

    with TestClient(create_app()) as c:
        r = c.post("/api/auth/login", json={"username": username, "password": "supersecret123"})
        assert r.status_code == 200, r.text
        yield c, tenant_id


def _launch_body(**over) -> dict:
    """路由的请求模型要求完整的创建参数（image_id 等），照 test_launch_count 的 _body 来。"""
    body = {
        "display_name": "arm",
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "ocpus": 1,
        "memory_in_gbs": 6,
        "boot_volume_size_in_gbs": 50,
        "as_retry": True,
    }
    body.update(over)
    return body


def _stub_route(monkeypatch):
    """把路由周围会打 Oracle 的东西全部换掉，只留建任务那一段。"""
    session = MagicMock()
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    monkeypatch.setattr(instances_router, "fetch_launch_meta", lambda *a, **k: {})
    monkeypatch.setattr(
        instances_router,
        "build_launch_request",
        lambda body, meta=None: {
            "payload": {
                **_payload(body.get("auth_mode") or "key"),
                "display_name": body.get("display_name") or "instance",
                "boot_volume_vpus_per_gb": 10,
                "nsg_ids": ["nsg1"],
            },
            # 密码模式不填密码时 build_launch_request 会自己生成一个 —— 桩照做。
            "root_password": (body.get("root_password") or PASSWORD)
            if (body.get("auth_mode") or "key") == "password"
            else "",
            "custom_user_data": "",
            "as_retry": bool(body.get("as_retry")),
            "retry_interval_sec": 180,
            "retry_max_attempts": 200,
            "availability_domains": [],
            "fallback_configs": [],
        },
    )
    monkeypatch.setattr(instances_router, "prepare_launch_network", lambda s, p, **k: p)
    monkeypatch.setattr(instances_router, "enforce_secondary_region", lambda *a, **k: "")
    monkeypatch.setattr(instances_router, "enforce_launch_quota", lambda *a, **k: None)
    monkeypatch.setattr(instances_router, "format_guard_warnings", lambda g: [])
    monkeypatch.setattr(instances_router, "schedule_post_launch_adjustments", lambda *a, **k: None)
    return session


def test_a_password_mode_retry_stores_the_password_encrypted_and_nowhere_else(client, monkeypatch):
    c, tid = client
    session = _stub_route(monkeypatch)
    with SessionLocal() as db:
        db.query(CapacityJob).filter(CapacityJob.tenant_id == tid).delete()
        db.commit()

    r = c.post(
        f"/api/tenants/{tid}/launch",
        json=_launch_body(auth_mode="password"),
    )
    assert r.status_code == 200, r.text
    # 排队重试，不是立刻开机。
    session.launch_from_payload.assert_not_called()

    with SessionLocal() as db:
        job = db.query(CapacityJob).filter(CapacityJob.tenant_id == tid).one()
        assert job.root_password_encrypted, "密码没有跟着任务走"
        assert job.root_password_encrypted != PASSWORD, "密码明文落库了"
        assert decrypt_text(job.root_password_encrypted) == PASSWORD
        # payload 是明文 JSON —— 一个字都不能出现。
        assert PASSWORD not in repr(job.launch_payload)
        assert "root_password" not in job.launch_payload
        assert job.launch_payload["auth_mode"] == "password"

    # 任务列表接口只说「有」，永远不回密码本身。
    listed = c.get("/api/jobs/capacity")
    assert listed.status_code == 200, listed.text
    rows = [x for x in listed.json() if x["tenant_id"] == tid]
    assert rows and rows[0]["has_root_password"] is True
    assert PASSWORD not in listed.text


def test_a_key_mode_retry_leaves_the_password_column_empty(client, monkeypatch):
    c, tid = client
    _stub_route(monkeypatch)
    with SessionLocal() as db:
        db.query(CapacityJob).filter(CapacityJob.tenant_id == tid).delete()
        db.commit()
    r = c.post(
        f"/api/tenants/{tid}/launch",
        json=_launch_body(auth_mode="key"),
    )
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        job = db.query(CapacityJob).filter(CapacityJob.tenant_id == tid).one()
        assert job.root_password_encrypted == ""
    rows = [x for x in c.get("/api/jobs/capacity").json() if x["tenant_id"] == tid]
    assert rows[0]["has_root_password"] is False


# ------------------------------------------------------------------ worker：开机那一刻


class _Result:
    def __init__(self, ok=True, message="创建成功"):
        self.ok = ok
        self.message = message
        self.data = {"instance_id": "ocid1.instance.oc1..new"} if ok else {}
        self.work_request_id = ""


class _FakeSession:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
        self.calls.append({"root_password": root_password, "auth_mode": payload.get("auth_mode")})
        return _Result()

    def get_free_quota_usage(self, free_only_mode: bool = True):
        r = _Result(True, "")
        r.data = {
            "account_tier": "free",
            "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
            "remaining": {"a1_ocpu": 4.0, "a1_memory_gb": 24.0, "e2_micro_count": 2, "block_storage_gb": 200.0},
        }
        return r


class _FakeSessions:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    def get(self, _cfg):
        return _FakeSession(self.calls)


@pytest.fixture()
def _clean_db():
    init_db()
    with SessionLocal() as db:
        db.query(CapacityAttempt).delete()
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _seed_job(db, *, auth_mode: str, encrypted: str) -> str:
    user = User(username="w", password_hash="x")
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
        launch_payload={**_payload(auth_mode), "boot_volume_vpus_per_gb": 10, "nsg_ids": ["nsg1"]},
        root_password_encrypted=encrypted,
        interval_sec=180,
        max_attempts=200,
        attempts=0,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(job)
    db.commit()
    return job.id


def _tick(job_id: str) -> tuple[CapacityJob, list[dict], int]:
    calls: list[dict] = []
    worker = Worker()
    worker.sessions = _FakeSessions(calls)
    with SessionLocal() as db:
        worker.tick_capacity(db)
    with SessionLocal() as db:
        job = db.get(CapacityJob, job_id)
        attempts_logged = db.query(CapacityAttempt).filter(CapacityAttempt.job_id == job_id).count()
        db.expunge(job)
    return job, calls, attempts_logged


def test_the_worker_decrypts_the_password_and_hands_it_to_the_launch(_clean_db):
    """不传的话 launch_instance 不会写 ocibot_root_password 标签 ——
    机器半夜抢到了，用户却没地方看到密码。"""
    with SessionLocal() as db:
        job_id = _seed_job(db, auth_mode="password", encrypted=encrypt_text(PASSWORD))
    job, calls, _ = _tick(job_id)
    assert calls and calls[0]["root_password"] == PASSWORD
    assert job.status == "success"


def test_an_undecryptable_password_fails_closed_without_burning_attempts(_clean_db):
    """和启动脚本解不开「照样开机」刻意相反：脚本丢了能补跑，密码丢了机器就废了 ——
    还吃掉 Always Free 额度。所以停手、标失败、留痕，不消耗尝试次数。"""
    with SessionLocal() as db:
        job_id = _seed_job(db, auth_mode="password", encrypted="not-a-fernet-token")
    job, calls, attempts_logged = _tick(job_id)
    assert not calls, "解不开密码还是去开机了"
    assert job.status == "failed" and job.enabled is False
    assert job.attempts == 0, "解密失败不该消耗尝试次数"
    assert "无法解密" in (job.last_error or "")
    # 任务中心看的是尝试日志，只写 last_error 的话用户看到「失败」却不知道为什么。
    assert attempts_logged == 1


def test_a_key_mode_job_ignores_the_password_column(_clean_db):
    with SessionLocal() as db:
        job_id = _seed_job(db, auth_mode="key", encrypted=encrypt_text("stray"))
    job, calls, _ = _tick(job_id)
    assert calls and calls[0]["root_password"] == ""
    assert job.status == "success"


# ------------------------------------------------------------------ 老安装升级


def test_ensure_schema_adds_the_column_with_a_default_on_an_older_database(monkeypatch):
    """0.4.115 之前建的库没有这一列。_ensure_schema 只 ADD COLUMN，而 NOT NULL 列
    在 SQLite 上没有默认值就加不上 —— 所以模型里那个 default="" 是有用的。"""
    path = _TMP / "legacy.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE capacity_jobs DROP COLUMN root_password_encrypted")
    con.commit()
    assert "root_password_encrypted" not in {r[1] for r in con.execute("PRAGMA table_info(capacity_jobs)")}
    con.close()

    monkeypatch.setattr(db_module, "engine", engine)
    db_module._ensure_schema()

    con = sqlite3.connect(path)
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(capacity_jobs)")}
    con.close()
    assert "root_password_encrypted" in cols, "升级没补上这一列"
    assert cols["root_password_encrypted"][4] == "''", cols["root_password_encrypted"]

    # 老代码路径（不写这一列）的 INSERT 必须仍然成立。
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        user = User(username="legacy", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="L", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.flush()
        db.add(CapacityJob(owner_id=user.id, tenant_id=tenant.id, launch_payload={}))
        db.commit()
