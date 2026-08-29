"""清空实例所有 NSG 规则。

## 这个动作到底有多危险 —— 取决于子网安全列表

OCI 文档原话（networksecuritygroups.htm）：

    "A packet in question is allowed if **any rule in any of the VNIC's NSGs**
     allows the traffic (or if the traffic is part of an existing connection
     being tracked)"

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


def _session(groups, security_lists, *, delete_ok=True):
    s = TenantSession.__new__(TenantSession)
    s.deleted: list = []

    s.get_instance_firewall = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="",
        data={"groups": groups, "security_lists": security_lists, "has_ipv6": False},
    )

    def _delete(nsg_id, ids):
        s.deleted.append((nsg_id, list(ids)))
        if not delete_ok:
            return OperationResult(ok=False, message="删除失败", data={})
        return OperationResult(ok=True, message="已删除", data={"count": len(ids)})

    s.delete_nsg_rules = _delete  # type: ignore[method-assign]
    return s


# ------------------------------------------------------------- 端口判定


@pytest.mark.parametrize(
    "port_text,protocol,expected",
    [
        ("22", "6", True),
        ("全部", "6", True),
        ("", "6", True),
        ("20-25", "6", True),
        ("80-443", "6", True),      # 22 不在 80-443 里 → 见下面 False 那条
        ("80", "6", False),
        ("23-99", "6", False),
        ("类型 3 代码 4", "1", False),   # ICMP 没有端口
        ("22", "17", False),             # UDP 22 不等于 SSH 通了
        ("全部", "all", True),
    ],
)
def test_port_matching_handles_every_shape_the_normalizer_emits(port_text, protocol, expected):
    """解析的是 _normalize_firewall_rule 自己产出的文案，只有四种形态。

    80-443 那条特意留着：它是**区间**，而 22 不在里面 —— 断言写在下面。
    """
    rule = _r("x", protocol=protocol, port=port_text)
    got = TenantSession._rule_allows_ingress_tcp(rule, 22)
    if port_text == "80-443":
        assert got is False
    else:
        assert got is expected


def test_egress_rules_never_count_as_inbound_ssh():
    """出站放行 22 和「能不能 SSH 进来」毫无关系。"""
    assert TenantSession._rule_allows_ingress_tcp(_r("x", direction="EGRESS"), 22) is False


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
    assert r.data["ssh_survivors"] == ["默认安全列表"]
    assert "⚠" not in r.message
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
    assert "没有关联的网络安全组" in r.message
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
