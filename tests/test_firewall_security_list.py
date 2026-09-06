"""安全列表变成面板真正编辑的那一层。

## 为什么要有这一层

OCI 的生效规则是「**子网安全列表 ∪ VNIC 的所有 NSG**」（securityrules.htm）。
面板过去只编辑 NSG —— 而在 Oracle 控制台里建的机器根本没有 NSG，入站完全由安全
列表决定。对那批机器（多数）来说，面板的防火墙功能等于不存在：改什么都不生效。

参考实现 oci-launcher 没有这个毛病，原因很朴素 —— **它编辑的就是安全列表本身**：
VCN → 子网 → 那份安全列表，加规则 / 删规则 / 全开 / 清空 / Cloudflare 全落在上面。
这个文件把那条路补进 OCIBot，并补上参考实现里几处会咬人的缺口。

## 相对参考实现补上的几处

* **etag（if_match）**：安全列表是读-改-写的，后写的人覆盖整份入站规则。
  参考实现没有并发保护，两个标签页同时改就会有一方的规则被静默吞掉。
* **内容键里带 source_type / source_port_range**：参考实现的 key 只看协议、来源和
  目的端口，于是一条带源端口范围的用户规则会和不带的撞键，「删这一条」会删掉
  用户没选的那条。服务网关规则（source 是服务名不是 CIDR）同理。
* **删除按完整身份、添加按作用去重**：参考实现两边都用同一个 key，于是
  「TCP 443 说明=网站」和「TCP 443 说明=API」删一条会掉两条。
* **UNKNOWN_ENUM_VALUE**：SDK 读到不认识的 source_type 会替换成这个字面量，
  读-改-写会把它原样发回去，破坏一条用户根本没碰过的规则。宁可整次停手。
* **出站绝不碰**：见下面那条专门的测试。
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from app.oci_client import FirewallRuleSpec, OperationResult, TenantSession

oci = pytest.importorskip("oci")

UI = pathlib.Path("web/frontend/src/views/InstanceDetailView.vue")
T = TenantSession


# ------------------------------------------------------------------ 出站安全


def test_writing_ingress_never_mentions_egress_in_the_request_body():
    """这是整个功能里**唯一**会造成子网级断网的写法。

    Python SDK 的序列化器只丢掉 None 的字段（base_client:
    ``if getattr(obj, attr) is not None``）。所以不设 egress_security_rules 时，
    请求体里根本没有那个键，OCI 保持原样 —— 这正是我们要的。

    但空列表 ``[]`` **不是 None**：它会序列化成 ``"egressSecurityRules": []``，
    把整个子网的出站规则清空。机器连不上外网，而界面只说「已清空入站」。
    所以永远不要写 ``egress_security_rules=... or []``。
    """
    from oci.base_client import BaseClient

    client = BaseClient.__new__(BaseClient)
    client.complex_type_mappings = oci.core.models.__dict__

    rule = oci.core.models.IngressSecurityRule(
        protocol="6", source="0.0.0.0/0", source_type="CIDR_BLOCK", is_stateless=False
    )
    unset = oci.core.models.UpdateSecurityListDetails(ingress_security_rules=[rule])
    assert "egressSecurityRules" not in client.sanitize_for_serialization(unset)

    # 反面：真写了空列表就会出现那个键 —— 这一半证明上面那半不是巧合。
    wipes = oci.core.models.UpdateSecurityListDetails(
        ingress_security_rules=[rule], egress_security_rules=[]
    )
    assert "egressSecurityRules" in client.sanitize_for_serialization(wipes)


def test_the_writer_only_ever_builds_ingress_details():
    import inspect

    src = inspect.getsource(T._write_security_list_ingress)
    assert "egress_security_rules" not in src.split('"""')[2], "写回路径碰了出站"


# ------------------------------------------------------------------ 来源归一化


def test_a_bare_ip_becomes_a_single_host_cidr():
    assert T.normalize_cidr_source("1.2.3.4") == ("1.2.3.4/32", False)
    assert T.normalize_cidr_source("2001:db8::1") == ("2001:db8::1/128", True)


def test_host_bits_are_zeroed_so_the_same_rule_gets_the_same_key():
    """``1.2.3.4/24`` 和 ``1.2.3.0/24`` 是同一条规则的两种写法。

    不归一化的话：幂等添加会重复写一条，按键删除会找不到。
    """
    assert T.normalize_cidr_source("1.2.3.4/24")[0] == "1.2.3.0/24"
    assert T.normalize_cidr_source("2001:0db8:0000::1")[0] == "2001:db8::1/128"


def test_a_bad_source_is_rejected_not_guessed():
    for bad in ("", "   ", "不是IP", "1.2.3.999", "1.2.3.4/99"):
        with pytest.raises(ValueError):
            T.normalize_cidr_source(bad)


# ------------------------------------------------------------------ 规则构造


def _spec(**kw):
    base = dict(direction="INGRESS", protocol="6", cidr="0.0.0.0/0")
    base.update(kw)
    return FirewallRuleSpec(**base)


def test_icmp_follows_the_address_family_of_the_source():
    """IPv6 来源必须是协议 58。

    写成 1 不一定报错，但 IPv6 根本不跑协议 1 —— 界面会显示「ping 已放行」，
    而 ping 一直不通。反方向同理：IPv4 来源上的 58 也要纠回 1。
    """
    assert T._security_list_ingress_model(_spec(protocol="1", cidr="::/0")).protocol == "58"
    assert T._security_list_ingress_model(_spec(protocol="58", cidr="0.0.0.0/0")).protocol == "1"


def test_ports_are_never_attached_to_an_all_protocol_rule():
    """``tcp_options`` 对 ``protocol="all"`` 是非法的（文档：valid only for TCP）。

    options 必须在协议分支**里面**构造，不能先建好再挂。
    """
    rule = T._security_list_ingress_model(_spec(protocol="all", port_min=22, port_max=22))
    assert rule.tcp_options is None and rule.udp_options is None


def test_an_empty_description_is_omitted_not_sent_as_a_blank_string():
    """API 的 description 是 Min Length 1，而序列化器只丢 None、不丢空串。"""
    assert T._security_list_ingress_model(_spec()).description is None
    assert T._security_list_ingress_model(_spec(description="x" * 300)).description == "x" * 255


def test_udp_gets_udp_options_and_tcp_gets_tcp_options():
    udp = T._security_list_ingress_model(_spec(protocol="17", port_min=53, port_max=53))
    assert udp.udp_options is not None and udp.tcp_options is None
    tcp = T._security_list_ingress_model(_spec(protocol="6", port_min=53, port_max=53))
    assert tcp.tcp_options is not None and tcp.udp_options is None


# ------------------------------------------------------------------ 内容键


def _rule(**kw):
    base = dict(
        protocol="6",
        source="0.0.0.0/0",
        source_type="CIDR_BLOCK",
        is_stateless=False,
        description="",
        icmp_options=None,
        udp_options=None,
        tcp_options=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tcp_opts(lo, hi, src=None):
    return SimpleNamespace(
        destination_port_range=SimpleNamespace(min=lo, max=hi),
        source_port_range=SimpleNamespace(min=src[0], max=src[1]) if src else None,
    )


def test_no_port_options_is_a_different_rule_from_an_explicit_full_range():
    """「没写端口」= 全部端口，和显式 1-65535 在 OCI 里是两条不同的规则。

    键上把它们合并，删一条会连另一条一起删掉。
    """
    implicit = T._sl_rule_effect(_rule())
    explicit = T._sl_rule_effect(_rule(tcp_options=_tcp_opts(1, 65535)))
    assert implicit != explicit


def test_the_key_includes_the_source_port_range():
    """参考实现只看目的端口 —— 于是一条带源端口范围的用户规则会和不带的撞键，
    「删这一条」会删掉他没选的那条。这里补上。"""
    a = T._sl_rule_effect(_rule(tcp_options=_tcp_opts(80, 80)))
    b = T._sl_rule_effect(_rule(tcp_options=_tcp_opts(80, 80, src=(1024, 65535))))
    assert a != b


def test_the_key_includes_the_source_type():
    """服务网关规则的 source 是 ``oci-phx-objectstorage`` 这种服务名而不是 CIDR。

    不带 source_type，它就会和一条同名的 CIDR 规则撞键。
    """
    cidr = T._sl_rule_effect(_rule(source="oci-phx-objectstorage"))
    svc = T._sl_rule_effect(_rule(source="oci-phx-objectstorage", source_type="SERVICE_CIDR_BLOCK"))
    assert cidr != svc


def test_stateless_none_and_false_are_the_same_rule():
    """OCI 读回来是显式 false，我们自己构造的是 None。

    不归一就会把同一条规则认成两条，幂等添加于是每次都再写一遍。
    """
    assert T._sl_rule_effect(_rule(is_stateless=None)) == T._sl_rule_effect(
        _rule(is_stateless=False)
    )


def test_description_does_not_change_the_effect_but_does_change_the_delete_handle():
    """两条效果一样、只是说明不同的规则：

    * 对网络来说是同一条 —— 所以**添加**按作用去重，不会把同一个端口写两次；
    * 但用户在界面上点的是**某一行** —— 所以**删除**按完整身份，不会连坐。
    """
    a, b = _rule(description="网站"), _rule(description="API")
    assert T._sl_rule_effect(a) == T._sl_rule_effect(b)
    assert T._sl_rule_key(a) != T._sl_rule_key(b)


# ------------------------------------------------------------------ 会话装配


def _session(*, lists, ingress=None, etag="etag-1", has_ipv6=False, groups=None):
    """接了必要几根线的 TenantSession。

    ``lists`` 是 get_instance_firewall 会返回的那份摘要；``ingress`` 是
    get_security_list 读回来的真实规则对象。
    """
    s = T.__new__(T)
    s.written = []
    s.if_match = []
    s.tenant = SimpleNamespace(tenancy_ocid="ocid1.tenancy..root")
    rules = list(ingress or [])

    s.get_instance_firewall = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="",
        data={
            "subnet_id": "subnet-1",
            "has_ipv6": has_ipv6,
            "security_lists": lists,
            "security_lists_complete": True,
            "groups": list(groups or []),
        },
    )

    def _get(sl_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=sl_id,
                display_name="sl",
                ingress_security_rules=list(rules),
                egress_security_rules=[SimpleNamespace(protocol="all")],
            ),
            # 真实的响应头是 ``ETag``,SDK 把它放在 CaseInsensitiveDict 里。
            # 桩里写小写的话,代码退回成大小写敏感的查找仍然会绿 —— 而线上
            # 就静默丢掉了并发保护。
            headers={"ETag": etag},
        )

    def _update(sl_id, details, **kw):
        s.written.append((sl_id, list(details.ingress_security_rules or [])))
        s.if_match.append(kw.get("if_match"))
        # 出站字段必须原样是 None —— 传了就会被序列化,清空整个子网的出站。
        assert details.egress_security_rules is None
        return SimpleNamespace(data=None)

    s._network = SimpleNamespace(get_security_list=_get, update_security_list=_update)
    return s


def _sl(sl_id="sl-1", name="open-security-list", rules=None):
    return {"id": sl_id, "display_name": name, "rules": rules or []}


# ------------------------------------------------------------------ 添加


def test_adding_a_rule_writes_it_and_carries_the_etag():
    """etag 是并发保护:安全列表是读-改-写的,后写的人会覆盖整份入站规则。

    参考实现没有这一层,两个标签页同时改就有一方的规则被静默吞掉。
    """
    s = _session(lists=[_sl()])
    r = s.add_security_list_rules(
        "i", "c", security_list_id="sl-1", specs=[_spec(port_min=8080, port_max=8080)]
    )
    assert r.ok, r.message
    assert len(s.written[0][1]) == 1
    assert s.if_match == ["etag-1"]


def test_adding_the_same_rule_twice_is_a_no_op():
    """已经放行过的端口再写一条,只是白占掉 200 条配额里的一格。"""
    existing = T._security_list_ingress_model(_spec(port_min=22, port_max=22))
    s = _session(lists=[_sl()], ingress=[existing])
    r = s.add_security_list_rules(
        "i", "c", security_list_id="sl-1", specs=[_spec(port_min=22, port_max=22)]
    )
    assert r.ok
    assert not s.written, "重复的规则还是写进去了"
    assert (r.data or {}).get("skipped") == 1


def test_a_differently_written_but_identical_source_still_dedupes():
    """``1.2.3.0/24`` 和 ``1.2.3.4/24`` 是同一条规则。归一化如果发生在算键之后，
    这里就会重复写。"""
    existing = T._security_list_ingress_model(
        _spec(cidr="1.2.3.0/24", port_min=22, port_max=22)
    )
    s = _session(lists=[_sl()], ingress=[existing])
    r = s.add_security_list_rules(
        "i", "c", security_list_id="sl-1",
        specs=[_spec(cidr="1.2.3.4/24", port_min=22, port_max=22)],
    )
    assert r.ok and not s.written


def test_it_refuses_before_the_write_when_the_batch_would_blow_the_200_cap():
    """撞上限时给一句能看懂的话，而不是 Oracle 的 400；而且必须在写**之前**判断，
    否则会部分写入。"""
    full = [
        T._security_list_ingress_model(_spec(port_min=p, port_max=p))
        for p in range(1000, 1000 + 199)
    ]
    s = _session(lists=[_sl()], ingress=full)
    r = s.add_security_list_rules(
        "i", "c", security_list_id="sl-1",
        specs=[_spec(port_min=p, port_max=p) for p in (2000, 2001)],
    )
    assert r.ok is False
    assert not s.written
    assert "200" in r.message


def test_an_invalid_source_fails_the_add_without_touching_the_list():
    s = _session(lists=[_sl()])
    r = s.add_security_list_rules("i", "c", security_list_id="sl-1", specs=[_spec(cidr="nope")])
    assert r.ok is False and not s.written


# ------------------------------------------------------------------ 删除


def test_deleting_by_key_removes_exactly_that_rule():
    keep = T._security_list_ingress_model(_spec(port_min=22, port_max=22))
    drop = T._security_list_ingress_model(_spec(port_min=3306, port_max=3306))
    s = _session(lists=[_sl()], ingress=[keep, drop])
    r = s.delete_security_list_rules(
        "i", "c", security_list_id="sl-1", rule_keys=[T._sl_rule_key(drop)]
    )
    assert r.ok, r.message
    left = s.written[0][1]
    assert len(left) == 1 and left[0].tcp_options.destination_port_range.min == 22


def test_deleting_a_rule_that_is_already_gone_says_so_instead_of_writing():
    """别处刚删过的话，这里再写一遍等于把用户看到的旧状态推回去。"""
    s = _session(lists=[_sl()], ingress=[])
    r = s.delete_security_list_rules("i", "c", security_list_id="sl-1", rule_keys=["不存在"])
    assert r.ok is False and not s.written
    assert "刷新" in r.message


def test_two_rules_that_differ_only_in_description_are_deleted_one_at_a_time():
    a = T._security_list_ingress_model(_spec(port_min=443, port_max=443, description="网站"))
    b = T._security_list_ingress_model(_spec(port_min=443, port_max=443, description="API"))
    s = _session(lists=[_sl()], ingress=[a, b])
    r = s.delete_security_list_rules("i", "c", security_list_id="sl-1", rule_keys=[T._sl_rule_key(a)])
    assert r.ok
    left = s.written[0][1]
    assert [x.description for x in left] == ["API"]


# ------------------------------------------------------------------ 定位安全列表


def test_it_refuses_to_guess_when_the_subnet_has_more_than_one_list():
    """一个子网最多挂 5 份安全列表。随手挑第一份很可能挑中 Oracle 自带的那份，
    而用户以为改的是别的。"""
    s = _session(lists=[_sl("sl-1", "A"), _sl("sl-2", "B")])
    r = s.add_security_list_rules("i", "c", specs=[_spec()])
    assert r.ok is False and not s.written
    assert "A" in r.message and "B" in r.message


def test_a_single_list_is_used_without_being_named():
    s = _session(lists=[_sl()])
    assert s.add_security_list_rules("i", "c", specs=[_spec()]).ok


def test_it_refuses_a_security_list_that_is_not_on_this_instance_subnet():
    """否则从实例详情页就能改到租户里任意一份安全列表 —— 那和「在这台机器上操作
    防火墙」是两回事。"""
    s = _session(lists=[_sl()])
    r = s.add_security_list_rules("i", "c", security_list_id="sl-别人的", specs=[_spec()])
    assert r.ok is False and not s.written
    assert "拒绝修改" in r.message


# ------------------------------------------------------------------ 全开 / 清空


def test_open_all_adds_a_v4_rule_and_only_adds_v6_when_the_vcn_has_it():
    """IPv4-only 的 VCN 会拒绝 ``::/0`` 的规则，整批一起失败。"""
    s = _session(lists=[_sl()])
    assert s.open_all_security_list("i", "c", security_list_id="sl-1").ok
    assert len(s.written[0][1]) == 1

    s6 = _session(lists=[_sl()], has_ipv6=True)
    assert s6.open_all_security_list("i", "c", security_list_id="sl-1").ok
    assert len(s6.written[0][1]) == 2


def test_clear_empties_ingress_and_says_what_that_costs():
    """清空会连 Oracle 默认的 ICMP 一起带走。删掉 Path MTU Discovery 那条不会
    立刻断网，而是让大包静默丢失（ssh 能连、scp 卡死）—— 必须说。"""
    icmp = SimpleNamespace(
        protocol="1", source="0.0.0.0/0", source_type="CIDR_BLOCK", is_stateless=False,
        description="", tcp_options=None, udp_options=None,
        icmp_options=SimpleNamespace(type=3, code=4),
    )
    s = _session(lists=[_sl()], ingress=[icmp])
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok
    assert s.written[0][1] == []
    assert "scp" in r.message


def test_clear_warns_when_another_list_still_allows_inbound():
    """生效规则是并集 —— 清空一份不等于端口关上了。这个坑面板踩过三次。"""
    other = _sl("sl-2", "别人的列表", rules=[{"direction": "INGRESS", "protocol": "6"}])
    s = _session(lists=[_sl(), other], ingress=[_rule()])
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok
    assert "⚠" in r.message and "别人的列表" in r.message
    # 不能反过来断言「端口还开着」—— 那份列表也可能只放行了 VCN 内的 ICMP。
    # 诚实的说法是把来源点名，把结论留给它们的实际内容。
    assert "要看它们里面写了什么" in r.message


def test_a_wide_open_neighbour_makes_the_clear_pointless_and_says_so():
    """同子网另一份列表里有一条「全部协议 / 0.0.0.0/0」时，这次清空**一个端口都没关上**。

    这句话比「还有别的来源」强得多，也确实成立 —— 所以单独说。
    """
    wide = _sl(
        "sl-2", "别人的列表",
        rules=[{"direction": "INGRESS", "protocol": "all", "port": "全部", "cidr": "0.0.0.0/0"}],
    )
    s = _session(lists=[_sl(), wide], ingress=[_rule()])
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok
    assert "并没有关上任何端口" in r.message and "别人的列表" in r.message


def test_deleting_next_to_a_wide_open_rule_says_the_delete_changed_nothing():
    """面板自己的「放行全部端口」就会写一条 all/0.0.0.0/0。之后用户删掉旁边那条
    3306，确认框如果说「3306 将无法从外部访问」就是彻头彻尾的假话。"""
    wide = T._security_list_ingress_model(_spec(protocol="all"))
    drop = T._security_list_ingress_model(_spec(port_min=3306, port_max=3306))
    s = _session(lists=[_sl()], ingress=[wide, drop])
    r = s.delete_security_list_rules(
        "i", "c", security_list_id="sl-1", rule_keys=[T._sl_rule_key(drop)]
    )
    assert r.ok
    assert "并没有关上任何端口" in r.message


def test_a_plain_delete_does_not_shout_when_nothing_is_wide_open():
    """删掉一条规则时，别的来源还在放行是常态。每次都标红等于把红框用废 ——
    ⚠ 留给「这次删除白做了」那种情况。"""
    a = T._security_list_ingress_model(_spec(port_min=22, port_max=22))
    b = T._security_list_ingress_model(_spec(port_min=80, port_max=80))
    s = _session(lists=[_sl()], ingress=[a, b])
    r = s.delete_security_list_rules(
        "i", "c", security_list_id="sl-1", rule_keys=[T._sl_rule_key(b)]
    )
    assert r.ok
    assert "⚠" not in r.message


def test_clear_also_looks_at_the_nsgs_not_just_the_other_lists():
    """并集里还有 NSG。只查安全列表的话，一台 NSG 上还开着 22 的机器会被讲成
    「已经收紧」—— 而那正是这个项目已经修过三次的那类误导。"""
    s = _session(
        lists=[_sl()],
        groups=[{"display_name": "nsg-web", "rules": [{"direction": "INGRESS", "protocol": "6"}]}],
        ingress=[_rule()],
    )
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok
    assert "⚠" in r.message and "nsg-web" in r.message


def test_clear_says_plainly_when_nothing_else_allows_inbound():
    """真的关上了就要说清楚「包括 SSH」—— 这是用户会被锁在门外的那一半。"""
    s = _session(lists=[_sl()], ingress=[_rule()])
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok
    assert "包括 SSH" in r.message
    assert "⚠" not in r.message.split("包括 SSH")[0].split(chr(10))[-1]


def test_clearing_an_already_empty_list_writes_nothing():
    s = _session(lists=[_sl()], ingress=[])
    r = s.clear_security_list_rules("i", "c", security_list_id="sl-1")
    assert r.ok and not s.written


# ------------------------------------------------------------------ 读-改-写的坑


def test_an_unrecognised_source_type_aborts_the_whole_write():
    """SDK 读到它不认识的 source_type 会替换成 ``UNKNOWN_ENUM_VALUE``。

    读-改-写会把这个字面量原样发回去，破坏一条**用户根本没碰过**的规则。
    宁可整次停手，也不要改坏别人的东西。
    """
    poisoned = _rule(source_type="UNKNOWN_ENUM_VALUE")
    s = _session(lists=[_sl()], ingress=[poisoned])
    r = s.add_security_list_rules(
        "i", "c", security_list_id="sl-1", specs=[_spec(port_min=22, port_max=22)]
    )
    assert r.ok is False and not s.written
    assert "什么都没改" in r.message


def test_a_concurrent_edit_is_reported_as_such_not_as_a_bare_error():
    """412 = 别处刚改过。裸报 502 的话，用户不知道自己的改动为什么没了。"""
    s = _session(lists=[_sl()])

    def _boom(sl_id, details, **kw):
        raise oci.exceptions.ServiceError(412, "PreconditionFailed", {}, "etag mismatch")

    s._network.update_security_list = _boom
    r = s.add_security_list_rules("i", "c", security_list_id="sl-1", specs=[_spec()])
    assert r.ok is False
    assert "刷新" in r.message


# ------------------------------------------------------------------ 界面


def test_the_page_no_longer_calls_security_lists_read_only():
    ui = UI.read_text(encoding="utf-8")
    assert "子网安全列表 · 只读" not in ui
    assert "安全列表规则请在 Oracle 控制台修改" not in ui


def test_the_security_list_table_has_a_delete_button_keyed_on_content():
    """下标当 key 的话，删一行之后数组重排，Vue 会把 :disabled / DOM 复用到错的行。"""
    ui = UI.read_text(encoding="utf-8")
    assert "deleteSlRule(sl, r)" in ui
    assert ':key="r.key || `${sl.id}-${i}`"' in ui
    # 算不出键的（出站规则）不给删按钮 —— 这一路只改入站。
    assert 'v-if="r.key"' in ui


def test_the_add_form_exposes_everything_the_content_key_depends_on():
    """无状态与否是内容键的一部分 —— 表单里不给，就有一类规则永远删不掉。"""
    ui = UI.read_text(encoding="utf-8")
    for token in ("slForm.protocol", "slForm.cidr", "slForm.ports", "slForm.description", "slForm.stateless"):
        assert token in ui, token


def test_the_page_states_the_subnet_blast_radius():
    """用户是从**某一台实例**的详情页点进来的，天然会以为只影响这一台。"""
    ui = UI.read_text(encoding="utf-8")
    assert "作用于整个子网" in ui
    assert "同一子网里的其它实例也会一起受影响" in ui


def test_the_nsg_buttons_say_they_are_nsg_buttons():
    """两个控制面共存时，按钮就必须各归各的。以前顶部那三个全作用于 NSG，
    而用户以为它们管的是「这台机器的防火墙」。"""
    ui = UI.read_text(encoding="utf-8")
    assert "NSG 放行全部端口" in ui and "清空 NSG 规则" in ui


def test_the_security_list_writes_use_the_navigation_guard():
    """安全列表是子网级的：请求飞行途中切了实例，结果消息会落在新页面上
    而描述的是旧页面的子网。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function runSlWrite(")[1].split("async function addSlRule")[0]
    assert "act.moved()" in fn


def test_the_port_input_distinguishes_empty_from_malformed():
    """空 = 全部端口，格式错 = 别提交。两者混成一个 falsy 值的话，
    「8000-」这种手滑会被当成「全部端口」写进去。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("function parsePorts(")[1].split("const slPreview")[0]
    assert "return null" in fn and "return undefined" in fn


# ------------------------------------------------------------------ 复核修掉的几处


def test_the_blast_radius_note_does_not_understate_it_as_one_subnet():
    """安全列表属于 VCN，一份列表可以同时挂在**多个子网**上（Oracle 建 VCN 时自带的
    那份尤其如此）。说成「同一子网」是把实情说小了 —— 而说小的方向正是
    「点下去之后才发现别处也变了」。"""
    note = T._SL_SCOPE_NOTE
    assert "所有子网" in note
    assert "同一子网里" not in note


def test_cloudflare_notices_a_wide_open_rule_living_in_an_nsg():
    """「已经比 Cloudflare 更宽」不能只看这一份列表。

    另一份安全列表或这台机器的 NSG 上有一条全网放行 80/443，加再多 Cloudflare 网段
    也挡不住任何人直连源站 —— 漏掉这一半，警告就只在最不需要它的时候才出现。
    """
    nsg = {
        "id": "nsg-1",
        "display_name": "nsg-web",
        "rules": [
            {"direction": "INGRESS", "protocol": "6", "port": "443", "cidr": "0.0.0.0/0"}
        ],
    }
    s = _session(lists=[_sl()], groups=[nsg])
    r = s.add_cloudflare_security_list_rules(
        "i", "c", security_list_id="sl-1", ports=[443], include_ipv6=False
    )
    assert r.ok, r.message
    assert "⚠" in r.message and "nsg-web" in r.message
    assert "443" in (r.data or {}).get("wide_open", [])


def test_the_delete_dialog_does_not_promise_the_port_is_closed():
    """生效规则是并集 —— 只要同一张表里还躺着一条「全部协议 / 0.0.0.0/0」
    （面板自己的「放行全部端口」就会写一条），这次删除一个端口都关不上。
    在确认框里承诺关上，是这一类里最糟的错法。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function deleteSlRule(")[1].split("async function slAction")[0]
    # 注释里出现这句是**在解释为什么不能这么写**，那不算。只看真正会显示给用户的代码。
    code = chr(10).join(
        line for line in fn.splitlines() if not line.strip().startswith("//")
    )
    assert "删除后对应端口将无法从外部访问" not in code
    assert "不等于端口就关上了" in code


def test_the_clear_dialog_warns_that_ssh_goes_away():
    """对没有 NSG 的机器（这个功能的主要用户）安全列表就是唯一的入站来源 ——
    清空 = SSH 立刻断。这句必须在**点之前**说，结果消息里再说就晚了。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function slAction(")[1].split("async function clearFirewall")[0]
    assert "SSH 会立刻断开" in fn


def test_the_empty_firewall_state_offers_a_button_that_actually_renders():
    """空态那段文案指向的动作必须在**这个状态下**真的可点。

    上一版把「放行全部端口」挪进了 v-if="fwGroups.length" 的标题栏，
    而空态恰恰是 fwGroups 为空时才渲染 —— 文案指着一个不存在的按钮。
    """
    ui = UI.read_text(encoding="utf-8")
    block = ui.split("该实例没有关联的网络安全组")[1][:700]
    assert "openAllFirewall" in block, "空态里没有可点的动作"


def test_the_per_list_empty_row_states_only_a_fact_about_that_list():
    """「外部网络连不上任何端口」是机器级结论。一个子网可以挂 5 份安全列表，
    另一份（或一个 NSG）完全可能还开着 22。"""
    ui = UI.read_text(encoding="utf-8")
    assert "这份安全列表没有任何规则 —— 外部网络连不上任何端口" not in ui
    assert "不放行也不阻断任何东西" in ui


def test_a_stale_port_typo_does_not_block_an_icmp_rule():
    """端口输入框只在 TCP/UDP 下渲染。无条件校验它的话，用户在 TCP 上填错一半、
    切到 ICMP，输入框消失、预览也不再报错，可「添加」却被一个他看不见的值挡住。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function addSlRule(")[1].split("async function deleteSlRule")[0]
    assert "usesPorts ? parsePorts(slForm.ports) : null" in fn


def test_open_all_and_cloudflare_resolve_the_security_list_only_once():
    """_resolve_security_list 里那次 get_instance_firewall 要走好几个 OCI 调用
    （读 VNIC、读每个 NSG 的规则、读子网每份安全列表）。让包装方法再走一遍，
    等于一次点击花两份配额 —— 这个项目对 OCI 调用量是敏感的。"""
    import inspect

    for fn in (T.open_all_security_list, T.add_cloudflare_security_list_rules):
        src = inspect.getsource(fn)
        assert "_add_rules_to(" in src, fn.__name__
        assert "self.add_security_list_rules(" not in src, fn.__name__


def test_the_etag_lookup_does_not_depend_on_the_header_container_type():
    """真实的头名字是 ``ETag``。SDK 现在返回 CaseInsensitiveDict，所以
    ``.get("etag")`` 碰巧能拿到 —— 但这条并发保护不该建立在容器类型上：
    换成普通 dict 就静默变成「没有 etag」，写照常成功、只是不再有保护，
    而且没有任何地方会报错。"""
    for headers in ({"ETag": "e1"}, {"etag": "e1"}, {"Etag": "e1"}):
        s = _session(lists=[_sl()])
        s._network.get_security_list = lambda _id, _h=headers: SimpleNamespace(
            data=SimpleNamespace(
                id="sl-1", display_name="sl", ingress_security_rules=[],
                egress_security_rules=[],
            ),
            headers=_h,
        )
        s.add_security_list_rules("i", "c", security_list_id="sl-1", specs=[_spec()])
        assert s.if_match == ["e1"], headers
