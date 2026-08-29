"""清空实例所有 NSG 规则。

## 这个动作到底有多危险 —— 取决于子网安全列表

OCI 文档原话（securityrules.htm）：

    "If you use both security lists and network security groups, the set of
     rules that applies to a particular VNIC is the union of these items:
     The security rules in the security lists associated with the VNIC's subnet
     / The security rules in all NSGs that the VNIC is in"

（第一版引的是 networksecuritygroups.htm 里「any rule in any of the VNIC's NSGs」
那句 —— 那讲的是**多个 NSG 之间**如何合并，管不着 NSG 和安全列表的关系。
结论没错，但引文不支持它。）

安全列表和 NSG 是**并集**，不是交集。所以清空 NSG **不必然**把人锁在外面：
只要子网安全列表里还有一条放行 22，SSH 照样进得去。

面板本来就读得到那些安全列表（`get_instance_firewall` 返回 `security_lists`），
所以这里**具体算一遍**清完之后 22 端口还通不通，而不是甩一句泛泛的「可能连不上」。
说得准，用户才会认真看那条警告 —— 逢操作必恐吓的界面，警告会被当成噪音。

## 不做的事

**不偷偷保留一条 SSH。** 用户点的是「清空所有规则」，留一条会让「清空」这个词
说谎，而且下次他看规则列表时会莫名其妙多出一条自己没加的。真锁死了也是可恢复的
（见下面那条测试）—— 「放行全部端口」和「添加规则」都走 Oracle API，不需要先连上机器。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.oci_client import OperationResult, TenantSession

pytest.importorskip("oci")


def _nsg(nsg_id, rules):
    return {"id": nsg_id, "display_name": nsg_id, "is_managed": True, "rules": rules}


def _r(rid, direction="INGRESS", protocol="6", port="22", cidr="0.0.0.0/0"):
    return {
        "id": rid,
        "direction": direction,
        "protocol": protocol,
        "port": port,
        "cidr": cidr,
    }


def _sl(name, rules):
    return {"id": f"sl-{name}", "display_name": name, "rules": rules}


def _session(groups, security_lists, *, delete_ok=True, sl_complete=True,
             has_ipv6=False, partial_count=0):
    s = TenantSession.__new__(TenantSession)
    s.deleted = []

    s.get_instance_firewall = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="",
        data={
            "groups": groups,
            "security_lists": security_lists,
            "security_lists_complete": sl_complete,
            "has_ipv6": has_ipv6,
        },
    )

    def _delete(nsg_id, ids):
        s.deleted.append((nsg_id, list(ids)))
        if not delete_ok:
            # 分批删:失败时前面几批可能已经删掉了。
            return OperationResult(ok=False, message="删除失败", data={"count": partial_count})
        return OperationResult(ok=True, message="已删除", data={"count": len(ids)})

    s.delete_nsg_rules = _delete  # type: ignore[method-assign]
    return s


# ------------------------------------------------------------- 放行范围判定


@pytest.mark.parametrize(
    "port_text,protocol,expected",
    [
        ("22", "6", "public"),
        ("全部", "6", "public"),
        ("", "6", "public"),
        ("20-25", "6", "public"),
        ("全部", "all", "public"),
        ("80", "6", ""),
        ("23-99", "6", ""),
        ("80-443", "6", ""),          # 区间不含 22
        ("类型 3 代码 4", "1", ""),    # ICMP 没有端口
        ("22", "17", ""),              # UDP 22 不等于 SSH 通了
    ],
)
def test_port_matching_handles_every_shape_the_normalizer_emits(port_text, protocol, expected):
    """解析的是 _normalize_firewall_rule 自己产出的四种文案形态。"""
    rule = _r("x", protocol=protocol, port=port_text)
    assert TenantSession._ingress_tcp_scope(rule, 22) == expected


def test_a_private_cidr_is_not_public_reachability():
    """**这是这个功能唯一朝「虚假安心」失效的地方。**

    第一版的判定从头到尾没读 cidr：一条「TCP 22 源 10.0.0.0/16」会被算成幸存者，
    面板打出「SSH 不受影响」，而公网 SSH 其实已经断了。
    误报警告只是烦人，误报安全会让人真的被关在机器外面。
    """
    assert TenantSession._ingress_tcp_scope(_r("x", cidr="10.0.0.0/16"), 22) == "10.0.0.0/16"
    assert TenantSession._ingress_tcp_scope(_r("x", cidr="0.0.0.0/0"), 22) == "public"


def test_families_do_not_vouch_for_each_other():
    """IPv4 的存活规则不能替 IPv6 背书，反之亦然。"""
    v4 = _r("x", cidr="0.0.0.0/0")
    v6 = _r("x", cidr="::/0")
    assert TenantSession._ingress_tcp_scope(v4, 22, family="v4") == "public"
    assert TenantSession._ingress_tcp_scope(v4, 22, family="v6") == ""
    assert TenantSession._ingress_tcp_scope(v6, 22, family="v6") == "public"
    assert TenantSession._ingress_tcp_scope(v6, 22, family="v4") == ""


def test_non_cidr_sources_are_never_treated_as_public():
    """NSG 规则的源可以是 service OCID 或另一个 NSG 的 OCID。

    它可能确实放行了某些流量，但绝不能被当成「公网能进」。
    """
    for source in ("ocid1.service.oc1.iad.aaaa", "ocid1.networksecuritygroup.oc1..bbb", ""):
        assert TenantSession._ingress_tcp_scope(_r("x", cidr=source), 22) == ""


def test_egress_rules_never_count_as_inbound_ssh():
    """出站放行 22 和「能不能 SSH 进来」毫无关系。"""
    assert TenantSession._ingress_tcp_scope(_r("x", direction="EGRESS"), 22) == ""


# ------------------------------------------------------------- 清空行为


def test_it_deletes_every_rule_in_every_nsg():
    groups = [_nsg("nsg1", [_r("a"), _r("b")]), _nsg("nsg2", [_r("c")])]
    s = _session(groups, [])
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok, r.message
    assert s.deleted == [("nsg1", ["a", "b"]), ("nsg2", ["c"])]
    assert r.data["removed"] == 3
    assert r.data["groups"] == 2


def test_it_writes_nothing_back():
    """和「放行全部端口」的唯一区别就是不写回 —— 留一条都算功能说谎。"""
    import inspect

    src = inspect.getsource(TenantSession.clear_instance_firewall_rules)
    assert "add_nsg_rules" not in src
    assert "_open_all_specs" not in src
    assert "_ssh_only_specs" not in src


def test_a_surviving_security_list_rule_means_ssh_still_works():
    """并集语义：子网安全列表还放行 22 时，清空 NSG 不会把人锁在外面。

    这时候**不该**报警告 —— 逢操作必恐吓，真正危险的那次就没人看了。
    """
    s = _session([_nsg("nsg1", [_r("a")])], [_sl("默认安全列表", [_r("s1", port="22")])])
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok
    assert r.data["ssh_after"] is True
    assert "⚠" not in r.message
    assert "默认安全列表" in r.message
    assert "SSH 不受影响" in r.message


def test_no_surviving_ssh_rule_is_called_out_loudly():
    """没有任何东西放行 22 了 —— 这一次必须警告，而且要给恢复路径。"""
    s = _session([_nsg("nsg1", [_r("a")])], [_sl("空列表", [_r("s1", port="80")])])
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok
    assert r.data["ssh_after"] is False
    assert "⚠" in r.message
    assert "WebSSH" in r.message
    # 只说危险不说出路，等于把人吓住又不帮忙。
    assert "放行全部端口" in r.message
    assert "不需要先连上机器" in r.message


def test_the_union_semantics_are_stated_not_assumed():
    """文案要说清楚子网安全列表没被碰 —— 否则用户会以为整台机器都断了。"""
    s = _session([_nsg("nsg1", [_r("a")])], [_sl("默认", [_r("s1")])])
    msg = s.clear_instance_firewall_rules("i", "c").message
    assert "子网安全列表未受影响" in msg
    assert "并集" in msg


def test_an_instance_with_no_nsg_says_so_instead_of_pretending_to_work():
    s = _session([], [_sl("默认", [_r("s1")])])
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok
    assert r.data["removed"] == 0
    assert "没有关联网络安全组" in r.message
    assert not s.deleted


def test_an_empty_nsg_is_not_a_delete_call():
    """零条规则的安全组不该发一次空的删除请求。"""
    s = _session([_nsg("nsg1", [])], [])
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok and not s.deleted and r.data["removed"] == 0


def test_a_partial_failure_is_reported_as_failure():
    """删了一半失败，不能报「已清空」—— 那会让用户以为规则没了、其实还在。"""
    s = _session([_nsg("nsg1", [_r("a")])], [], delete_ok=False)
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok is False
    assert "清空部分失败" in r.message


def test_the_route_writes_an_audit_entry_including_the_ssh_verdict():
    """事后要能回答「谁什么时候清的、清完还通不通」。"""
    import pathlib

    src = pathlib.Path("web/backend/routers/instance_ops.py").read_text(encoding="utf-8")
    block = src.split('action="firewall.clear"', 1)[1][:600]
    assert '"removed"' in block
    assert '"ssh_after"' in block


def test_an_unreadable_security_list_is_not_an_assertion_that_nothing_allows_22():
    """读失败 ≠「没有任何东西放行 22」。

    _subnet_security_lists 以前把读失败静默吞成空列表，而空列表是一个**事实
    陈述**。于是一次限流或权限不足会被讲成「清空后 SSH 将连不上」，把一台其实
    连得上的机器报成即将失联。同 list_console_connections 的既定说法：
    空列表是断言，读失败不是。
    """
    s = _session([_nsg("nsg1", [_r("a")])], [], sl_complete=False)
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok
    assert r.data["security_lists_complete"] is False
    assert "说不准" in r.message
    assert "没有任何" not in r.message


def test_ipv6_gets_its_own_verdict():
    """实例有 IPv6 时要分族算 —— IPv4 的存活规则不能替 IPv6 背书。

    本面板的典型形态正是「::/0 的放行写在 NSG 里」，一清就没了。
    """
    s = _session(
        [_nsg("nsg1", [_r("a")])],
        [_sl("默认", [_r("s1", cidr="0.0.0.0/0", port="22")])],
        has_ipv6=True,
    )
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok
    assert len(r.data["verdicts"]) == 2
    assert "IPv4" in r.message and "IPv6" in r.message
    # v4 通、v6 不通 → 整体仍要报警告。
    assert r.data["ssh_after"] is False
    assert "⚠" in r.message


def test_a_partial_failure_reports_what_was_already_deleted_and_the_ssh_verdict():
    """删到一半失败是**最需要**知道 SSH 状态的时刻 —— 规则已经少了一部分，
    机器可能比操作前更连不上，而用户看到的只是一句「失败」，最容易以为
    「那就是什么都没发生」。"""
    s = _session(
        [_nsg("nsg1", [_r(str(i)) for i in range(40)])],
        [],
        delete_ok=False,
        partial_count=25,
    )
    r = s.clear_instance_firewall_rules("i", "c")
    assert r.ok is False
    assert r.data["removed"] == 25, "已删掉的批次没被算进去"
    assert "无法回滚" in r.message
    assert "⚠" in r.message, "失败分支没给 SSH 结论"


def test_the_message_admits_the_scope_is_only_the_primary_vnic():
    """resolve_primary_network 命中 is_primary 就 break —— 只清主网卡的安全组。"""
    s = _session([_nsg("nsg1", [_r("a")])], [_sl("默认", [_r("s1")])])
    assert "主网卡" in s.clear_instance_firewall_rules("i", "c").message
