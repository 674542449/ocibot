"""让面板的防火墙**真正生效**。

## 问题

用户的诉求：「我在面板选一个实例操作防火墙，比如只开放 22 和 80，就真的只开
22 和 80，不能出现我啥都没开居然全开」。

而在此之前它做不到 —— OCI 的生效规则是

    子网安全列表 ∪ VNIC 的所有 NSG          （securityrules.htm）

面板管的是 NSG（实例级），入站却被**子网级**的安全列表兜底放行了。两个来源：

* 面板自己建网时往安全列表里写了一条 `protocol=all, source=0.0.0.0/0`；
* 更普遍的是 **Oracle 建 VCN 时自带的默认安全列表**里那条 `TCP 22 from
  0.0.0.0/0` —— 面板优先复用现有子网，所以绝大多数租户走的是这一条。

并集之下：

* 详情页那一整套 NSG 操作（加规则 / 删规则 / Cloudflare 白名单 / 清空）全是摆设；
* 创建向导里「不允许外网直接访问」用的是 `_ssh_only_specs`（只放 22），
  **这个选项从来没生效过** —— 机器实际全端口对公网开放。

## 修法

* 新建网络写**基线**安全列表：入站只留 ICMP，出站全放行。入站不开端口之后
  NSG 就是唯一的入站控制点（"Without security rules, no traffic is allowed in
  and out of VNICs"）。
* 存量网络给一个显式的收紧动作：删掉安全列表里**所有**对公网开端口的入站规则，
  Oracle 自带的那份也算 —— 只肯动自己建的列表，对绝大多数人等于什么都没做。
* 动手前先 `preview`：把要删的每一条原样报给用户。他完全可能真的在 Oracle
  控制台开过 3306，面板无权替他判断那是不是笔误。
* 界面直接把「绕过 NSG 的是哪几条」列出来，而不是只说一句「有一条全开」。

## 为什么保留 ICMP 和出站

* **ICMP type 3 code 4**：Path MTU Discovery。Oracle 自己的默认安全列表就有它，
  文档原话 "enables Compute instances to receive Path MTU Discovery fragmentation
  messages"。删掉不会立刻断网，而是让大包静默丢失 —— ssh 能连上但 scp 卡死，
  这种故障极难排查。同理还有一条 VCN 内的 ICMP type 3，文档在列完之后直接写
  "Don't remove those rules"。
* **出站 all**：有状态规则的回程包本来就自动放行（"the response is tracked and
  automatically allowed back to the originating host, regardless of any egress
  rules"），所以出站留在安全列表里不削弱入站管控；而搬进 NSG 的话，子网里任何
  一台没有 NSG 的机器会连出网都没有。
"""

from __future__ import annotations

import inspect
import pathlib
from types import SimpleNamespace

import pytest

from app.oci_client import LEGACY_DEFAULT_SL_NAMES, OperationResult, TenantSession

oci = pytest.importorskip("oci")

UI = pathlib.Path("web/frontend/src/views/InstanceDetailView.vue")


# ------------------------------------------------------------------ 基线规则


def test_the_baseline_list_opens_no_inbound_ports():
    """入站不放行任何 TCP/UDP 端口 —— 那正是让 NSG 说了算的前提。"""
    ingress, _ = TenantSession._baseline_security_list_rules()
    assert ingress, "全空会连 Path MTU Discovery 都没有"
    for rule in ingress:
        # 只允许 ICMP(1) / ICMPv6(58)；出现 all / 6 / 17 就等于又开了端口。
        assert str(rule.protocol) in ("1", "58"), rule.protocol


def test_the_baseline_keeps_path_mtu_discovery():
    """删掉它不会立刻断网，而是让大包静默丢失（ssh 能连、scp 卡死）。

    Oracle 自己的默认安全列表就带这一条，没理由比它更激进。
    """
    ingress, _ = TenantSession._baseline_security_list_rules()
    pmtu = [
        r
        for r in ingress
        if str(r.protocol) == "1"
        and getattr(r.icmp_options, "type", None) == 3
        and getattr(r.icmp_options, "code", None) == 4
    ]
    assert pmtu, "基线里没有 ICMP type 3 code 4"
    assert pmtu[0].source == "0.0.0.0/0"


def test_the_baseline_keeps_the_in_vcn_icmp_when_the_cidr_is_known():
    """Oracle 默认列表三条规则的第三条，文档紧接着写 "Don't remove those rules"。

    少了它，同 VCN 内本该秒失败的连接要等到超时才报错。
    """
    ingress, _ = TenantSession._baseline_security_list_rules(vcn_cidrs=["10.0.0.0/16"])
    in_vcn = [r for r in ingress if str(r.protocol) == "1" and r.source == "10.0.0.0/16"]
    assert in_vcn, "基线里没有 VCN 内的 ICMP"
    # code 必须留空 = 全部 code，只放 code 4 会漏掉「端口不可达」。
    assert getattr(in_vcn[0].icmp_options, "type", None) == 3
    assert getattr(in_vcn[0].icmp_options, "code", None) is None


def test_the_baseline_survives_a_vcn_cidr_it_cannot_read():
    """拿不到 VCN CIDR 只是少一条锦上添花的规则，不该让整条建网路径失败。"""
    ingress, _ = TenantSession._baseline_security_list_rules(vcn_cidrs=[])
    assert any(str(r.protocol) == "1" for r in ingress)


def test_the_baseline_keeps_egress_open():
    """出站关掉会让子网里没有 NSG 的机器连出网都没有 —— 代价远大于收益。"""
    _, egress = TenantSession._baseline_security_list_rules()
    assert any(str(r.protocol) == "all" and r.destination == "0.0.0.0/0" for r in egress)


def test_ipv6_baseline_adds_packet_too_big_not_a_blanket_allow():
    ingress, egress = TenantSession._baseline_security_list_rules(include_ipv6=True)
    v6_in = [r for r in ingress if str(r.protocol) == "58"]
    assert v6_in and v6_in[0].source == "::/0"
    # v6 入站同样不能出现 all。
    assert not any(str(r.protocol) == "all" for r in ingress)
    assert any(str(r.protocol) == "all" and r.destination == "::/0" for r in egress)


def test_new_networks_no_longer_get_a_wide_open_list():
    """建网路径必须用基线规则。这一条是整个诉求的根 —— 回退它，
    「只开 22 和 80」就又变成谎话。"""
    src = inspect.getsource(TenantSession._ensure_open_security_list)
    assert "_baseline_security_list_rules" in src
    assert "_open_security_list_rules" not in src


# ------------------------------------------------------------------ 识别「绕过 NSG」


def test_oracle_s_default_ssh_rule_counts_as_bypassing_the_nsg():
    """Oracle 自带的 TCP 22 from 0.0.0.0/0 不是「全开」，却同样让 NSG 说了不算。

    只认 protocol=all 的话，用 Oracle 向导建网的租户（绝大多数）根本看不到告警。
    """
    rule = {"direction": "INGRESS", "protocol": "6", "port": "22", "cidr": "0.0.0.0/0"}
    assert TenantSession._rule_opens_public_ports(rule)
    # 而旧的「全开」判定看不见它 —— 这正是当初漏掉这一整类的原因。
    assert not TenantSession._ingress_wide_open(rule)


def test_icmp_and_private_sources_do_not_count():
    """ICMP 开不了端口；私网源也不是「对公网开着」。误报会让用户去删不该删的。"""
    icmp = {"direction": "INGRESS", "protocol": "1", "port": "类型 3 代码 4", "cidr": "0.0.0.0/0"}
    private = {"direction": "INGRESS", "protocol": "6", "port": "22", "cidr": "10.0.0.0/16"}
    egress = {"direction": "EGRESS", "protocol": "all", "port": "全部", "cidr": "0.0.0.0/0"}
    assert not TenantSession._rule_opens_public_ports(icmp)
    assert not TenantSession._rule_opens_public_ports(private)
    assert not TenantSession._rule_opens_public_ports(egress)


def test_a_rule_is_described_in_words_the_user_can_check():
    """确认框里摆的必须是「TCP 22 ← 0.0.0.0/0」这种能核对的东西，
    不是「1 条入站规则」。用户要认出自己手工开过的那条。"""
    rule = SimpleNamespace(
        protocol="6",
        source="0.0.0.0/0",
        description="",
        tcp_options=SimpleNamespace(destination_port_range=SimpleNamespace(min=22, max=22)),
        udp_options=None,
    )
    text = TenantSession._describe_sdk_ingress(rule)
    assert "TCP" in text and "22" in text and "0.0.0.0/0" in text


# ------------------------------------------------------------------ 收紧动作


def _sl(name, ingress, *, managed=True, sl_id="sl-1"):
    return SimpleNamespace(
        id=sl_id,
        display_name=name,
        ingress_security_rules=ingress,
        egress_security_rules=[],
        freeform_tags={"managed_by": "oci-console-helper"} if managed else {},
    )


def _ing(protocol="all", source="0.0.0.0/0", description="ocibot open all IPv4 ingress"):
    return SimpleNamespace(
        protocol=protocol,
        source=source,
        description=description,
        icmp_options=None,
        tcp_options=None,
        udp_options=None,
    )


def _tcp(port, source="0.0.0.0/0", description=""):
    return SimpleNamespace(
        protocol="6",
        source=source,
        description=description,
        icmp_options=None,
        udp_options=None,
        tcp_options=SimpleNamespace(
            destination_port_range=SimpleNamespace(min=port, max=port)
        ),
    )


def _session(
    security_lists,
    *,
    at_risk=None,
    complete=True,
    has_ipv6=False,
    other_subnets=(),
    subnets_readable=True,
):
    """一个只接了必要几根线的 TenantSession。

    ``at_risk`` 可以是列表（只有本子网有风险）或 {subnet_id: [...]} 。
    ``other_subnets`` 是同 VCN 里也挂着这些安全列表的子网。
    """
    s = TenantSession.__new__(TenantSession)
    s.updated = []
    s.tenant = SimpleNamespace(tenancy_ocid="ocid1.tenancy..root")

    risk_map = at_risk if isinstance(at_risk, dict) else {"subnet-1": list(at_risk or [])}

    s.get_instance_firewall = lambda *a, **k: OperationResult(  # type: ignore[method-assign]
        ok=True, message="", data={"subnet_id": "subnet-1", "has_ipv6": has_ipv6}
    )
    s._subnet_vnics_at_risk = lambda sid: (list(risk_map.get(sid, [])), complete)  # type: ignore[method-assign]

    all_ids = [sl.id for sl in security_lists]

    def _list_subnets(compartment_id=None, vcn_id=None):
        if not subnets_readable:
            raise RuntimeError("throttled")
        rows = [
            {"id": "subnet-1", "display_name": "public-subnet", "security_list_ids": all_ids}
        ]
        rows.extend(other_subnets)
        return rows

    s.list_subnets = _list_subnets  # type: ignore[method-assign]

    def _update(sl_id, details):
        s.updated.append((sl_id, list(details.ingress_security_rules or [])))
        return SimpleNamespace(data=None)

    s._network = SimpleNamespace(
        get_subnet=lambda _id: SimpleNamespace(
            data=SimpleNamespace(
                security_list_ids=all_ids, vcn_id="vcn-1", compartment_id="comp-1"
            )
        ),
        get_security_list=lambda sid: SimpleNamespace(
            data=next(sl for sl in security_lists if sl.id == sid)
        ),
        get_vcn=lambda _id: SimpleNamespace(
            data=SimpleNamespace(cidr_blocks=["10.0.0.0/16"], cidr_block="10.0.0.0/16")
        ),
        update_security_list=_update,
    )
    return s


def test_it_drops_the_wide_open_rule_and_leaves_a_baseline():
    s = _session([_sl("open-security-list", [_ing()])])
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok, r.message
    assert s.updated, "没有真的写回去"
    _sid, new_ingress = s.updated[0]
    assert not any(str(x.protocol) == "all" for x in new_ingress), "全开规则还在"
    assert any(str(x.protocol) == "1" for x in new_ingress), "基线 ICMP 没补上"


def test_it_drops_oracle_s_default_ssh_rule_once_allowed():
    """这条才是绝大多数租户的实际情况 —— 漏掉它，按钮对他们等于没做事。"""
    s = _session([_sl("Default Security List for oci-worker-vcn", [_tcp(22)], managed=False)])
    r = s.tighten_subnet_security_list("i", "c", include_foreign=True)
    assert r.ok, r.message
    _sid, new_ingress = s.updated[0]
    assert not any(str(x.protocol) == "6" for x in new_ingress), "TCP 22 还在"


def test_it_will_not_touch_someone_else_s_list_without_a_second_yes():
    """别人的东西不碰，但要把「不碰的后果」摆清楚，而不是打发用户去 Oracle 控制台。"""
    s = _session([_sl("Default Security List for oci-worker-vcn", [_tcp(22)], managed=False)])
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok is False
    assert not s.updated
    assert (r.data or {}).get("needs_foreign_consent") is True
    # 要删的那条得原样出现，用户才认得出。
    assert "TCP 22" in r.message and "0.0.0.0/0" in r.message


def test_a_foreign_list_gets_rules_removed_but_none_added_back():
    """已经在动别人的东西了，再往里塞面板的基线规则超出了用户同意的范围。"""
    s = _session([_sl("别人的列表", [_tcp(22), _tcp(3306)], managed=False)])
    r = s.tighten_subnet_security_list("i", "c", include_foreign=True)
    assert r.ok
    _sid, new_ingress = s.updated[0]
    assert new_ingress == [], "往别人的列表里补了规则"


def test_user_added_public_ports_are_removed_too_but_reported_one_by_one():
    """留着用户手工加的那条全开，按钮就还是在撒谎（「只开 22 和 80」不成立）。

    所以照删 —— 但每一条都要在 preview 里报出来，让他先看见再决定。
    """
    mine = _ing()
    theirs_private = _ing(protocol="6", source="1.2.3.0/24", description="我自己加的")
    theirs_public = _tcp(3306, description="我自己开的 MySQL")
    s = _session([_sl("open-security-list", [mine, theirs_private, theirs_public])])

    pre = s.tighten_subnet_security_list("i", "c", preview=True)
    assert pre.ok and not s.updated, "preview 不该写任何东西"
    assert "3306" in pre.message, "要删的规则没摆给用户看"

    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok
    _sid, new_ingress = s.updated[0]
    kept = {x.description for x in new_ingress}
    # 私网源的规则不开公网端口，一条不碰。
    assert "我自己加的" in kept
    assert "我自己开的 MySQL" not in kept
    assert "ocibot open all IPv4 ingress" not in kept


def test_preview_writes_nothing_even_when_everything_checks_out():
    s = _session([_sl("open-security-list", [_ing()])])
    r = s.tighten_subnet_security_list("i", "c", preview=True)
    assert r.ok and (r.data or {}).get("preview") is True
    assert not s.updated


def test_a_legacy_named_list_is_still_recognised_as_ours():
    """老装机留下的列表没有 freeform_tags，只能靠名字认。"""
    for name in LEGACY_DEFAULT_SL_NAMES:
        s = _session([_sl(name, [_ing()], managed=False)])
        assert s.tighten_subnet_security_list("i", "c").ok, name


def test_it_stops_when_some_instance_would_lose_inbound():
    """安全列表是**子网级**的 —— 收紧会波及同子网所有实例。

    预检发现有实例进不来时先停手、只报名单，别替用户做这个决定。
    """
    s = _session([_sl("open-security-list", [_ing()])], at_risk=["web-2（没有任何 NSG）"])
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok is False
    assert not s.updated, "预检没过还是动手了"
    assert "web-2" in r.message
    assert "先给它们各自的 NSG 加上" in r.message


def test_it_checks_every_subnet_that_shares_the_same_list():
    """一份安全列表可以挂在同 VCN 的多个子网上。

    只预检实例自己那个子网，就会把**另一个子网**里没有 NSG 的机器悄悄关在门外 ——
    而那台机器根本不在用户这次操作的视野里。
    """
    s = _session(
        [_sl("open-security-list", [_ing()])],
        other_subnets=[
            {"id": "subnet-2", "display_name": "db-subnet", "security_list_ids": ["sl-1"]}
        ],
        at_risk={"subnet-2": ["db-1（没有任何 NSG）"]},
    )
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok is False
    assert not s.updated
    assert "db-subnet" in r.message and "db-1" in r.message


def test_a_subnet_on_a_different_list_is_not_dragged_in():
    """不共用这份列表的子网不受影响，别拿它的实例吓唬用户。"""
    s = _session(
        [_sl("open-security-list", [_ing()])],
        other_subnets=[
            {"id": "subnet-9", "display_name": "别的子网", "security_list_ids": ["sl-other"]}
        ],
        at_risk={"subnet-9": ["无关机器"]},
    )
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok, r.message
    assert "无关机器" not in r.message


def test_force_proceeds_but_says_who_got_cut_off():
    s = _session([_sl("open-security-list", [_ing()])], at_risk=["web-2（没有任何 NSG）"])
    r = s.tighten_subnet_security_list("i", "c", force=True)
    assert r.ok and s.updated
    assert "⚠" in r.message and "web-2" in r.message


def test_an_incomplete_preflight_stops_instead_of_guessing():
    """读不全子网里的实例 = 判断不了谁会失联。

    这是会波及整个子网的写操作，读失败就该停手，而不是当成「没有风险」。
    """
    s = _session([_sl("open-security-list", [_ing()])], complete=False)
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok is False
    assert not s.updated
    assert "没读全" in r.message and "什么都没改" in r.message


def test_a_failed_subnet_scan_also_stops():
    """列不出「还有哪些子网共用这份列表」，就等于不知道会波及谁。"""
    s = _session([_sl("open-security-list", [_ing()])], subnets_readable=False)
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok is False
    assert not s.updated
    assert "没读全" in r.message


def test_running_it_twice_is_a_no_op():
    already = SimpleNamespace(
        protocol="1",
        source="0.0.0.0/0",
        description="ocibot Path MTU Discovery",
        icmp_options=SimpleNamespace(type=3, code=4),
        tcp_options=None,
        udp_options=None,
    )
    s = _session([_sl("open-security-list", [already])])
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok
    assert not s.updated, "已经收紧过了还去写一次"
    assert "无需改动" in r.message


# ------------------------------------------------------------------ 可见性


def test_the_firewall_page_says_when_the_nsg_is_pointless():
    """安全列表放行了公网端口时，先告诉用户下面那套 NSG 规则不起作用 ——
    否则他会在一个不生效的面板上认真配规则。"""
    ui = UI.read_text(encoding="utf-8")
    assert "fwBypass" in ui
    assert "NSG 规则当前不起作用" in ui
    assert "bypass_lists" in ui, "没从后端读那个标志"
    # 判定留在后端,别在 TS 里重写一份必然漂移的。
    assert "0.0.0.0/0" not in ui.split("fwBypass")[1][:900]


def test_the_banner_lists_the_actual_rules_not_just_a_count():
    """只说「有一条全开」的话，Oracle 默认列表里那条 TCP 22 就永远不会被人看见。"""
    ui = UI.read_text(encoding="utf-8")
    assert "sl.rules.join" in ui


def test_the_tighten_button_cannot_be_talked_into_skipping_confirmation():
    """`@click="tightenSubnet"` 会把 PointerEvent 当参数传进去（truthy）。

    以前那个参数是 force —— 点一下就同时跳过确认框和预检。现在函数干脆不收参数，
    从源头上没得可传。
    """
    ui = UI.read_text(encoding="utf-8")
    assert "async function tightenSubnet() {" in ui, "别给它加回参数"
    assert '@click="tightenSubnet()"' in ui


def test_the_ui_branches_on_flags_not_on_chinese_prose():
    """靠 message 里有没有某几个汉字来决定「要不要再确认一次」，
    文案改一个字就失灵 —— 而失灵的方向是**跳过确认直接写**。"""
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function tightenSubnet()")[1].split("async function clearFirewall")[0]
    assert "needs_foreign_consent" in fn
    assert "at_risk" in fn
    assert "会失去入站" not in fn, "又回去匹配中文了"


def test_the_ui_previews_before_it_writes():
    ui = UI.read_text(encoding="utf-8")
    fn = ui.split("async function tightenSubnet()")[1].split("async function clearFirewall")[0]
    assert "preview: true" in fn
    # 没点确认就不能落到那次真写。
    assert "if (!approved) return" in fn


def test_a_root_compartment_403_does_not_block_the_button():
    """非管理员在租户根 compartment 上列资源本来就经常被拒。

    把那次失败算成「没读全」，等于让这个按钮对最需要它的那批人永远点不动 ——
    而兄弟子网跟 VCN 同 compartment 是压倒性的常态。
    """
    s = _session([_sl("open-security-list", [_ing()])])
    real = s.list_subnets

    def _only_own(compartment_id=None, vcn_id=None):
        if compartment_id != "comp-1":
            raise RuntimeError("NotAuthorizedOrNotFound")
        return real(compartment_id=compartment_id, vcn_id=vcn_id)

    s.list_subnets = _only_own
    r = s.tighten_subnet_security_list("i", "c")
    assert r.ok, r.message
    assert s.updated
