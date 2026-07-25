from types import SimpleNamespace

from app.oci_client import FirewallRuleSpec, OperationResult, TenantSession


class FakeNsgNetwork:
    def __init__(self):
        self.created = None
        self.deleted = []

    def create_network_security_group(self, details):
        self.created = details
        return SimpleNamespace(data=SimpleNamespace(id="nsg-new"))

    def delete_network_security_group(self, nsg_id):
        self.deleted.append(nsg_id)


def test_open_all_includes_both_directions_and_ip_families():
    rules = TenantSession._open_all_specs(include_ipv6=True)
    assert {(rule.direction, rule.cidr, rule.protocol) for rule in rules} == {
        ("INGRESS", "0.0.0.0/0", "all"),
        ("EGRESS", "0.0.0.0/0", "all"),
        ("INGRESS", "::/0", "all"),
        ("EGRESS", "::/0", "all"),
    }


def test_open_all_ipv4_only_when_no_ipv6():
    rules = TenantSession._open_all_specs(include_ipv6=False)
    assert {(rule.direction, rule.cidr, rule.protocol) for rule in rules} == {
        ("INGRESS", "0.0.0.0/0", "all"),
        ("EGRESS", "0.0.0.0/0", "all"),
    }
    assert all(rule.cidr != "::/0" for rule in rules)


def test_create_managed_nsg_installs_ipv4_and_ipv6_rules():
    network = FakeNsgNetwork()
    session = TenantSession.__new__(TenantSession)
    session._network = network
    added = []
    session.add_nsg_rules = lambda nsg_id, specs: (  # type: ignore[method-assign]
        added.extend((s.direction, s.cidr) for s in specs)
        or OperationResult(ok=True, message="added")
    )

    result = session.create_managed_nsg(
        vcn_id="vcn-1", compartment_id="comp", display_name="server", include_ipv6=True
    )

    assert result.ok
    assert set(added) == {
        ("INGRESS", "0.0.0.0/0"),
        ("EGRESS", "0.0.0.0/0"),
        ("INGRESS", "::/0"),
        ("EGRESS", "::/0"),
    }


def test_create_managed_nsg_cleans_up_when_rules_fail():
    network = FakeNsgNetwork()
    session = TenantSession.__new__(TenantSession)
    session._network = network
    session.add_nsg_rules = lambda *_a, **_k: OperationResult(ok=False, message="denied")  # type: ignore[method-assign]

    result = session.create_managed_nsg(
        vcn_id="vcn-1", compartment_id="comp", display_name="server", include_ipv6=True
    )

    assert not result.ok
    assert network.deleted == ["nsg-new"]


def test_tcp_rule_maps_to_oci_destination_port():
    model = TenantSession._firewall_rule_model(
        FirewallRuleSpec("INGRESS", "6", "192.0.2.1/32", 22, 22, description="ssh")
    )
    assert model.direction == "INGRESS"
    assert model.source == "192.0.2.1/32"
    assert model.protocol == "6"
    assert model.tcp_options.destination_port_range.min == 22
    assert model.tcp_options.destination_port_range.max == 22


def test_udp_egress_rule_maps_destination():
    model = TenantSession._firewall_rule_model(
        FirewallRuleSpec("EGRESS", "17", "2001:db8::/32", 53, 53)
    )
    assert model.direction == "EGRESS"
    assert model.destination == "2001:db8::/32"
    assert model.udp_options.destination_port_range.min == 53


def test_replace_firewall_open_all_ipv4_only_clears_then_opens():
    session = TenantSession.__new__(TenantSession)
    deleted = []
    added = []

    session.get_instance_firewall = lambda *_a, **_k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="ok",
        data={
            "has_ipv6": False,
            "groups": [
                {
                    "id": "nsg-1",
                    "display_name": "nsg",
                    "rules": [{"id": "r1"}, {"id": "r2"}],
                }
            ],
        },
    )
    session.delete_nsg_rules = lambda nsg_id, ids: (  # type: ignore[method-assign]
        deleted.append((nsg_id, list(ids)))
        or OperationResult(ok=True, message=f"已删除 {len(ids)} 条规则", data={"count": len(ids)})
    )
    session.add_nsg_rules = lambda nsg_id, specs: (  # type: ignore[method-assign]
        added.append((nsg_id, [(s.direction, s.cidr) for s in specs]))
        or OperationResult(ok=True, message="ok")
    )

    result = session.replace_instance_firewall_with_open_all("inst", "comp")
    assert result.ok, result.message
    assert deleted == [("nsg-1", ["r1", "r2"])]
    assert added == [
        ("nsg-1", [("INGRESS", "0.0.0.0/0"), ("EGRESS", "0.0.0.0/0")])
    ]
    assert result.data["include_ipv6"] is False
    assert "IPv4" in result.message
    assert "IPv6" not in result.message or "IPv4 + IPv6" not in result.message


def test_replace_firewall_open_all_includes_ipv6_when_present():
    session = TenantSession.__new__(TenantSession)
    added = []

    session.get_instance_firewall = lambda *_a, **_k: OperationResult(  # type: ignore[method-assign]
        ok=True,
        message="ok",
        data={
            "has_ipv6": True,
            "groups": [{"id": "nsg-1", "display_name": "nsg", "rules": [{"id": "old"}]}],
        },
    )
    session.delete_nsg_rules = lambda nsg_id, ids: OperationResult(  # type: ignore[method-assign]
        ok=True, message="deleted", data={"count": len(ids)}
    )
    session.add_nsg_rules = lambda nsg_id, specs: (  # type: ignore[method-assign]
        added.append([(s.direction, s.cidr) for s in specs])
        or OperationResult(ok=True, message="ok")
    )

    result = session.replace_instance_firewall_with_open_all("inst", "comp")
    assert result.ok
    assert result.data["include_ipv6"] is True
    assert set(added[0]) == {
        ("INGRESS", "0.0.0.0/0"),
        ("EGRESS", "0.0.0.0/0"),
        ("INGRESS", "::/0"),
        ("EGRESS", "::/0"),
    }
    assert "IPv4 + IPv6" in result.message


def test_replace_firewall_open_all_creates_nsg_when_missing():
    session = TenantSession.__new__(TenantSession)
    calls = {"ensure": 0, "get": 0}
    added = []

    def get_fw(*_a, **_k):
        calls["get"] += 1
        if calls["get"] == 1:
            return OperationResult(ok=True, message="ok", data={"has_ipv6": False, "groups": []})
        return OperationResult(
            ok=True,
            message="ok",
            data={
                "has_ipv6": False,
                "groups": [{"id": "nsg-new", "display_name": "managed", "rules": []}],
            },
        )

    session.get_instance_firewall = get_fw  # type: ignore[method-assign]
    session.ensure_instance_nsg = lambda *_a, **_k: (  # type: ignore[method-assign]
        calls.__setitem__("ensure", calls["ensure"] + 1)
        or OperationResult(ok=True, message="created", data={"nsg_ids": ["nsg-new"]})
    )
    session.delete_nsg_rules = lambda *_a, **_k: OperationResult(ok=True, message="none", data={"count": 0})  # type: ignore[method-assign]
    session.add_nsg_rules = lambda nsg_id, specs: (  # type: ignore[method-assign]
        added.append((nsg_id, len(specs))) or OperationResult(ok=True, message="ok")
    )

    result = session.replace_instance_firewall_with_open_all("inst", "comp")
    assert result.ok, result.message
    assert calls["ensure"] == 1
    assert added == [("nsg-new", 2)]


def test_normalize_firewall_rule_chinese_labels():
    rule = SimpleNamespace(
        id="rule-1",
        direction="INGRESS",
        protocol="6",
        source="0.0.0.0/0",
        destination=None,
        tcp_options=SimpleNamespace(destination_port_range=SimpleNamespace(min=22, max=22)),
        udp_options=None,
        is_stateless=False,
        description="ssh",
    )
    normalized = TenantSession._normalize_firewall_rule(rule)
    assert normalized["direction_label"] == "入站"
    assert normalized["protocol_label"] == "TCP"
    assert normalized["port"] == "22"
    assert normalized["description"] == "ssh"

    open_rule = SimpleNamespace(
        id="rule-2",
        direction="EGRESS",
        protocol="all",
        source=None,
        destination="::/0",
        tcp_options=None,
        udp_options=None,
        is_stateless=True,
        description="open",
    )
    normalized_open = TenantSession._normalize_firewall_rule(open_rule)
    assert normalized_open["direction_label"] == "出站"
    assert normalized_open["protocol_label"] == "全部协议"
    assert normalized_open["port"] == "全部"
    assert normalized_open["cidr"] == "::/0"


def test_is_ocibot_managed_nsg_accepts_legacy_and_current_tags():
    assert TenantSession._is_ocibot_managed_nsg({"managed_by": "oci-console-helper"})
    assert TenantSession._is_ocibot_managed_nsg({"ocibot_managed": "true"})
    assert TenantSession._is_ocibot_managed_nsg(
        {"managed_by": "oci-console-helper", "ocibot_managed": "true"}
    )
    assert not TenantSession._is_ocibot_managed_nsg({})
    assert not TenantSession._is_ocibot_managed_nsg({"managed_by": "someone-else"})


def test_create_managed_nsg_tags_include_both_markers():
    network = FakeNsgNetwork()
    session = TenantSession.__new__(TenantSession)
    session._network = network
    session.add_nsg_rules = lambda *_a, **_k: OperationResult(ok=True, message="added")  # type: ignore[method-assign]

    result = session.create_managed_nsg(
        vcn_id="vcn-1", compartment_id="comp", display_name="server", include_ipv6=False
    )
    assert result.ok
    tags = network.created.freeform_tags
    assert tags["managed_by"] == "oci-console-helper"
    assert tags["ocibot_managed"] == "true"
    assert tags.get("launch_token")
