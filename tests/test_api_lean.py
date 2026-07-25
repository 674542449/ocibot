"""Unit tests for lean list refresh / launch-meta helpers (no live OCI)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.oci_client import InstanceInfo, TenantSession


def _inst(**kwargs) -> InstanceInfo:
    base = dict(
        id="ocid1.instance.oc1..x",
        display_name="web",
        lifecycle_state="RUNNING",
        region="ap-tokyo-1",
        availability_domain="AD-1",
        fault_domain="FD-1",
        shape="VM.Standard.A1.Flex",
        ocpus=1.0,
        memory_gb=6.0,
        time_created="2024-01-01T00:00:00+00:00",
        compartment_id="ocid1.compartment.oc1..c",
        image_id="ocid1.image.oc1..i",
        freeform_tags={},
        defined_tags={},
        tenant_id="t1",
        tenant_name="t",
    )
    base.update(kwargs)
    return InstanceInfo(**base)


def test_enrich_instance_skips_terminated():
    session = TenantSession.__new__(TenantSession)
    session._enrich_instances_parallel = MagicMock()
    dead = _inst(lifecycle_state="TERMINATED")
    out = session.enrich_instance(dead)
    assert out is dead
    session._enrich_instances_parallel.assert_not_called()


def test_enrich_instance_calls_parallel_for_running():
    session = TenantSession.__new__(TenantSession)
    session.resolve_compartment = lambda: "root"
    session._enrich_instances_parallel = MagicMock()
    live = _inst(compartment_id="comp-a")
    out = session.enrich_instance(live)
    assert out is live
    session._enrich_instances_parallel.assert_called_once()
    args = session._enrich_instances_parallel.call_args[0]
    assert args[0] == [live]
    assert args[1] == "comp-a"


def test_instance_needs_enrichment_logic():
    def needs(inst: InstanceInfo, enriched: set[str]) -> bool:
        if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
            return False
        if inst.id in enriched:
            return False
        has_net = bool(inst.public_ip or inst.private_ip or inst.ipv6_addresses)
        has_disk = inst.boot_volume_gb is not None
        return not (has_net and has_disk)

    bare = _inst()
    assert needs(bare, set()) is True
    filled = _inst(public_ip="1.2.3.4", private_ip="10.0.0.2", boot_volume_gb=50)
    assert needs(filled, set()) is False
    assert needs(bare, {bare.id}) is False
    assert needs(_inst(lifecycle_state="TERMINATING"), set()) is False


def test_fetch_launch_meta_does_not_scan_compartments():
    """_fetch_launch_meta must not list VCNs across the tenancy tree."""
    from app.gui import OCIBotApp

    calls = {"list_vcns": 0, "list_subnets": 0}

    class FakeSession:
        def list_compartments(self):
            return [{"id": "c1", "name": "a"}, {"id": "c2", "name": "b"}]

        def list_availability_domains(self):
            return ["AD-1"]

        def list_images(self, **_kw):
            return [{"id": "img", "label": "Ubuntu"}]

        def list_shapes(self, **_kw):
            return [{"shape": "VM.Standard.A1.Flex", "label": "VM.Standard.A1.Flex"}]

        def ensure_default_network(self, **_kw):
            return SimpleNamespace(
                ok=True,
                message="using default",
                data={
                    "created": False,
                    "vcns": [{"id": "vcn1", "label": "vcn"}],
                    "subnets_by_vcn": {"vcn1": [{"id": "sub1", "label": "sub"}]},
                    "vcn": {"id": "vcn1"},
                    "subnet": {"id": "sub1"},
                },
            )

        def list_vcns(self, *_a, **_k):
            calls["list_vcns"] += 1
            return []

        def list_subnets(self, *_a, **_k):
            calls["list_subnets"] += 1
            return []

    app = object.__new__(OCIBotApp)
    app.sessions = SimpleNamespace(get=lambda _t: FakeSession())
    tenant = SimpleNamespace(
        id="tid",
        name="T",
        region="ap-tokyo-1",
        tenancy_ocid="ocid1.tenancy.oc1..t",
        compartment_ocid="",
    )
    meta = OCIBotApp._fetch_launch_meta(app, tenant)
    assert meta["ads"] == ["AD-1"]
    assert meta["preferred_vcn_id"] == "vcn1"
    assert calls["list_vcns"] == 0
    assert calls["list_subnets"] == 0
