"""IPv6 地址段的服务限额 ipv6-flexible-cidrs-allowed-count-per-vcn。

真实账号上的报错（ap-singapore-2，给已有实例分配 /120）：

  [400] LimitExceeded Limit for ipv6-flexible-cidrs-allowed-count-per-vcn of 0
  has been already reached.

0.4.116 把它和其它 400 一视同仁，附上「子网前缀是功能 GA 之前建的，提工单」的
提示 —— 对这个错误是错的：原因是账号的限额为 0，要去申请提高限额。

这个文件钉住：
1. 这条错误被识别出来，说的是限额，而不是旧前缀；
2. 限额读出来是 0 时，在创建任何东西之前就拒绝（实例详情路径不会先塞一个
   普通地址；创建路径不会开出一台拿不到地址段的机器）；
3. 读不到限额时（None）不拦，交给 CreateIpv6 自己判。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_v6limit_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'v.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "v6limit-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "v6limit-jwt-secret-0123456789abcdef")

oci = pytest.importorskip("oci")

from oci.exceptions import ServiceError  # noqa: E402

from app.oci_client import (  # noqa: E402
    IPV6_CIDR_LIMIT_NAME,
    OperationResult,
    TenantSession,
    _ipv6_cidr_error_text,
)

_REAL_MESSAGE = (
    "Limit for ipv6-flexible-cidrs-allowed-count-per-vcn of 0 has been already reached."
)


# ------------------------------------------------------------------ 错误文案


def test_the_real_error_is_explained_as_a_zero_limit():
    exc = ServiceError(400, "LimitExceeded", {}, _REAL_MESSAGE)
    text = _ipv6_cidr_error_text(exc)
    assert IPV6_CIDR_LIMIT_NAME in text
    assert "为 0" in text and "申请提高" in text
    # 不能再把用户指向「旧前缀」那条路 —— 对这个错误它是错的。
    assert "之前创建" not in text
    # Oracle 原文保留，opc-request-id 之类的诊断信息不能丢。
    assert _REAL_MESSAGE in text


def test_a_nonzero_limit_that_is_used_up_says_so():
    exc = ServiceError(
        400, "LimitExceeded", {}, f"Limit for {IPV6_CIDR_LIMIT_NAME} of 16 has been already reached."
    )
    text = _ipv6_cidr_error_text(exc)
    assert "已达到" in text and "16" in text
    assert "为 0" not in text


def test_other_400s_keep_the_old_prefix_hint():
    exc = ServiceError(400, "InvalidParameter", {}, "Invalid cidrPrefixLength")
    assert "之前创建" in _ipv6_cidr_error_text(exc)


# ------------------------------------------------------------------ 读限额


class _Limits:
    def __init__(self, values=None, fail=False):
        self.values = values or []
        self.fail = fail
        self.calls: list[dict] = []

    def list_limit_values(self, compartment_id, **kwargs):
        from oci.response import Response

        self.calls.append({"compartment_id": compartment_id, **kwargs})
        if self.fail:
            raise ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope")
        return Response(200, {}, list(self.values), None)


def _limit_session(limits: _Limits) -> TenantSession:
    s = TenantSession.__new__(TenantSession)
    s._limits = limits
    s.tenant = SimpleNamespace(tenancy_ocid="ocid1.tenancy.oc1..t", region="ap-singapore-2", id="t1")
    return s


def test_limit_is_read_with_one_filtered_call():
    limits = _Limits([SimpleNamespace(name=IPV6_CIDR_LIMIT_NAME, value=0)])
    assert _limit_session(limits).ipv6_cidr_limit_value() == 0
    assert len(limits.calls) == 1
    call = limits.calls[0]
    assert call["compartment_id"] == "ocid1.tenancy.oc1..t"
    assert call["service_name"] == "vcn" and call["name"] == IPV6_CIDR_LIMIT_NAME


def test_limit_value_is_returned():
    limits = _Limits([SimpleNamespace(name=IPV6_CIDR_LIMIT_NAME, value=16)])
    assert _limit_session(limits).ipv6_cidr_limit_value() == 16


@pytest.mark.parametrize(
    "limits",
    [
        _Limits([]),  # 该区域的 Limits 服务没有发布这个名字
        _Limits(fail=True),  # 没权限 / 网络错误
        _Limits([SimpleNamespace(name="vcn-count", value=0)]),  # 过滤没生效也不能误读
    ],
)
def test_an_unreadable_limit_is_unknown_not_zero(limits):
    """None 和 0 必须分开：读失败当成 0 会把一个确实开通了的账号挡在门外。"""
    assert _limit_session(limits).ipv6_cidr_limit_value() is None


# ------------------------------------------------------------------ 实例详情路径


def _assign_session(limit):
    s = TenantSession.__new__(TenantSession)
    s.ipv6_cidr_limit_value = lambda: limit  # type: ignore[method-assign]
    s.base_calls = []
    s.created = []

    def base(instance_id, compartment_id):
        s.base_calls.append(instance_id)
        return OperationResult(ok=True, message="已分配公网 IPv6")

    s.assign_public_ipv6 = base  # type: ignore[method-assign]

    def resolve(*a, **k):
        raise AssertionError("限额为 0 时不应该再去读网卡")

    s.resolve_primary_network = resolve  # type: ignore[method-assign]
    return s


def test_a_zero_limit_fails_before_touching_the_instance():
    s = _assign_session(0)
    res = s.assign_ipv6_prefix("i1", "c1", 120)
    assert res.ok is False
    assert IPV6_CIDR_LIMIT_NAME in res.message
    # 以前会先给没有 IPv6 的实例分一个普通地址，再在地址段那步失败。
    assert s.base_calls == []


def test_a_zero_limit_does_not_block_a_single_address():
    s = _assign_session(0)
    assert s.assign_ipv6_prefix("i1", "c1", 128).ok
    assert s.base_calls == ["i1"]


# ------------------------------------------------------------------ 创建路径


pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

from tests._keys import TEST_PEM  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    init_db()
    username = "v6limit-user"
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
                name="V6",
                region="ap-singapore-2",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(TEST_PEM),
            )
            db.add(tenant)
        db.commit()
        tenant_id = tenant.id

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": username, "password": "supersecret123"})
        assert r.status_code == 200, r.text
        yield c, tenant_id


def _stub(monkeypatch, *, limit):
    session = MagicMock()
    session.ipv6_cidr_limit_value.return_value = limit
    session.launch_from_payload.return_value = OperationResult(
        ok=True, message="创建成功", data={"instance_id": "ocid1.instance.oc1..n"}
    )
    network_prepared: list = []
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    monkeypatch.setattr(instances_router, "fetch_launch_meta", lambda *a, **k: {})
    monkeypatch.setattr(
        instances_router,
        "prepare_launch_network",
        lambda s, p, **k: network_prepared.append(p) or p,
    )
    monkeypatch.setattr(instances_router, "enforce_secondary_region", lambda *a, **k: "")
    monkeypatch.setattr(instances_router, "enforce_launch_quota", lambda *a, **k: None)
    monkeypatch.setattr(instances_router, "format_guard_warnings", lambda g: [])
    monkeypatch.setattr(instances_router, "schedule_post_launch_adjustments", lambda *a, **k: None)
    return session, network_prepared


def _body(**over):
    body = {
        "display_name": "v6",
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "subnet_id": "ocid1.subnet.oc1..s",
        "compartment_id": "ocid1.compartment.oc1..c",
        "availability_domain": "AD-1",
        "auth_mode": "key",
        "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample key",
        "ocpus": 1,
        "memory_in_gbs": 6,
        "assign_ipv6_ip": True,
        "ipv6_prefix_length": 120,
    }
    body.update(over)
    return body


def test_launch_is_refused_before_anything_is_created_when_the_limit_is_zero(client, monkeypatch):
    c, tid = client
    session, prepared = _stub(monkeypatch, limit=0)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body())
    assert r.status_code == 400, r.text
    assert IPV6_CIDR_LIMIT_NAME in r.json()["detail"]
    assert prepared == [], "网络 / NSG 不该被准备"
    session.launch_from_payload.assert_not_called()


def test_a_capacity_retry_job_is_not_queued_either(client, monkeypatch):
    c, tid = client
    session, _ = _stub(monkeypatch, limit=0)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(as_retry=True))
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("limit", [None, 16])
def test_an_unknown_or_positive_limit_lets_the_launch_through(client, monkeypatch, limit):
    c, tid = client
    session, _ = _stub(monkeypatch, limit=limit)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body())
    assert r.status_code == 200, r.text
    session.launch_from_payload.assert_called_once()


def test_a_single_address_launch_never_reads_the_limit(client, monkeypatch):
    """/128 用不到这个限额，就不该为它多花一次 API 调用。"""
    c, tid = client
    session, _ = _stub(monkeypatch, limit=0)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(ipv6_prefix_length=128))
    assert r.status_code == 200, r.text
    session.ipv6_cidr_limit_value.assert_not_called()
