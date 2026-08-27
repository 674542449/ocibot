"""给一台实例分配一整块 IPv6（CreateIpv6 的 cidrPrefixLength）。

## 为什么需要这个

用户想让一台机器用上几百到几千个 IPv6 做出口。直觉是「拿一个 /64 路由到机器上」
—— 那是 Vultr / Hetzner 的模型。OCI 不是：/64 是**子网**（二层域），VCN 做源地址
校验，没在 API 里注册过的地址发不出去。

但 Oracle 在 2025-08-22 上线了 `cidrPrefixLength`：一个 IPv6 地址**对象**可以直接
是一整个 CIDR 块。一个 /112 就是 65,536 个地址，而且只占 VNIC 那 32 个地址对象
配额里的 **1 个**。逐个 CreateIpv6 建几千个的路子会在第 32 个上撞墙。

## 文档钉死的几条（这些数字错了就是一条含糊的 400）

- 掩码取值 **80–128**，且**必须被 4 整除**（"Custom mask values between 80 and 128
  are supported"、"The mask value must be divisible by 4 without any remainder"）。
- **/64 不在范围内** —— 它是子网本身的大小。
- 地址掩码**必须作为辅助 IP 分配**，也就是 VNIC 上得先有一个常规 IPv6。
- 子网前缀的**首尾各一个 /80** 保留给临时地址，不能用作掩码块。
- 每 VNIC 32 个 IPv6 地址对象（服务限额页 "Secondary Private IPv6 addresses"；
  IPv4 那条是 64，别记串）。
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from app.oci_client import (
    _IPV6_MASK_MAX,
    _IPV6_MASK_MIN,
    _IPV6_OBJECTS_PER_VNIC,
    PrimaryNetworkInfo,
    TenantSession,
    ipv6_prefix_choices,
    validate_ipv6_prefix_length,
)

oci = pytest.importorskip("oci")


# ---------------------------------------------------------------- 参数校验


def test_a_slash_64_is_rejected_with_the_reason_not_just_out_of_range():
    """/64 是最容易想当然的值，而它错得**有理由** —— 别家 VPS 给的正是「路由一个
    /64 给你」。只回一句「超出范围」等于让用户以为是面板小气。"""
    msg = validate_ipv6_prefix_length(64)
    assert msg
    assert "子网" in msg, "没解释 /64 为什么不行"
    assert f"/{_IPV6_MASK_MIN}" in msg, "没告诉用户能拿到的最大块是多少"


@pytest.mark.parametrize("n", [80, 84, 112, 124, 128])
def test_valid_masks_pass(n):
    assert validate_ipv6_prefix_length(n) == ""


@pytest.mark.parametrize("n", [79, 129, 132, 8, 1])
def test_out_of_range_masks_are_rejected(n):
    assert validate_ipv6_prefix_length(n)


@pytest.mark.parametrize("n", [81, 82, 113, 127])
def test_masks_not_divisible_by_four_are_rejected(n):
    msg = validate_ipv6_prefix_length(n)
    assert "4" in msg
    # 顺手告诉用户最近的合法值 —— 否则他得自己算。
    assert f"/{n - n % 4}" in msg


def test_empty_means_a_single_address_ie_the_old_behaviour():
    for blank in (None, "", 0):
        assert validate_ipv6_prefix_length(blank) == ""


def test_garbage_is_rejected_without_blowing_up():
    assert validate_ipv6_prefix_length("abc")
    assert validate_ipv6_prefix_length([1])


def test_every_offered_choice_is_actually_valid():
    """界面上列出来的每一个都必须是 Oracle 会接受的 —— 列一个会被拒的选项，
    等于让用户替我们发现 bug。"""
    choices = ipv6_prefix_choices()
    assert choices
    for c in choices:
        n = c["prefix_length"]
        assert validate_ipv6_prefix_length(n) == "", f"/{n} 被列出来了但不合法"
        assert c["count"] == 1 << (128 - n)
    assert 64 not in [c["prefix_length"] for c in choices]
    assert {c["prefix_length"] for c in choices} <= set(range(_IPV6_MASK_MIN, _IPV6_MASK_MAX + 1))


def test_the_frontend_does_not_offer_a_slash_64_either():
    """前端那个下拉是用户唯一会看的地方。列上 /64 就等于邀请他去点一个必失败的选项。"""
    ui = pathlib.Path("web/frontend/src/views/InstanceDetailView.vue").read_text(encoding="utf-8")
    assert "ipv6Choices" in ui
    block = ui.split("const ipv6Choices", 1)[1].split("]", 1)[0]
    assert "n: 64" not in block.replace(" ", "").replace("n:64", "n: 64")
    for n in (120, 112, 96, 80):
        assert f"n: {n}" in block


# ---------------------------------------------------------------- 打桩


def _Resp(data):
    """返回**真的** oci.Response，不是自己捏的 SimpleNamespace。

    `oci.pagination.list_call_get_all_results` 会依次读 next_page / has_next_page /
    status / request …… 自己捏的桩每加一处分页就少一个属性，而缺属性时它抛的异常会被
    上层 `except Exception: return -1` 吞掉 —— 于是配额闸门**静默失效**，测试却看不出来。
    这个坑本仓修过一次（0.4.97 的 _Resp），别再犯第二次。
    """
    return oci.Response(status=200, headers={}, data=data, request=None)


class FakeNetwork:
    """够用的假 VirtualNetworkClient。

    每个方法都带 `**kwargs` —— 真实 SDK 方法签名都是 (..., **kwargs)，面板会往里
    传 retry_strategy。桩不收的话，一个本该无害的调用会变成 TypeError。
    （这个坑本仓踩过不止一次。）
    """

    def __init__(self, existing_ipv6=0):
        self.created: list[oci.core.models.CreateIpv6Details] = []
        self.existing_ipv6 = existing_ipv6
        self.raise_400_on_prefix = False
        self.subnets = {
            "subnet-1": SimpleNamespace(
                id="subnet-1",
                vcn_id="vcn-1",
                compartment_id="comp",
                ipv6_cidr_block="2603:c020::/64",
                ipv6_cidr_blocks=["2603:c020::/64"],
                route_table_id="rt-1",
                lifecycle_state="AVAILABLE",
                prohibit_public_ip_on_vnic=False,
            )
        }

    def get_subnet(self, subnet_id, **_kw):
        return _Resp(self.subnets[subnet_id])

    def list_ipv6s(self, **kwargs):
        return _Resp([SimpleNamespace(id=f"ip-{i}") for i in range(self.existing_ipv6)])

    def create_ipv6(self, details, **_kw):
        if self.raise_400_on_prefix and getattr(details, "cidr_prefix_length", None):
            raise oci.exceptions.ServiceError(
                400, "InvalidParameter", {}, "cidrPrefixLength is not supported"
            )
        self.created.append(details)
        n = getattr(details, "cidr_prefix_length", None)
        return _Resp(
            SimpleNamespace(
                id=f"ocid1.ipv6.{len(self.created)}",
                ip_address="2603:c020::1000",
                cidr_prefix_length=n,
            )
        )


def _session(net, ipv6_addresses=()):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(id="t1", tenancy_ocid="tenancy", compartment_ocid="comp")
    s._network = net
    s.resolve_compartment = lambda: "comp"  # type: ignore[method-assign]
    s.resolve_primary_network = lambda *_a, **_k: PrimaryNetworkInfo(  # type: ignore[method-assign]
        vnic_id="vnic-1",
        subnet_id="subnet-1",
        private_ip_id="pip-1",
        private_ip_compartment_id="comp",
        ipv6_addresses=list(ipv6_addresses),
    )
    s.ensure_subnet_ipv6 = lambda *_a, **_k: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, message="", data={"created": False}
    )
    s.ensure_ipv6_internet_access = lambda *_a, **_k: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, message="公网路由已就绪"
    )
    s._ensure_ipv6_rules_on_managed_nsgs = lambda *_a, **_k: ""  # type: ignore[method-assign]
    return s


# ---------------------------------------------------------------- 行为


def test_the_prefix_length_actually_reaches_the_api():
    net = FakeNetwork()
    result = _session(net, ipv6_addresses=["2603:c020::1"]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert result.ok, result.message
    assert net.created, "根本没调 create_ipv6"
    assert net.created[-1].cidr_prefix_length == 112


def test_an_invalid_mask_never_reaches_oracle():
    """校验在本地做，不拿一次注定失败的请求去换一条含糊的 400。"""
    net = FakeNetwork()
    result = _session(net).assign_public_ipv6("inst", "comp", cidr_prefix_length=64)
    assert not result.ok
    assert not net.created, "非法掩码不该发出任何请求"


def test_having_an_address_already_does_not_short_circuit_a_block_request():
    """「已经有 IPv6 了」对单地址来说是终点，对「我要一块做出口池」恰恰相反 ——
    而且文档要求掩码块必须作为辅助 IP，本来就该在已有地址之上加。"""
    net = FakeNetwork()
    s = _session(net, ipv6_addresses=["2603:c020::1"])

    # 不要块：维持老行为，直接报告已有地址，不新建。
    plain = s.assign_public_ipv6("inst", "comp")
    assert plain.ok and not net.created

    # 要块：必须真的建出来。
    block = s.assign_public_ipv6("inst", "comp", cidr_prefix_length=112)
    assert block.ok and len(net.created) == 1


def test_an_empty_vnic_gets_a_plain_address_seeded_first():
    """文档：地址掩码**必须作为辅助 IP 分配**。空网卡直接要块会被 Oracle 拒掉，
    而那条错误完全看不出少的是这一步。"""
    net = FakeNetwork()
    result = _session(net, ipv6_addresses=[]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert result.ok, result.message
    assert len(net.created) == 2, "应该先补一个常规地址，再建块"
    assert getattr(net.created[0], "cidr_prefix_length", None) is None
    assert net.created[1].cidr_prefix_length == 112
    assert result.data["seeded"]
    assert "辅助" in result.message


def test_the_per_vnic_object_budget_is_checked_before_calling_oracle():
    """每 VNIC 32 个地址对象。撞上限时 Oracle 给的是一条看不出所以然的 400。"""
    net = FakeNetwork(existing_ipv6=_IPV6_OBJECTS_PER_VNIC)
    result = _session(net, ipv6_addresses=["2603:c020::1"]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert not result.ok
    assert str(_IPV6_OBJECTS_PER_VNIC) in result.message
    assert not net.created


def test_an_unreadable_object_count_does_not_block_the_assignment():
    """闸门只是提前说明白，不是安全边界。一次列举失败不该把整个功能挡掉。"""
    net = FakeNetwork()
    net.list_ipv6s = lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom"))
    result = _session(net, ipv6_addresses=["2603:c020::1"]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert result.ok, result.message


def test_the_result_says_how_many_addresses_and_admits_what_it_did_not_do():
    """「已分配」不能让人以为机器里立刻就能用了 —— 块分给了网卡，实例内部还得自己配，
    而这一步 Oracle 文档没写。不说破就是把一个已知的坑留给用户去踩。"""
    net = FakeNetwork()
    result = _session(net, ipv6_addresses=["2603:c020::1"]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert result.ok
    assert "65,536" in result.message
    assert result.data["address_count"] == 65536
    assert result.data["cidr_prefix_length"] == 112
    assert "/112" in result.data["ipv6"]
    assert "实例内部" in result.message and "文档" in result.message


def test_a_400_on_a_block_explains_the_two_likely_causes():
    """这个字段 2025-08-22 才上线。区域没铺到时 Oracle 回一条看不出所以然的 400，
    不说破的话用户会以为是自己参数写错了。"""
    net = FakeNetwork()
    net.raise_400_on_prefix = True
    result = _session(net, ipv6_addresses=["2603:c020::1"]).assign_public_ipv6(
        "inst", "comp", cidr_prefix_length=112
    )
    assert not result.ok
    assert "2025-08-22" in result.message
    assert "/80" in result.message, "没提首尾 /80 保留这条"


def test_a_plain_assignment_is_unchanged_by_all_of_this():
    """老路径一个字节都不该变 —— 只想要一个 IPv6 的人不该被这个功能影响。"""
    net = FakeNetwork()
    result = _session(net, ipv6_addresses=[]).assign_public_ipv6("inst", "comp")
    assert result.ok
    assert len(net.created) == 1
    assert getattr(net.created[0], "cidr_prefix_length", None) is None
    assert "地址块" not in result.message


def test_the_route_still_works_without_a_request_body():
    """老前端（和任何直接 curl 的人）不带 body。带 body 才是新行为。"""
    src = pathlib.Path("web/backend/routers/instances.py").read_text(encoding="utf-8")
    assert "payload: AssignIpv6Request | None = None" in src
    assert "payload.cidr_prefix_length if payload else None" in src
