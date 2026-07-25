"""Unit tests for lean list refresh helpers (no live OCI)."""

from __future__ import annotations

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
