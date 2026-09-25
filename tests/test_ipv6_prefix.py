"""创建实例时选择 IPv6 地址段（/120、/116、/112 …）。

依据 OCI 文档「IPv6 Addresses → Assignment of IPv6 Addresses to a VNIC」：

  "By using the CreateIpv6 API with the cidrPrefixLength attribute, you can
   allocate a contiguous range of host IP addresses within a subnet to a VNIC"
  "The IP address mask must be assigned as a secondary IP address to the VNIC."
  "The mask value must be within the range of 80-128."
  "The mask value must be divisible by 4 without any remainder."

LaunchInstance 的 CreateVnicDetails 没有前缀长度字段（SDK 里
Ipv6AddressIpv6SubnetCidrPairDetails 只有 ipv6_address / ipv6_id /
ipv6_subnet_cidr），所以地址段只能在开机、VNIC 挂好之后用 CreateIpv6 加。

这个文件钉住：
1. 前缀长度的取值规则（80–128、被 4 整除），前后端两份要一致；
2. launch payload 带着它进库（抢机任务开机后也要用），没勾 IPv6 时归一成 128；
3. assign_ipv6_prefix 真的把 cidr_prefix_length 传给 CreateIpv6，先保证有一个普通
   地址，已有同长度的地址段时不重复分配，Oracle 拒绝时给出可操作的提示；
4. 即时创建和抢机成功两条路径都会把前缀交给开机后的后台步骤。
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ocibot-ipv6-prefix-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_TMP / 'app.db').as_posix()}")

oci = pytest.importorskip("oci")

from oci.exceptions import ServiceError  # noqa: E402

import app.oci_client as oci_client  # noqa: E402
from app.oci_client import (  # noqa: E402
    IPV6_PREFIX_LENGTHS,
    OperationResult,
    TenantSession,
    normalize_ipv6_prefix_length,
    sanitize_launch_payload,
)

# ------------------------------------------------------------------ 取值规则


def test_allowed_prefix_lengths_follow_the_oci_doc():
    assert IPV6_PREFIX_LENGTHS == tuple(range(80, 129, 4))
    for p in (120, 116, 112, 80, 124, 128):
        assert normalize_ipv6_prefix_length(p) == p
    assert normalize_ipv6_prefix_length("/120") == 120
    assert normalize_ipv6_prefix_length(None) == 128
    assert normalize_ipv6_prefix_length("") == 128


@pytest.mark.parametrize("bad", [76, 79, 81, 118, 130, 64, "abc", True, 12.5])
def test_out_of_range_or_not_divisible_by_four_is_refused(bad):
    with pytest.raises(ValueError):
        normalize_ipv6_prefix_length(bad)


def test_the_frontend_offers_exactly_the_same_lengths():
    """前后端各有一份清单；前端多一个选项，用户就能选一个服务端必然 400 的值。"""
    src = (
        Path(__file__).resolve().parents[1] / "web" / "frontend" / "src" / "utils" / "ipv6.ts"
    ).read_text(encoding="utf-8")
    m = re.search(r"IPV6_PREFIX_OPTIONS[^=]*=\s*\[([^\]]*)\]", src)
    assert m, "IPV6_PREFIX_OPTIONS not found"
    offered = sorted(int(x) for x in re.findall(r"\d+", m.group(1)))
    assert offered == sorted(IPV6_PREFIX_LENGTHS)


# ------------------------------------------------------------------ launch payload


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
    }
    base.update(over)
    return base


def test_payload_keeps_the_prefix_when_ipv6_is_requested():
    clean = sanitize_launch_payload(_payload(assign_ipv6_ip=True, ipv6_prefix_length=120))
    assert clean["ipv6_prefix_length"] == 120


def test_payload_without_ipv6_is_normalized_to_a_single_address():
    """否则开机后的后台步骤会给一台不要 IPv6 的机器建地址段。"""
    clean = sanitize_launch_payload(_payload(assign_ipv6_ip=False, ipv6_prefix_length=112))
    assert clean["ipv6_prefix_length"] == 128


def test_an_old_job_payload_without_the_key_means_one_address():
    clean = sanitize_launch_payload(_payload(assign_ipv6_ip=True))
    assert clean["ipv6_prefix_length"] == 128


def test_an_invalid_prefix_is_refused_when_building_the_launch():
    from web.backend.launch_service import build_launch_request

    body = {**_payload(), "assign_ipv6_ip": True, "ipv6_prefix_length": 118}
    with pytest.raises(ValueError, match="被 4 整除"):
        build_launch_request(body, meta=None)


def test_build_launch_request_carries_the_prefix_into_the_payload():
    from web.backend.launch_service import build_launch_request

    body = {**_payload(), "assign_ipv6_ip": True, "ipv6_prefix_length": 116}
    built = build_launch_request(body, meta=None)
    assert built["payload"]["ipv6_prefix_length"] == 116


def test_the_launch_request_schema_bounds_the_field():
    from pydantic import ValidationError

    from web.backend.schemas import LaunchInstanceRequest

    base = {"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..i"}
    assert LaunchInstanceRequest(**base).ipv6_prefix_length == 128
    with pytest.raises(ValidationError):
        LaunchInstanceRequest(**base, ipv6_prefix_length=64)


# ------------------------------------------------------------------ assign_ipv6_prefix


class _Resp:
    def __init__(self, data):
        self.data = data


class _Network:
    def __init__(self, ipv6s=(), subnet_blocks=("2603:c020:8004:1a00::/64",), fail: Exception | None = None):
        self.ipv6s = list(ipv6s)
        self.subnet_blocks = list(subnet_blocks)
        self.fail = fail
        self.created: list = []

    def list_ipv6s(self, **kwargs):
        from oci.response import Response

        return Response(200, {}, list(self.ipv6s), None)

    def get_subnet(self, subnet_id):
        return _Resp(SimpleNamespace(id=subnet_id, ipv6_cidr_block="", ipv6_cidr_blocks=self.subnet_blocks))

    def create_ipv6(self, details):
        if self.fail is not None:
            raise self.fail
        self.created.append(details)
        return _Resp(SimpleNamespace(id="ocid1.ipv6.oc1..new", ip_address="2603:c020:8004:1a00:0:0:1:0"))


def _session(net: _Network, *, has_address: bool = True):
    s = TenantSession.__new__(TenantSession)
    s._network = net
    s.base_calls = []
    s.route_calls = []

    def resolve(instance_id, compartment_id, **k):
        return SimpleNamespace(
            vnic_id="ocid1.vnic.oc1..v",
            subnet_id="ocid1.subnet.oc1..s",
            ipv6_addresses=["2603:c020:8004:1a00::10"] if (has_address or s.base_calls) else [],
            nsg_ids=[],
        )

    def base(instance_id, compartment_id):
        s.base_calls.append(instance_id)
        net.ipv6s.append(SimpleNamespace(id="b", ip_address="2603:c020:8004:1a00::10", cidr_prefix_length=None))
        return OperationResult(ok=True, message="已分配公网 IPv6：2603:c020:8004:1a00::10")

    def route(subnet_id, compartment_id):
        s.route_calls.append(subnet_id)
        return OperationResult(ok=True, message="路由已就绪", data={})

    s.resolve_primary_network = resolve  # type: ignore[method-assign]
    s.assign_public_ipv6 = base  # type: ignore[method-assign]
    s.ensure_ipv6_internet_access = route  # type: ignore[method-assign]
    return s


def _single(addr="2603:c020:8004:1a00::10"):
    return SimpleNamespace(id="a", ip_address=addr, cidr_prefix_length=None)


def test_create_ipv6_receives_the_cidr_prefix_length():
    net = _Network(ipv6s=[_single()])
    s = _session(net)
    res = s.assign_ipv6_prefix("i1", "c1", 120)
    assert res.ok, res.message
    assert len(net.created) == 1
    details = net.created[0]
    assert isinstance(details, oci.core.models.CreateIpv6Details)
    assert details.cidr_prefix_length == 120
    assert details.vnic_id == "ocid1.vnic.oc1..v"
    # 单前缀子网不传 ipv6_subnet_cidr，别给它引入新的失败面。
    assert details.ipv6_subnet_cidr is None
    assert res.data["cidr"].endswith("/120")
    assert res.data["count"] == 256
    assert "256" in res.message
    # 已经有地址，不应再走一遍「先分配一个普通地址」。
    assert s.base_calls == []


def test_a_vnic_without_ipv6_first_gets_a_plain_address():
    """文档：地址段必须是 VNIC 上的 secondary IP。"""
    net = _Network(ipv6s=[])
    s = _session(net, has_address=False)
    res = s.assign_ipv6_prefix("i1", "c1", 112)
    assert res.ok, res.message
    assert s.base_calls == ["i1"]
    assert net.created and net.created[0].cidr_prefix_length == 112


def test_an_existing_range_of_the_same_length_is_not_allocated_twice():
    existing = SimpleNamespace(id="r", ip_address="2603:c020:8004:1a00::1:0", cidr_prefix_length=116)
    net = _Network(ipv6s=[_single(), existing])
    s = _session(net)
    res = s.assign_ipv6_prefix("i1", "c1", 116)
    assert res.ok
    assert net.created == []
    assert res.data["already"] is True
    assert res.data["cidr"] == "2603:c020:8004:1a00::1:0/116"


def test_multi_prefix_subnet_picks_the_gua_prefix():
    net = _Network(
        ipv6s=[_single()],
        subnet_blocks=["fd00:aaaa:123:1111::/64", "2603:c020:8004:1a00::/64"],
    )
    s = _session(net)
    assert s.assign_ipv6_prefix("i1", "c1", 120).ok
    assert net.created[0].ipv6_subnet_cidr == "2603:c020:8004:1a00::/64"


def test_single_address_delegates_to_the_old_path():
    net = _Network(ipv6s=[])
    s = _session(net, has_address=False)
    res = s.assign_ipv6_prefix("i1", "c1", 128)
    assert res.ok
    assert s.base_calls == ["i1"]
    assert net.created == []


def test_invalid_prefix_never_reaches_oracle():
    net = _Network(ipv6s=[_single()])
    s = _session(net)
    res = s.assign_ipv6_prefix("i1", "c1", 118)
    assert res.ok is False
    assert net.created == []


def test_a_refusal_explains_the_old_prefix_caveat():
    """文档：功能 GA 之前建的前缀需要工单开通 —— 裸报 400 用户不知道下一步。"""
    err = ServiceError(400, "InvalidParameter", {}, "Invalid cidrPrefixLength")
    net = _Network(ipv6s=[_single()], fail=err)
    s = _session(net)
    res = s.assign_ipv6_prefix("i1", "c1", 120)
    assert res.ok is False
    assert "工单" in res.message


def test_waiting_for_the_vnic_polls_until_attached(monkeypatch):
    states = iter([[], [SimpleNamespace(lifecycle_state="ATTACHING")], [SimpleNamespace(lifecycle_state="ATTACHED")]])
    sleeps: list[float] = []

    class _Compute:
        def list_vnic_attachments(self, **kwargs):
            from oci.response import Response

            return Response(200, {}, next(states), None)

        def get_instance(self, instance_id):
            return _Resp(SimpleNamespace(lifecycle_state="PROVISIONING"))

    net = _Network(ipv6s=[_single()])
    s = _session(net)
    s._compute = _Compute()
    monkeypatch.setattr(oci_client.time, "sleep", lambda sec: sleeps.append(sec))
    res = s.assign_ipv6_prefix("i1", "c1", 120, wait_for_vnic_sec=600)
    assert res.ok, res.message
    assert len(sleeps) == 2
    assert net.created


def test_waiting_stops_when_the_instance_is_terminated(monkeypatch):
    class _Compute:
        def list_vnic_attachments(self, **kwargs):
            from oci.response import Response

            return Response(200, {}, [], None)

        def get_instance(self, instance_id):
            return _Resp(SimpleNamespace(lifecycle_state="TERMINATED"))

    net = _Network(ipv6s=[_single()])
    s = _session(net)
    s._compute = _Compute()
    monkeypatch.setattr(oci_client.time, "sleep", lambda sec: None)
    res = s.assign_ipv6_prefix("i1", "c1", 120, wait_for_vnic_sec=600)
    assert res.ok is False
    assert "终止" in res.message
    assert net.created == []


# ------------------------------------------------------------------ 开机后的后台步骤


def test_post_launch_step_waits_for_the_vnic_and_assigns_the_range():
    from web.backend.launch_service import IPV6_PREFIX_VNIC_WAIT_SEC, post_launch_ipv6_prefix

    calls: list = []

    class _S:
        def assign_ipv6_prefix(self, instance_id, compartment_id, prefix, *, wait_for_vnic_sec=0):
            calls.append((instance_id, compartment_id, prefix, wait_for_vnic_sec))
            return OperationResult(ok=True, message="已分配 IPv6 地址段：x/120", data={"cidr": "x/120"})

    note = post_launch_ipv6_prefix(_S(), instance_id="i1", compartment_id="c1", prefix_length=120)
    assert calls == [("i1", "c1", 120, IPV6_PREFIX_VNIC_WAIT_SEC)]
    assert "x/120" in note


def test_nothing_is_scheduled_for_a_plain_launch(monkeypatch):
    import threading

    from web.backend.launch_service import schedule_post_launch_adjustments

    started: list = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    schedule_post_launch_adjustments(
        object(), instance_id="i1", compartment_id="c1", boot_vpu=10, ipv6_prefix_length=128
    )
    assert started == []
    schedule_post_launch_adjustments(
        object(), instance_id="i1", compartment_id="c1", boot_vpu=10, ipv6_prefix_length=120
    )
    assert len(started) == 1


# ------------------------------------------------------------------ 抢机成功路径


def test_the_capacity_worker_hands_the_prefix_to_the_post_launch_step(monkeypatch):
    pytest.importorskip("fastapi")
    import web.backend.launch_service as launch_service
    from web.backend.db import SessionLocal, init_db
    from web.backend.models import CapacityJob, Tenant, User
    from web.backend.worker import Worker

    class _Result:
        ok = True
        message = "创建成功"
        data = {"instance_id": "ocid1.instance.oc1..new"}
        work_request_id = ""

    class _FakeSession:
        def launch_from_payload(self, payload, root_password="", custom_user_data="", idempotency_key=""):
            return _Result()

        def get_free_quota_usage(self, free_only_mode: bool = True):
            r = OperationResult(ok=True, message="")
            r.data = {
                "account_tier": "free",
                "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0},
                "remaining": {"a1_ocpu": 4.0, "a1_memory_gb": 24.0, "e2_micro_count": 2, "block_storage_gb": 200.0},
            }
            return r

    class _FakeSessions:
        def get(self, _cfg):
            return _FakeSession()

    scheduled: list[dict] = []
    monkeypatch.setattr(
        launch_service, "schedule_post_launch_adjustments", lambda *a, **k: scheduled.append(k)
    )

    init_db()
    with SessionLocal() as db:
        user = User(username=f"ipv6-prefix-{os.getpid()}-{datetime.now().timestamp()}", password_hash="x")
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
            launch_payload={
                **_payload(assign_ipv6_ip=True, ipv6_prefix_length=112),
                "boot_volume_vpus_per_gb": 10,
                "nsg_ids": ["nsg1"],
            },
            interval_sec=180,
            max_attempts=200,
            attempts=0,
            # 远在过去：一次 tick 最多取 20 个到期任务、按 next_run_at 排序，同进程里
            # 别的测试模块留下的任务不能把这一条挤出本轮。
            next_run_at=datetime.now(timezone.utc) - timedelta(days=365),
        )
        db.add(job)
        db.commit()
        job_id, owner_id, tenant_id = job.id, user.id, tenant.id

    worker = Worker()
    worker.sessions = _FakeSessions()
    with SessionLocal() as db:
        worker.tick_capacity(db)
    with SessionLocal() as db:
        assert db.get(CapacityJob, job_id).status == "success"

    mine = [kw for kw in scheduled if kw.get("owner_id") == owner_id]
    assert mine, "抢机成功后没有安排 IPv6 地址段"
    kw = mine[0]
    assert kw["ipv6_prefix_length"] == 112
    assert kw["instance_id"] == "ocid1.instance.oc1..new"
    assert kw["owner_id"] == owner_id and kw["tenant_id"] == tenant_id
