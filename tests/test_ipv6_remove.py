"""Cancelling an instance's public IPv6.

The inverse of assign_public_ipv6, but deliberately NOT symmetric: assigning may
enable an IPv6 prefix on the VCN and subnet and add a ``::/0`` route, and those
are shared. Removing them here would take every other instance in the subnet off
IPv6 as a side effect of one machine's change, so this only deletes the
addresses on that instance's own VNIC.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.oci_client import TenantSession  # noqa: E402


class _Network:
    def __init__(self, addresses, *, fail_on=()):
        self._v6 = [
            types.SimpleNamespace(id=f"ocid1.ipv6.oc1..v{i}", ip_address=a)
            for i, a in enumerate(addresses)
        ]
        self._fail_on = set(fail_on)
        self.deleted: list[str] = []

    def list_ipv6s(self, **kwargs):
        # A real Response: oci.pagination.list_call_get_all_results reads status,
        # headers and has_next_page off it, and guessing which attributes it
        # needs one failure at a time is not a test worth having.
        from oci.response import Response

        return Response(200, {}, list(self._v6), None)

    def delete_ipv6(self, ipv6_id, **kwargs):
        match = next((v for v in self._v6 if v.id == ipv6_id), None)
        if match is not None and match.ip_address in self._fail_on:
            raise RuntimeError("oracle refused")
        self.deleted.append(ipv6_id)
        return types.SimpleNamespace(data=None)


def _session(addresses, *, vnic="ocid1.vnic.oc1..v", fail_on=()):
    s = TenantSession.__new__(TenantSession)
    net = _Network(addresses, fail_on=fail_on)
    s._network = net
    s.resolve_primary_network = lambda instance_id, compartment_id, **k: types.SimpleNamespace(  # type: ignore[method-assign]
        vnic_id=vnic, subnet_id="ocid1.subnet.oc1..s", ipv6_addresses=list(addresses)
    )
    return s, net


def test_removes_every_address_on_the_vnic():
    s, net = _session(["2603:c020::1", "2603:c020::2"])
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok, res.message
    assert len(net.deleted) == 2
    assert set(res.data["removed"]) == {"2603:c020::1", "2603:c020::2"}


def test_no_ipv6_is_success_not_an_error():
    """A second click must not produce an alarming message."""
    s, net = _session([])
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok is True
    assert net.deleted == []
    assert "没有 IPv6" in res.message


def test_shared_network_resources_are_left_alone():
    """The subnet /64, the VCN prefix and the ::/0 route belong to the whole
    subnet. Removing them because one instance dropped its address would take
    the other instances off IPv6 too."""
    s, net = _session(["2603:c020::1"])
    for forbidden in ("remove_ipv6_subnet_cidr", "remove_ipv6_vcn_cidr"):
        setattr(
            net,
            forbidden,
            lambda *a, **k: pytest.fail(f"{forbidden} must not be called"),
        )
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok
    assert "保持不变" in res.message


def test_partial_failure_is_reported_rather_than_claimed_as_success():
    """Saying "done" while an address is still attached would send the operator
    away believing the instance is off IPv6 when it is not."""
    s, net = _session(["2603:c020::1", "2603:c020::2"], fail_on={"2603:c020::2"})
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok is False
    assert res.data["removed"] == ["2603:c020::1"]
    assert res.data["failed"]
    assert "2603:c020::2" in res.message


def test_total_failure_reports_nothing_removed():
    s, net = _session(["2603:c020::1"], fail_on={"2603:c020::1"})
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok is False
    assert res.data["removed"] == []
    assert net.deleted == []


def test_missing_vnic_is_an_error_not_a_silent_success():
    s, _ = _session(["2603:c020::1"], vnic="")
    res = s.remove_public_ipv6("i1", "c1")
    assert res.ok is False
    assert "VNIC" in res.message
