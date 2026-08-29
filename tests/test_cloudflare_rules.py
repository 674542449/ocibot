"""一键放行 Cloudflare CDN 回源网段。

这个功能有三处值得钉住，都不是「能不能加成规则」那么简单：

1. **它保护不了任何东西，除非你先删掉宽规则。** OCI 的安全规则是**白名单**：
   加放行不会关掉任何已有放行。如果 NSG 里还留着 `0.0.0.0/0` 放行 80/443，
   加完 22 个 Cloudflare 网段，源站**仍然全网可达** —— 而用户会以为自己锁好了。
   这是整个功能里最容易让人误判的一点，所以必须警告，而且警告要盖过「成功」。

2. **NSG 每组只有 120 条规则，Oracle 硬限制、不可调整。** 22 个网段 × 2 个端口
   = 44 条，占掉三分之一多。撞上限时 Oracle 回的是一条看不懂的 400，
   所以要在写之前自己算。

3. **外部来源的数据要写进防火墙。** 每条 CIDR 都得验过；拉不到要有兜底；
   用了兜底必须说出来 —— 把一份可能过期的内置表当成官方最新值，
   正是这类功能最容易犯的错。
"""

from __future__ import annotations

from types import SimpleNamespace
import pytest

from app import cloudflare_ips as cf
from app.oci_client import TenantSession

oci = pytest.importorskip("oci")


# ------------------------------------------------------------------ 取网段


def test_bad_cidrs_never_become_firewall_rules():
    """这是唯一一处把外部数据直接写成放行规则的地方，坏值必须丢掉。"""
    cleaned = cf._clean(
        ["1.2.3.0/24", "not-a-cidr", "", None, "999.1.1.1/8", "2606:4700::/32"], 4
    )
    assert cleaned == ["1.2.3.0/24"]


def test_version_mismatch_is_dropped():
    """v6 网段混进 v4 列表时不能被当成 v4 放行。"""
    assert cf._clean(["2606:4700::/32", "1.2.3.0/24"], 4) == ["1.2.3.0/24"]
    assert cf._clean(["1.2.3.0/24", "2606:4700::/32"], 6) == ["2606:4700::/32"]


def test_the_list_is_capped():
    """被污染的响应返回一万条网段，不该变成一万条放行规则。"""
    many = [f"10.{i // 256}.{i % 256}.0/24" for i in range(500)]
    assert len(cf._clean(many, 4)) == cf._MAX_CIDRS


def test_duplicates_are_collapsed():
    assert cf._clean(["1.2.3.0/24", "1.2.3.0/24"], 4) == ["1.2.3.0/24"]


def test_the_fallback_is_usable_on_its_own():
    fb = cf.fallback_ips()
    assert len(fb["ipv4"]) >= 10 and len(fb["ipv6"]) >= 5
    assert fb["source"] == "fallback"
    # 用了兜底必须说出来 —— 它会过期。
    assert "过期" in fb["note"]
    for cidr in fb["ipv4"] + fb["ipv6"]:
        import ipaddress

        ipaddress.ip_network(cidr, strict=False)  # 不抛就算过


def test_a_broken_response_falls_back_instead_of_writing_nothing(monkeypatch):
    """Cloudflare 抖一下不该让这个按钮变成死的。"""

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            raise RuntimeError("network down")

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: _Boom())
    out = cf.fetch_cloudflare_ips()
    assert out["source"] == "fallback"
    assert out["ipv4"]


def test_covers_is_what_lets_us_skip_redundant_rules():
    assert cf.covers("0.0.0.0/0", "104.16.0.0/13") is True
    assert cf.covers("::/0", "2606:4700::/32") is True
    assert cf.covers("104.16.0.0/13", "104.16.0.0/13") is True
    assert cf.covers("1.2.3.0/24", "104.16.0.0/13") is False
    # 跨版本不能算覆盖，否则一条 0.0.0.0/0 会把 v6 网段也「跳过」。
    assert cf.covers("0.0.0.0/0", "2606:4700::/32") is False


# ------------------------------------------------------------------ 写规则


def _rule(source, lo=None, hi=None, proto="6", direction="INGRESS"):
    opts = None
    if lo is not None:
        opts = SimpleNamespace(destination_port_range=SimpleNamespace(min=lo, max=hi))
    return SimpleNamespace(
        id=f"r-{source}-{lo}", direction=direction, source=source, protocol=proto, tcp_options=opts
    )


def _session(existing, monkeypatch, *, feed=None):
    s = TenantSession.__new__(TenantSession)

    # 用**普通函数**而不是 MagicMock：oci.pagination 的重试层会去读
    # `func_ref.__name__`（util.should_record_body_position_for_retry），
    # MagicMock 没有这个属性，抛的 AttributeError 会被方法里的宽 except 吞掉，
    # 变成一条 message='__name__' 的假失败。真实 SDK 方法当然是有 __name__ 的。
    def _list(nsg_id, **kwargs):
        return oci.Response(status=200, headers={}, data=list(existing), request=None)

    s._network = SimpleNamespace(list_network_security_group_security_rules=_list)
    s.added = []

    def _add(nsg_id, specs):
        s.added.extend(specs)
        from app.oci_client import OperationResult

        return OperationResult(ok=True, message=f"已新增 {len(specs)} 条防火墙规则", data={})

    s.add_nsg_rules = _add  # type: ignore[method-assign]
    monkeypatch.setattr(
        cf,
        "fetch_cloudflare_ips",
        lambda *a, **kw: feed
        or {"ipv4": ["1.1.1.0/24", "2.2.2.0/24"], "ipv6": ["2606:4700::/32"],
            "etag": "x", "source": "live", "note": ""},
    )
    return s


def test_it_adds_one_rule_per_cidr_per_port(monkeypatch):
    s = _session([], monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80, 443])
    assert r.ok, r.message
    # 3 个网段 × 2 个端口
    assert len(s.added) == 6
    assert {spec.cidr for spec in s.added} == {"1.1.1.0/24", "2.2.2.0/24", "2606:4700::/32"}
    assert {spec.port_min for spec in s.added} == {80, 443}
    assert all(spec.direction == "INGRESS" and spec.protocol == "6" for spec in s.added)


def test_ipv6_can_be_left_out(monkeypatch):
    s = _session([], monkeypatch)
    s.add_cloudflare_rules("nsg1", ports=[443], include_ipv6=False)
    assert {spec.cidr for spec in s.added} == {"1.1.1.0/24", "2.2.2.0/24"}


def test_clicking_twice_does_not_duplicate(monkeypatch):
    """已有的完全相同的规则要跳过 —— 否则点两次就是两套重复规则白占额度。"""
    existing = [_rule("1.1.1.0/24", 80, 80), _rule("2.2.2.0/24", 80, 80)]
    s = _session(existing, monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80])
    assert r.ok
    assert [spec.cidr for spec in s.added] == ["2606:4700::/32"]
    assert r.data["skipped"] == 2


def test_a_wide_open_rule_makes_every_cidr_redundant_and_says_so(monkeypatch):
    """最重要的一条：0.0.0.0/0 在场时，加 Cloudflare 网段**一条都不改变可达性**。

    用户点这个按钮的目的是「只让 Cloudflare 进来」。如果面板只回一句「已放行」，
    他会以为源站锁好了 —— 而实际上全网都还进得来。
    """
    s = _session([_rule("0.0.0.0/0", None, None)], monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80, 443], include_ipv6=False)
    assert r.ok
    assert not s.added, "已经全放行了还去加规则"
    assert "⚠" in r.message
    assert "白名单" in r.message
    assert "仍然全网可达" in r.message
    assert r.data["wide_open"] == [80, 443]


def test_the_warning_also_fires_when_rules_were_actually_added(monkeypatch):
    """宽规则只盖了 80，Cloudflare 要 80+443 —— 443 那批会真的写进去。

    这时候仍然要警告：80 那个口子还开着，源站没被锁住。
    「加成功了」和「你以为的保护并不存在」可以同时为真。
    """
    s = _session([_rule("0.0.0.0/0", 80, 80)], monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80, 443], include_ipv6=False)
    assert r.ok
    assert {spec.port_min for spec in s.added} == {443}
    assert "⚠" in r.message
    assert r.data["wide_open"] == [80]


def test_it_refuses_before_blowing_the_120_rule_ceiling(monkeypatch):
    """NSG 每组 120 条是 Oracle 硬限制。撞上去 Oracle 只回一条含糊的 400，
    而那时已经打了好几次 API。"""
    existing = [_rule(f"10.0.{i}.0/24", 22, 22) for i in range(119)]
    s = _session(existing, monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80, 443])
    assert r.ok is False
    assert not s.added, "明知写不下还是发了请求"
    assert "120" in r.message
    assert "不可调整" in r.message
    # 要给出路，不能只说不行。
    assert "IPv6" in r.message


def test_egress_rules_do_not_count_as_covering_ingress(monkeypatch):
    """出站的 0.0.0.0/0 和入站可达性毫无关系，不能拿它去跳过或告警。"""
    s = _session([_rule("0.0.0.0/0", None, None, direction="EGRESS")], monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80], include_ipv6=False)
    assert r.ok
    assert len(s.added) == 2
    assert r.data["wide_open"] == []


def test_a_udp_rule_does_not_cover_a_tcp_need(monkeypatch):
    """协议 17 (UDP) 放行 80 端口，挡不住也不等于 TCP 80 已经开了。"""
    s = _session([_rule("0.0.0.0/0", 80, 80, proto="17")], monkeypatch)
    r = s.add_cloudflare_rules("nsg1", ports=[80], include_ipv6=False)
    assert r.ok
    assert len(s.added) == 2
    assert r.data["wide_open"] == []


def test_the_fallback_source_is_reported_to_the_user(monkeypatch):
    """用内置表就得说 —— 它可能已经过期，用户得知道该不该复核。"""
    s = _session([], monkeypatch, feed=cf.fallback_ips())
    r = s.add_cloudflare_rules("nsg1", ports=[443])
    assert r.ok
    assert r.data["source"] == "fallback"
    assert "内置" in r.message


def test_no_ports_is_rejected(monkeypatch):
    s = _session([], monkeypatch)
    assert s.add_cloudflare_rules("nsg1", ports=[]).ok is False
