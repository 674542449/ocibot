"""Unit tests for TenantSession.ensure_default_network."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import oci

from app.oci_client import (
    DEFAULT_SUBNET_CIDR,
    DEFAULT_SUBNET_NAME,
    DEFAULT_VCN_CIDR,
    DEFAULT_VCN_NAME,
    TenantSession,
)


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeNetwork:
    """In-memory VirtualNetworkClient stand-in for ensure_default_network tests."""

    def __init__(self):
        self.vcns: dict[str, SimpleNamespace] = {}
        self.subnets: dict[str, SimpleNamespace] = {}
        self.igws: dict[str, SimpleNamespace] = {}
        self.route_tables: dict[str, SimpleNamespace] = {}
        self.security_lists: dict[str, SimpleNamespace] = {}
        self._seq = 0

    def _id(self, kind: str) -> str:
        self._seq += 1
        return f"ocid1.{kind}.test.{self._seq:04d}"

    # --- list / get ---
    def list_vcns(self, compartment_id: str, **_kwargs):
        items = [
            v
            for v in self.vcns.values()
            if v.compartment_id == compartment_id and v.lifecycle_state == "AVAILABLE"
        ]
        return FakeResponse(items)

    def list_subnets(self, compartment_id: str, vcn_id: Optional[str] = None, **_kwargs):
        items = []
        for s in self.subnets.values():
            if s.compartment_id != compartment_id or s.lifecycle_state != "AVAILABLE":
                continue
            if vcn_id and s.vcn_id != vcn_id:
                continue
            items.append(s)
        return FakeResponse(items)

    def list_internet_gateways(self, compartment_id: str, vcn_id: Optional[str] = None, **_kwargs):
        items = [
            g
            for g in self.igws.values()
            if g.compartment_id == compartment_id and (not vcn_id or g.vcn_id == vcn_id)
        ]
        return FakeResponse(items)

    def list_route_tables(self, compartment_id: str, vcn_id: Optional[str] = None, **_kwargs):
        items = [
            t
            for t in self.route_tables.values()
            if t.compartment_id == compartment_id and (not vcn_id or t.vcn_id == vcn_id)
        ]
        return FakeResponse(items)

    def list_security_lists(self, compartment_id: str, vcn_id: Optional[str] = None, **_kwargs):
        items = [
            sl
            for sl in self.security_lists.values()
            if sl.compartment_id == compartment_id and (not vcn_id or sl.vcn_id == vcn_id)
        ]
        return FakeResponse(items)

    def get_vcn(self, vcn_id: str):
        return FakeResponse(self.vcns[vcn_id])

    def get_subnet(self, subnet_id: str):
        return FakeResponse(self.subnets[subnet_id])

    def get_internet_gateway(self, igw_id: str):
        return FakeResponse(self.igws[igw_id])

    def get_route_table(self, rt_id: str):
        return FakeResponse(self.route_tables[rt_id])

    # --- create / update ---
    def create_vcn(self, details):
        vcn_id = self._id("vcn")
        cidr_blocks = list(getattr(details, "cidr_blocks", None) or [])
        if getattr(details, "cidr_block", None):
            cidr_blocks = [details.cidr_block] + cidr_blocks
        vcn = SimpleNamespace(
            id=vcn_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            cidr_block=cidr_blocks[0] if cidr_blocks else DEFAULT_VCN_CIDR,
            cidr_blocks=cidr_blocks or [DEFAULT_VCN_CIDR],
            ipv6_cidr_blocks=[],
            lifecycle_state="AVAILABLE",
            freeform_tags=dict(getattr(details, "freeform_tags", None) or {}),
        )
        self.vcns[vcn_id] = vcn
        return FakeResponse(vcn)

    def create_internet_gateway(self, details):
        igw_id = self._id("igw")
        igw = SimpleNamespace(
            id=igw_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            vcn_id=details.vcn_id,
            is_enabled=bool(details.is_enabled),
            lifecycle_state="AVAILABLE",
            freeform_tags=dict(getattr(details, "freeform_tags", None) or {}),
        )
        self.igws[igw_id] = igw
        return FakeResponse(igw)

    def update_internet_gateway(self, igw_id: str, details):
        igw = self.igws[igw_id]
        if getattr(details, "is_enabled", None) is not None:
            igw.is_enabled = bool(details.is_enabled)
        return FakeResponse(igw)

    def create_route_table(self, details):
        rt_id = self._id("routetable")
        rt = SimpleNamespace(
            id=rt_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            vcn_id=details.vcn_id,
            route_rules=list(details.route_rules or []),
            lifecycle_state="AVAILABLE",
            freeform_tags=dict(getattr(details, "freeform_tags", None) or {}),
        )
        self.route_tables[rt_id] = rt
        return FakeResponse(rt)

    def update_route_table(self, rt_id: str, details):
        rt = self.route_tables[rt_id]
        if getattr(details, "route_rules", None) is not None:
            rt.route_rules = list(details.route_rules)
        return FakeResponse(rt)

    def create_security_list(self, details):
        sl_id = self._id("securitylist")
        sl = SimpleNamespace(
            id=sl_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            vcn_id=details.vcn_id,
            ingress_security_rules=list(details.ingress_security_rules or []),
            egress_security_rules=list(details.egress_security_rules or []),
            lifecycle_state="AVAILABLE",
            freeform_tags=dict(getattr(details, "freeform_tags", None) or {}),
        )
        self.security_lists[sl_id] = sl
        return FakeResponse(sl)

    def create_subnet(self, details):
        subnet_id = self._id("subnet")
        subnet = SimpleNamespace(
            id=subnet_id,
            display_name=details.display_name,
            compartment_id=details.compartment_id,
            vcn_id=details.vcn_id,
            cidr_block=details.cidr_block,
            availability_domain=getattr(details, "availability_domain", "") or "",
            prohibit_public_ip_on_vnic=bool(getattr(details, "prohibit_public_ip_on_vnic", False)),
            prohibit_internet_ingress=bool(getattr(details, "prohibit_internet_ingress", False)),
            ipv6_cidr_block="",
            ipv6_cidr_blocks=[],
            security_list_ids=list(getattr(details, "security_list_ids", None) or []),
            route_table_id=getattr(details, "route_table_id", "") or "",
            lifecycle_state="AVAILABLE",
            freeform_tags=dict(getattr(details, "freeform_tags", None) or {}),
        )
        self.subnets[subnet_id] = subnet
        return FakeResponse(subnet)


def _seed_vcn(net: FakeNetwork, *, compartment: str = "comp", name: str = "existing-vcn") -> SimpleNamespace:
    vcn_id = net._id("vcn")
    vcn = SimpleNamespace(
        id=vcn_id,
        display_name=name,
        compartment_id=compartment,
        cidr_block="10.1.0.0/16",
        cidr_blocks=["10.1.0.0/16"],
        ipv6_cidr_blocks=[],
        lifecycle_state="AVAILABLE",
        freeform_tags={},
    )
    net.vcns[vcn_id] = vcn
    return vcn


def _seed_subnet(
    net: FakeNetwork,
    vcn: SimpleNamespace,
    *,
    name: str = "existing-subnet",
    public: bool = True,
    cidr: str = "10.1.0.0/24",
) -> SimpleNamespace:
    subnet_id = net._id("subnet")
    subnet = SimpleNamespace(
        id=subnet_id,
        display_name=name,
        compartment_id=vcn.compartment_id,
        vcn_id=vcn.id,
        cidr_block=cidr,
        availability_domain="",
        prohibit_public_ip_on_vnic=not public,
        prohibit_internet_ingress=False,
        ipv6_cidr_block="",
        ipv6_cidr_blocks=[],
        security_list_ids=[],
        route_table_id="",
        lifecycle_state="AVAILABLE",
        freeform_tags={},
    )
    net.subnets[subnet_id] = subnet
    return subnet


def make_session(network: FakeNetwork, *, tenancy: str = "tenancy", compartment: str = "comp") -> TenantSession:
    session = TenantSession.__new__(TenantSession)
    session.tenant = SimpleNamespace(tenancy_ocid=tenancy, compartment_ocid=compartment)
    session._network = network
    session.resolve_compartment = lambda: compartment  # type: ignore[method-assign]
    # Bypass wait loops — fakes are already AVAILABLE.
    session._wait_network_resource = (  # type: ignore[method-assign]
        lambda getter, resource_id, **_kwargs: getter(resource_id).data
    )
    return session


def test_reuses_existing_public_subnet(monkeypatch):
    net = FakeNetwork()
    vcn = _seed_vcn(net)
    subnet = _seed_subnet(net, vcn, public=True)
    session = make_session(net)

    # list_* go through oci.pagination; patch to call our fake directly.
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp")
    assert result.ok
    assert result.data["created"] is False
    assert result.data["subnet"]["id"] == subnet.id
    assert result.data["vcn"]["id"] == vcn.id
    assert net.vcns  # unchanged count path — no new VCN
    assert len(net.vcns) == 1
    assert len(net.subnets) == 1


def test_prefers_public_over_private_subnet(monkeypatch):
    net = FakeNetwork()
    vcn = _seed_vcn(net)
    private = _seed_subnet(net, vcn, name="private", public=False, cidr="10.1.1.0/24")
    public = _seed_subnet(net, vcn, name="public", public=True, cidr="10.1.0.0/24")
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp")
    assert result.ok
    assert result.data["created"] is False
    assert result.data["subnet"]["id"] == public.id
    assert result.data["subnet"]["id"] != private.id


def test_creates_full_stack_when_empty(monkeypatch):
    net = FakeNetwork()
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp")
    assert result.ok, result.message
    assert result.data["created"] is True
    assert result.data["vcn"]["display_name"] == DEFAULT_VCN_NAME
    assert result.data["subnet"]["display_name"] == DEFAULT_SUBNET_NAME
    assert result.data["subnet"]["cidr_block"] == DEFAULT_SUBNET_CIDR
    assert result.data["subnet"]["prohibit_public_ip_on_vnic"] is False
    assert len(net.vcns) == 1
    assert len(net.subnets) == 1
    assert len(net.igws) == 1
    assert len(net.route_tables) == 1
    assert len(net.security_lists) == 1
    # IPv4-only VCN must NOT get ::/0 (OCI rejects it with InvalidParameter).
    rules = list(net.route_tables.values())[0].route_rules
    dests = {getattr(r, "destination", "") for r in rules}
    assert "0.0.0.0/0" in dests
    assert "::/0" not in dests
    # Security list likewise IPv4-only.
    sl = list(net.security_lists.values())[0]
    assert all(
        (getattr(r, "source", None) or getattr(r, "destination", None)) != "::/0"
        for r in list(sl.ingress_security_rules) + list(sl.egress_security_rules)
    )


def test_creates_subnet_on_existing_vcn_without_subnets(monkeypatch):
    net = FakeNetwork()
    vcn = _seed_vcn(net, name="lonely-vcn")
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp")
    assert result.ok, result.message
    assert result.data["created"] is True
    assert result.data["vcn"]["id"] == vcn.id
    assert len(net.vcns) == 1  # did not create another VCN
    assert len(net.subnets) == 1
    assert result.data["subnet"]["vcn_id"] == vcn.id
    assert result.data["subnet"]["prohibit_public_ip_on_vnic"] is False
    # Existing IPv4-only VCN must not receive ::/0 either.
    rules = list(net.route_tables.values())[0].route_rules
    dests = {getattr(r, "destination", "") for r in rules}
    assert "0.0.0.0/0" in dests
    assert "::/0" not in dests


def test_ipv6_enabled_vcn_gets_v6_default_route(monkeypatch):
    net = FakeNetwork()
    vcn = _seed_vcn(net, name="v6-vcn")
    vcn.ipv6_cidr_blocks = ["2603:c020:8004:1a00::/56"]
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp")
    assert result.ok, result.message
    rules = list(net.route_tables.values())[0].route_rules
    dests = {getattr(r, "destination", "") for r in rules}
    assert "0.0.0.0/0" in dests
    assert "::/0" in dests


def test_missing_without_create_flag(monkeypatch):
    net = FakeNetwork()
    session = make_session(net)
    monkeypatch.setattr(
        oci.pagination,
        "list_call_get_all_results",
        lambda fn, *args, **kwargs: SimpleNamespace(data=fn(*args, **kwargs).data),
    )

    result = session.ensure_default_network(compartment_id="comp", create_if_missing=False)
    assert not result.ok
    assert result.data["created"] is False
