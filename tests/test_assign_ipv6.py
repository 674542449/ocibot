"""Unit tests for auto-enabling IPv6 on VCN/Subnet when assigning public IPv6."""

from __future__ import annotations

from types import SimpleNamespace

import oci

from app.oci_client import PrimaryNetworkInfo, TenantSession


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeNetwork:
    def __init__(self):
        self.vcns = {}
        self.subnets = {}
        self.igws = {}
        self.route_tables = {}
        self.created_ipv6 = []
        self.add_vcn_calls = []
        self.add_subnet_calls = []
        self._seq = 0

    def _id(self, kind: str) -> str:
        self._seq += 1
        return f"ocid1.{kind}.test.{self._seq:04d}"

    def get_subnet(self, subnet_id: str):
        return FakeResponse(self.subnets[subnet_id])

    def get_vcn(self, vcn_id: str):
        return FakeResponse(self.vcns[vcn_id])

    def get_route_table(self, rt_id: str):
        return FakeResponse(self.route_tables[rt_id])

    def get_internet_gateway(self, igw_id: str):
        return FakeResponse(self.igws[igw_id])

    def list_subnets(self, compartment_id: str, vcn_id: str = "", **_kwargs):
        items = [
            s
            for s in self.subnets.values()
            if s.compartment_id == compartment_id and (not vcn_id or s.vcn_id == vcn_id)
        ]
        return FakeResponse(items)

    def list_internet_gateways(self, compartment_id: str, vcn_id: str = "", **_kwargs):
        items = [
            g
            for g in self.igws.values()
            if g.compartment_id == compartment_id and (not vcn_id or g.vcn_id == vcn_id)
        ]
        return FakeResponse(items)

    def add_ipv6_vcn_cidr(self, vcn_id: str, add_vcn_ipv6_cidr_details=None, **_kwargs):
        self.add_vcn_calls.append((vcn_id, add_vcn_ipv6_cidr_details))
        vcn = self.vcns[vcn_id]
        vcn.ipv6_cidr_blocks = ["2603:c020:8004:1a00::/56"]
        vcn.lifecycle_state = "AVAILABLE"
        return FakeResponse(None)

    def add_ipv6_subnet_cidr(self, subnet_id: str, details, **_kwargs):
        self.add_subnet_calls.append((subnet_id, details.ipv6_cidr_block))
        subnet = self.subnets[subnet_id]
        subnet.ipv6_cidr_block = details.ipv6_cidr_block
        subnet.ipv6_cidr_blocks = [details.ipv6_cidr_block]
        subnet.lifecycle_state = "AVAILABLE"
        return FakeResponse(None)

    def create_internet_gateway(self, details):
        igw_id = self._id("igw")
        igw = SimpleNamespace(
            id=igw_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            vcn_id=details.vcn_id,
            is_enabled=True,
            lifecycle_state="AVAILABLE",
        )
        self.igws[igw_id] = igw
        return FakeResponse(igw)

    def update_internet_gateway(self, igw_id, details):
        igw = self.igws[igw_id]
        if getattr(details, "is_enabled", None) is not None:
            igw.is_enabled = bool(details.is_enabled)
        return FakeResponse(igw)

    def update_route_table(self, rt_id, details):
        rt = self.route_tables[rt_id]
        if getattr(details, "route_rules", None) is not None:
            rt.route_rules = list(details.route_rules)
        return FakeResponse(rt)

    def create_ipv6(self, details):
        self.created_ipv6.append(details.vnic_id)
        return FakeResponse(SimpleNamespace(id=self._id("ipv6"), ip_address="2603:c020:8004:1a00::10"))


def _seed(net: FakeNetwork, *, vcn_v6=None, subnet_v6=None):
    vcn = SimpleNamespace(
        id="vcn-1",
        display_name="vcn",
        compartment_id="comp",
        cidr_block="10.0.0.0/16",
        cidr_blocks=["10.0.0.0/16"],
        ipv6_cidr_blocks=list(vcn_v6 or []),
        ipv6_cidr_block=(vcn_v6 or [""])[0] if vcn_v6 else "",
        lifecycle_state="AVAILABLE",
    )
    subnet = SimpleNamespace(
        id="subnet-1",
        display_name="subnet",
        compartment_id="comp",
        vcn_id="vcn-1",
        cidr_block="10.0.0.0/24",
        ipv6_cidr_block=subnet_v6 or "",
        ipv6_cidr_blocks=[subnet_v6] if subnet_v6 else [],
        route_table_id="rt-1",
        lifecycle_state="AVAILABLE",
        prohibit_public_ip_on_vnic=False,
    )
    rt = SimpleNamespace(
        id="rt-1",
        display_name="rt",
        compartment_id="comp",
        vcn_id="vcn-1",
        route_rules=[],
        lifecycle_state="AVAILABLE",
    )
    net.vcns[vcn.id] = vcn
    net.subnets[subnet.id] = subnet
    net.route_tables[rt.id] = rt
    return vcn, subnet, rt


def make_session(net: FakeNetwork, info: PrimaryNetworkInfo | None = None) -> TenantSession:
    session = TenantSession.__new__(TenantSession)
    session.tenant = SimpleNamespace(tenancy_ocid="tenancy", compartment_ocid="comp")
    session._network = net
    session.resolve_compartment = lambda: "comp"  # type: ignore[method-assign]
    session.resolve_primary_network = lambda *_a, **_k: info or PrimaryNetworkInfo(  # type: ignore[method-assign]
        vnic_id="vnic-1",
        subnet_id="subnet-1",
        private_ip_id="pip-1",
        private_ip_compartment_id="comp",
    )
    return session


def test_ensure_subnet_ipv6_enables_vcn_and_subnet(monkeypatch):
    net = FakeNetwork()
    _seed(net)
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_subnet_ipv6("subnet-1", "comp")
    assert result.ok, result.message
    assert result.data["created"] is True
    assert net.add_vcn_calls, "should request Oracle GUA on VCN"
    assert getattr(net.add_vcn_calls[0][1], "is_oracle_gua_allocation_enabled") is True
    assert net.add_subnet_calls
    assert net.add_subnet_calls[0][1] == "2603:c020:8004:1a00::/64"
    assert "2603:c020:8004:1a00::/64" in result.data["ipv6_cidr_blocks"]
    dests = {getattr(r, "destination", "") for r in net.route_tables["rt-1"].route_rules}
    assert "::/0" in dests


def test_ensure_subnet_ipv6_skips_prefixes_but_repairs_route_when_already_enabled(monkeypatch):
    net = FakeNetwork()
    _seed(net, vcn_v6=["2603:c020:8004:1a00::/56"], subnet_v6="2603:c020:8004:1a00::/64")
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_subnet_ipv6("subnet-1", "comp")
    assert result.ok
    assert result.data["created"] is False
    assert result.data["route_ok"] is True
    assert net.add_vcn_calls == []
    assert net.add_subnet_calls == []
    routes = [r for r in net.route_tables["rt-1"].route_rules if r.destination == "::/0"]
    assert len(routes) == 1
    assert routes[0].network_entity_id in net.igws


def test_ipv6_route_replaces_wrong_target_and_preserves_other_rules(monkeypatch):
    net = FakeNetwork()
    _vcn, _subnet, rt = _seed(
        net,
        vcn_v6=["2603:c020:8004:1a00::/56"],
        subnet_v6="2603:c020:8004:1a00::/64",
    )
    igw = SimpleNamespace(
        id="igw-1", display_name="igw", compartment_id="comp", vcn_id="vcn-1",
        is_enabled=True, lifecycle_state="AVAILABLE",
    )
    net.igws[igw.id] = igw
    rt.route_rules = [
        oci.core.models.RouteRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id="nat-1"
        ),
        oci.core.models.RouteRule(
            destination="::/0", destination_type="CIDR_BLOCK", network_entity_id="wrong-igw"
        ),
        oci.core.models.RouteRule(
            destination="2001:db8::/32", destination_type="CIDR_BLOCK", network_entity_id="drg-1"
        ),
    ]
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_ipv6_internet_access("subnet-1", "comp")
    assert result.ok
    assert result.data["corrected_route"] is True
    assert {(r.destination, r.network_entity_id) for r in rt.route_rules} == {
        ("0.0.0.0/0", "nat-1"),
        ("2001:db8::/32", "drg-1"),
        ("::/0", "igw-1"),
    }


def test_ipv6_route_enables_disabled_gateway(monkeypatch):
    net = FakeNetwork()
    _seed(net, vcn_v6=["2603:c020:8004:1a00::/56"], subnet_v6="2603:c020:8004:1a00::/64")
    igw = SimpleNamespace(
        id="igw-1", display_name="igw", compartment_id="comp", vcn_id="vcn-1",
        is_enabled=False, lifecycle_state="AVAILABLE",
    )
    net.igws[igw.id] = igw
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_ipv6_internet_access("subnet-1", "comp")
    assert result.ok
    assert result.data["enabled_igw"] is True
    assert net.igws["igw-1"].is_enabled is True


def test_assign_public_ipv6_auto_enables_then_allocates(monkeypatch):
    net = FakeNetwork()
    _seed(net)
    info = PrimaryNetworkInfo(
        vnic_id="vnic-1",
        subnet_id="subnet-1",
        private_ip_id="pip-1",
        private_ip_compartment_id="comp",
    )
    session = make_session(net, info)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.assign_public_ipv6("instance-1", "comp")
    assert result.ok, result.message
    assert result.data["ipv6"] == "2603:c020:8004:1a00::10"
    assert net.created_ipv6 == ["vnic-1"]
    assert net.add_vcn_calls
    assert net.add_subnet_calls


def test_pick_subnet_ipv6_avoids_used_prefix(monkeypatch):
    net = FakeNetwork()
    vcn, _subnet, _rt = _seed(net, vcn_v6=["2603:c020:8004:1a00::/56"])
    sibling = SimpleNamespace(
        id="subnet-2",
        display_name="other",
        compartment_id="comp",
        vcn_id="vcn-1",
        cidr_block="10.0.1.0/24",
        ipv6_cidr_block="2603:c020:8004:1a00::/64",
        ipv6_cidr_blocks=["2603:c020:8004:1a00::/64"],
        route_table_id="rt-1",
        lifecycle_state="AVAILABLE",
        prohibit_public_ip_on_vnic=False,
    )
    net.subnets[sibling.id] = sibling
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    chosen = session._pick_subnet_ipv6_cidr(vcn, "subnet-1", "comp")
    assert chosen == "2603:c020:8004:1a01::/64"
