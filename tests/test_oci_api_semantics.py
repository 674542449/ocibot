"""对照 OCI SDK 文档查出来的语义问题（0.4.169）。

方法名、参数名、模型字段都是对的 —— 这些 bug 全在「API 接受什么、返回什么」这一层，
所以每条测试都写明它依据的是哪句文档。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.oci_client import FirewallRuleSpec, OperationResult, TenantSession

oci = pytest.importorskip("oci")

T = TenantSession


class FakeBlockstorage:
    def __init__(self, *, size=50, vpu=10, hydrated=True):
        self.updated = None
        self.reads = 0
        self._bv = dict(size_in_gbs=size, vpus_per_gb=vpu, is_hydrated=hydrated)

    def get_boot_volume(self, _bv_id):
        self.reads += 1
        return SimpleNamespace(data=SimpleNamespace(lifecycle_state="AVAILABLE", **self._bv))

    def update_boot_volume(self, bv_id, details):
        self.updated = details
        return SimpleNamespace(data=SimpleNamespace(id=bv_id))


def _boot_session(**kw):
    s = T.__new__(T)
    s._blockstorage = FakeBlockstorage(**kw)
    s._compute = SimpleNamespace(
        get_instance=lambda _i: SimpleNamespace(data=SimpleNamespace(availability_domain="AD-1"))
    )
    s._find_boot_volume_id = lambda *a, **k: "bv-1"  # type: ignore[method-assign]
    return s


def test_boot_volumes_have_no_lower_cost_tier():
    """UpdateBootVolumeDetails.vpus_per_gb 的 Allowed values 只有 10 / 20 / 30-120。"""
    s = _boot_session()
    r = s.resize_boot_volume("i", "c", vpus_per_gb=0, wait_for_volume=False)
    assert not r.ok
    assert s._blockstorage.updated is None
    assert "低成本" not in r.message


def test_an_unchanged_size_is_not_sent_to_oracle():
    """size_in_gbs: "Has to be larger than the current size"。

    表单总是把大小和性能一起发，只改性能时大小等于当前值。
    """
    s = _boot_session(size=100, vpu=10)
    r = s.resize_boot_volume("i", "c", size_in_gbs=100, vpus_per_gb=20, wait_for_volume=False)
    assert r.ok, r.message
    assert s._blockstorage.updated.size_in_gbs is None
    assert s._blockstorage.updated.vpus_per_gb == 20


def test_a_size_only_change_does_not_wait_for_hydration():
    """VPU 更新在 hydrating 时会被 409 拒绝；没改性能就不该卷进这条限制。"""
    s = _boot_session(size=100, vpu=10, hydrated=False)
    r = s.resize_boot_volume(
        "i", "c", size_in_gbs=200, vpus_per_gb=10, wait_for_volume=False, hydration_timeout=1
    )
    assert r.ok, r.message
    assert s._blockstorage.updated.size_in_gbs == 200
    assert s._blockstorage.updated.vpus_per_gb is None


def test_no_change_at_all_makes_no_write():
    s = _boot_session(size=100, vpu=20)
    r = s.resize_boot_volume("i", "c", size_in_gbs=100, vpus_per_gb=20, wait_for_volume=False)
    assert r.ok
    assert s._blockstorage.updated is None
    assert r.data["unchanged"] is True


def test_shrinking_is_still_refused():
    s = _boot_session(size=100)
    r = s.resize_boot_volume("i", "c", size_in_gbs=60, wait_for_volume=False)
    assert not r.ok
    assert s._blockstorage.updated is None


# ------------------------------------------------------------------ 块卷


class FakeVolumes:
    def __init__(self, *, size=50, vpu=10):
        self.created = None
        self.updated = None
        self._vol = dict(size_in_gbs=size, vpus_per_gb=vpu)

    def create_volume(self, details):
        self.created = details
        return SimpleNamespace(
            data=SimpleNamespace(
                id="v", display_name="v", size_in_gbs=details.size_in_gbs,
                lifecycle_state="PROVISIONING", availability_domain="AD-1",
            )
        )

    def get_volume(self, _vid):
        return SimpleNamespace(data=SimpleNamespace(**self._vol))

    def update_volume(self, _vid, details):
        self.updated = details


def _volume_session(**kw):
    s = T.__new__(T)
    s._blockstorage = FakeVolumes(**kw)
    return s


def test_a_block_volume_can_be_created_at_lower_cost():
    """CreateVolumeDetails.vpus_per_gb: "`0`: Represents Lower Cost option"。

    以前 `vpus_per_gb or 10` 把 0 吃掉，选了低成本实际建成平衡档，还多付钱。
    """
    s = _volume_session()
    r = s.create_block_volume(
        compartment_id="c", availability_domain="AD-1", size_in_gbs=50, vpus_per_gb=0
    )
    assert r.ok, r.message
    assert s._blockstorage.created.vpus_per_gb == 0


def test_create_block_volume_defaults_to_balanced_when_unset():
    s = _volume_session()
    r = s.create_block_volume(
        compartment_id="c", availability_domain="AD-1", size_in_gbs=50, vpus_per_gb=None
    )
    assert r.ok, r.message
    assert s._blockstorage.created.vpus_per_gb == 10


def test_the_route_does_not_swallow_zero_vpus():
    from web.backend.routers import storage

    assert "vpus_per_gb=body.vpus_per_gb," in inspect.getsource(storage.create_block_volume)


def test_a_block_volume_update_drops_the_fields_that_did_not_change():
    """UpdateVolumeDetails.size_in_gbs 同样要求 "larger than the current size"。"""
    s = _volume_session(size=100, vpu=10)
    r = s.update_block_volume("v", size_in_gbs=100, vpus_per_gb=20)
    assert r.ok, r.message
    assert s._blockstorage.updated.size_in_gbs is None
    assert s._blockstorage.updated.vpus_per_gb == 20


def test_a_block_volume_update_with_no_change_makes_no_write():
    s = _volume_session(size=100, vpu=10)
    r = s.update_block_volume("v", size_in_gbs=100, vpus_per_gb=10)
    assert r.ok
    assert s._blockstorage.updated is None


def test_zero_vpus_is_labelled_lower_cost():
    assert T._volume_perf_label(0) == "低成本"
    assert T._volume_perf_label(10) == "平衡"


# ------------------------------------------------------------------ 引导卷列表


def _bv(bv_id):
    return SimpleNamespace(
        id=bv_id, display_name=bv_id, size_in_gbs=50, vpus_per_gb=10, is_hydrated=True,
        lifecycle_state="AVAILABLE", availability_domain="AD-1", compartment_id="c",
        time_created=None,
    )


def _boot_list_session(attachments):
    s = T.__new__(T)
    s.tenant = SimpleNamespace(tenancy_ocid="t", compartment_ocid="c")
    s._blockstorage = SimpleNamespace(
        list_boot_volumes=lambda **k: SimpleNamespace(data=[_bv("bv-1"), _bv("bv-2")])
    )
    s._compute = SimpleNamespace(
        list_boot_volume_attachments=lambda ad, cid: SimpleNamespace(data=attachments),
        get_instance=lambda iid: SimpleNamespace(data=SimpleNamespace(display_name=f"name-{iid}")),
    )
    s.list_availability_domains = lambda: ["AD-1"]  # type: ignore[method-assign]
    s.resolve_compartment = lambda: "c"  # type: ignore[method-assign]
    return s


@pytest.fixture
def plain_pagination(monkeypatch):
    def _call(fn, *args, **kwargs):
        kwargs.pop("retry_strategy", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(oci.pagination, "list_call_get_all_results", _call, raising=False)


def _att(bv_id, iid, state):
    return SimpleNamespace(boot_volume_id=bv_id, instance_id=iid, lifecycle_state=state)


def test_a_detached_boot_volume_is_an_orphan(plain_pagination):
    """服务端会把 DETACHED 的历史附件一起返回。

    不筛的话，实例终止后保留下来的卷显示「已挂载」，界面没有删除入口，
    orphan_boot_count 也算不到它 —— 正是这个计数告诉用户 200 GB 免费额度被谁占了。
    """
    s = _boot_list_session([_att("bv-1", "dead", "DETACHED")])
    r = s.list_boot_volumes(include_subcompartments=False)
    assert r.ok, r.message
    by_id = {v["id"]: v for v in r.data["volumes"]}
    assert by_id["bv-1"]["instance_id"] == ""
    assert r.data["summary"]["orphaned"] == 2


def test_the_live_attachment_wins_over_a_historic_one(plain_pagination):
    """同一块卷挂过两台机器：不管 API 按什么顺序返回，都要显示现在挂着的那台。"""
    s = _boot_list_session([_att("bv-1", "new", "ATTACHED"), _att("bv-1", "old", "DETACHED")])
    r = s.list_boot_volumes(include_subcompartments=False)
    by_id = {v["id"]: v for v in r.data["volumes"]}
    assert by_id["bv-1"]["instance_id"] == "new"
    assert by_id["bv-1"]["instance_name"] == "name-new"


# ------------------------------------------------------------------ NSG 创建


def _nsg_session():
    s = T.__new__(T)
    s.added = []
    s._network = SimpleNamespace(
        create_network_security_group=lambda _d: SimpleNamespace(data=SimpleNamespace(id="nsg-1"))
    )

    def _add(_nsg, specs):
        s.added.extend(specs)
        return OperationResult(ok=True, message="")

    s.add_nsg_rules = _add  # type: ignore[method-assign]
    return s


def test_the_ssh_only_nsg_does_not_claim_everything_is_open():
    s = _nsg_session()
    r = s.create_managed_nsg(vcn_id="v", compartment_id="c", display_name="x", open_all=False)
    assert r.ok
    assert "全部" not in r.message


def test_a_bare_nsg_has_no_rules_at_all():
    """一键修复的前提：这个 NSG 从空表开始，多一条都不再是「可达性不变」。"""
    s = _nsg_session()
    r = s.create_managed_nsg(vcn_id="v", compartment_id="c", display_name="x", bare=True)
    assert r.ok, r.message
    assert s.added == []


def test_ensure_instance_nsg_passes_bare_through():
    s = T.__new__(T)
    seen = {}

    def _create(**kw):
        seen.update(kw)
        return OperationResult(ok=True, message="", data={"nsg_id": "nsg-1"})

    s.create_managed_nsg = _create  # type: ignore[method-assign]
    s.resolve_primary_network = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
        nsg_ids=[], subnet_id="sub", vnic_id="vnic", ipv6_addresses=[]
    )
    s._network = SimpleNamespace(
        get_subnet=lambda _s: SimpleNamespace(data=SimpleNamespace(vcn_id="v", compartment_id="c")),
        update_vnic=lambda *a, **k: None,
    )
    assert s.ensure_instance_nsg("instance-id", "c", bare=True).ok
    assert seen["bare"] is True


# ------------------------------------------------------------------ 源端口


def _tcp_rule(*, src=None, dst=None):
    def _range(pair):
        return SimpleNamespace(min=pair[0], max=pair[1]) if pair else None

    return SimpleNamespace(
        id="", direction="", protocol="6", source="0.0.0.0/0", source_type="CIDR_BLOCK",
        is_stateless=False, description="", icmp_options=None, udp_options=None,
        tcp_options=SimpleNamespace(
            destination_port_range=_range(dst), source_port_range=_range(src)
        ),
    )


def test_a_rule_limited_by_source_port_is_not_widened_when_moved():
    """TcpOptions 有两个独立的范围。只留 destination_port_range 的话，一条「源端口 53」
    的规则会被读成目的端口全开，再被一键修复原样写进 NSG。"""
    norm = T._normalize_firewall_rule(_tcp_rule(src=(53, 53)), direction="INGRESS")
    assert norm["source_port"] == "53"
    assert T._norm_rule_to_spec(norm) is None


def test_repair_does_not_promise_unchanged_reachability_when_a_rule_stayed_behind():
    """没搬的规则也会被 tighten 从子网删掉，这时不能再说「可达性没有变化」。"""
    s = T.__new__(T)
    rule = T._normalize_firewall_rule(_tcp_rule(src=(53, 53)), direction="INGRESS")
    s.get_instance_firewall = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="",
        data={
            "groups": [{"id": "nsg-1", "display_name": "fw", "rules": []}],
            "security_lists": [{"id": "sl-1", "display_name": "sl", "rules": [rule]}],
            "security_lists_complete": True,
            "subnet_id": "sub",
            "has_ipv6": False,
        },
    )
    s.tighten_subnet_security_list = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True, message="", data={}
    )
    r = s.repair_instance_firewall("i", "c")
    assert r.ok, r.message
    assert "可达性没有变化" not in r.message
    assert "不会搬" in r.message


def test_a_plain_rule_still_moves():
    norm = T._normalize_firewall_rule(_tcp_rule(dst=(22, 22)), direction="INGRESS")
    assert norm["source_port"] == ""
    spec = T._norm_rule_to_spec(norm)
    assert spec is not None and spec.port_min == 22


# ------------------------------------------------------------------ 收紧的并发保护


def _tighten_session(*, update):
    s = T.__new__(T)
    s.tenant = SimpleNamespace(tenancy_ocid="t")

    def _get(_sl_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                display_name="open-security-list",
                freeform_tags={"managed_by": "oci-console-helper"},
                ingress_security_rules=[_tcp_rule(dst=(22, 22))],
            ),
            headers={"ETag": "etag-1"},
        )

    s._network = SimpleNamespace(
        get_subnet=lambda _s: SimpleNamespace(
            data=SimpleNamespace(security_list_ids=["sl-1"], vcn_id="", compartment_id="c")
        ),
        get_security_list=_get,
        update_security_list=update,
    )
    s._subnet_vnics_at_risk = lambda _s: ([], True)  # type: ignore[method-assign]
    return s


_STATE = OperationResult(ok=True, message="", data={"subnet_id": "sub", "has_ipv6": False})


def test_tighten_writes_with_the_etag_it_read():
    """这条路径的读和写之间隔着整段预检，是所有写路径里窗口最长的一条。"""
    seen = {}

    def _update(sl_id, details, **kw):
        seen.update(kw)

    s = _tighten_session(update=_update)
    r = s.tighten_subnet_security_list("i", "c", state=_STATE)
    assert r.ok, r.message
    assert seen.get("if_match") == "etag-1"


def test_a_concurrent_edit_stops_tighten():
    def _update(*a, **k):
        raise oci.exceptions.ServiceError(412, "NoEtagMatch", {}, "etag mismatch")

    s = _tighten_session(update=_update)
    r = s.tighten_subnet_security_list("i", "c", state=_STATE)
    assert not r.ok
    assert "刚被别处改过" in r.message


# ------------------------------------------------------------------ IPv6 安全规则


def _nsg_rule(direction, protocol, cidr, port=None, description=""):
    rng = SimpleNamespace(min=port, max=port) if port else None
    opts = SimpleNamespace(destination_port_range=rng, source_port_range=None) if rng else None
    return SimpleNamespace(
        id="r", direction=direction, protocol=protocol,
        source=cidr if direction == "INGRESS" else None,
        destination=cidr if direction == "EGRESS" else None,
        is_stateless=False, description=description, tcp_options=opts, udp_options=None,
        icmp_options=None,
    )


def _ipv6_session(existing, *, add_ok=True):
    s = T.__new__(T)
    s.added = []

    def _list(_nsg_id, **_k):
        return oci.Response(status=200, headers={}, data=list(existing), request=None)

    s._network = SimpleNamespace(
        get_network_security_group=lambda _n: SimpleNamespace(
            data=SimpleNamespace(freeform_tags={"ocibot_managed": "true"})
        ),
        list_network_security_group_security_rules=_list,
    )

    def _add(_nsg, specs):
        s.added.extend(specs)
        return OperationResult(ok=add_ok, message="" if add_ok else "写不进去")

    s.add_nsg_rules = _add  # type: ignore[method-assign]
    return s


def test_assigning_ipv6_does_not_open_every_port_on_a_locked_down_machine():
    """防火墙只开 22 的机器，IPv6 上也只能开 22。

    以前这里无条件写 all + ::/0：用户收紧过的规则在 IPv6 上全部作废，而且没有提示。
    """
    s = _ipv6_session(
        [_nsg_rule("INGRESS", "6", "0.0.0.0/0", 22), _nsg_rule("EGRESS", "all", "0.0.0.0/0")]
    )
    note = s._ensure_ipv6_rules_on_managed_nsgs(SimpleNamespace(nsg_ids=["nsg-1"]))
    ingress = [spec for spec in s.added if spec.direction == "INGRESS"]
    assert [(i.protocol, i.cidr, i.port_min) for i in ingress] == [("6", "::/0", 22)]
    assert [(e.protocol, e.cidr) for e in s.added if e.direction == "EGRESS"] == [("all", "::/0")]
    assert "2 条" in note


def test_an_open_machine_stays_open_on_ipv6():
    s = _ipv6_session(
        [_nsg_rule("INGRESS", "all", "0.0.0.0/0"), _nsg_rule("EGRESS", "all", "0.0.0.0/0")]
    )
    s._ensure_ipv6_rules_on_managed_nsgs(SimpleNamespace(nsg_ids=["nsg-1"]))
    assert {(spec.direction, spec.protocol, spec.cidr) for spec in s.added} == {
        ("INGRESS", "all", "::/0"),
        ("EGRESS", "all", "::/0"),
    }


def test_rules_from_a_private_source_are_not_copied():
    s = _ipv6_session([_nsg_rule("INGRESS", "6", "10.0.0.0/8", 22)])
    s._ensure_ipv6_rules_on_managed_nsgs(SimpleNamespace(nsg_ids=["nsg-1"]))
    assert s.added == []


def test_existing_ipv6_rules_are_not_written_twice():
    s = _ipv6_session(
        [_nsg_rule("INGRESS", "6", "0.0.0.0/0", 22), _nsg_rule("INGRESS", "6", "::/0", 22)]
    )
    s._ensure_ipv6_rules_on_managed_nsgs(SimpleNamespace(nsg_ids=["nsg-1"]))
    assert s.added == []


def test_a_failed_write_is_not_reported_as_added():
    """add_nsg_rules 失败时是**返回**，不是抛出。"""
    s = _ipv6_session([_nsg_rule("INGRESS", "6", "0.0.0.0/0", 22)], add_ok=False)
    note = s._ensure_ipv6_rules_on_managed_nsgs(SimpleNamespace(nsg_ids=["nsg-1"]))
    assert "已按现有" not in note
    assert "写不进去" in note


# ------------------------------------------------------------------ 控制台连接


def test_a_failed_delete_stops_the_console_connection_create():
    """每台实例只能有一个活动连接。旧的删不掉时继续创建，只会收到一个
    和真实原因无关的报错。"""
    s = T.__new__(T)
    created = []
    s.list_console_connections = lambda *a: [SimpleNamespace(id="old")]  # type: ignore[method-assign]
    s.delete_console_connection = lambda _i: OperationResult(  # type: ignore[method-assign]
        ok=False, message="没权限"
    )
    s._compute = SimpleNamespace(create_instance_console_connection=created.append)
    r = s.create_console_connection("i", "c", "ssh-rsa AAAA test")
    assert not r.ok
    assert "没权限" in r.message
    assert created == []


# ------------------------------------------------------------------ 规格变更文案


def test_the_shape_change_message_matches_what_oracle_does():
    """update_operation_constraint 默认 ALLOW_DOWNTIME：运行中的实例会被重启。"""
    s = T.__new__(T)
    s._compute = SimpleNamespace(
        get_instance=lambda _i: SimpleNamespace(data=SimpleNamespace(shape="VM.Standard.A1.Flex")),
        update_instance=lambda *a, **k: None,
    )
    r = s.update_instance_shape("i", 2, 12)
    assert r.ok, r.message
    assert "重启" in r.message
    assert "部分变更" not in r.message


def test_an_egress_rule_is_mirrored_as_egress():
    """_norm_rule_to_spec 固定产出入站，方向要由这里换回来。"""
    specs = T._ipv6_mirror_specs([_nsg_rule("EGRESS", "6", "0.0.0.0/0", 443)])
    assert specs == [
        FirewallRuleSpec(
            direction="EGRESS", protocol="6", cidr="::/0", port_min=443, port_max=443,
            description="(IPv6)",
        )
    ]
