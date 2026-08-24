"""A failed read must never be reported as a factual zero.

Three places in app/oci_client.py used to answer an unreadable list with an empty
one. Each of these tests drives the REAL method (never a stub of the function
under test) with an OCI client that raises, and asserts the failure is visible in
what the caller gets back.
"""

from types import SimpleNamespace

import oci
import pytest
from oci.exceptions import ServiceError

from app.oci_client import OCIClientError, TenantSession


# ---------------------------------------------------------------------------
# Fakes


class FakeTenant:
    id = "t1"
    name = "tenant"
    region = "ap-tokyo-1"
    compartment_ocid = "ocid1.compartment..root"
    tenancy_ocid = "ocid1.tenancy..t"
    account_tier = "free"


def _instance(oid: str, comp: str, *, ocpus: float, memory: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=oid,
        display_name=oid,
        lifecycle_state="RUNNING",
        availability_domain="AD-1",
        fault_domain="FD-1",
        shape="VM.Standard.A1.Flex",
        shape_config=SimpleNamespace(ocpus=ocpus, memory_in_gbs=memory),
        time_created=None,
        compartment_id=comp,
        image_id="ocid1.image..i",
        freeform_tags={},
        defined_tags={},
    )


class DenyingIdentity:
    """ListCompartments refused — the IAM policy grants compute but not
    `inspect compartments in tenancy`, so OCI answers 404 forever."""

    def list_compartments(self, *args, **kwargs):
        raise ServiceError(404, "NotAuthorizedOrNotFound", {}, "Authorization failed")


class ChildOnlyCompute:
    """Every instance lives in a CHILD compartment; the root itself is empty."""

    def list_instances(self, compartment_id=None, **kwargs):
        if compartment_id == "ocid1.compartment..child":
            return SimpleNamespace(
                data=[_instance("ocid1.instance..a1", compartment_id, ocpus=4.0, memory=24.0)]
            )
        return SimpleNamespace(data=[])

    def list_boot_volume_attachments(self, *args, **kwargs):
        return SimpleNamespace(data=[])

    def list_volume_attachments(self, compartment_id, **kwargs):
        return SimpleNamespace(data=[])

    def get_instance(self, iid):
        return SimpleNamespace(data=SimpleNamespace(id=iid, display_name="inst"))


class ChildOnlyBlockstorage:
    def list_boot_volumes(self, compartment_id=None, **kwargs):
        if compartment_id == "ocid1.compartment..child":
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="ocid1.bootvolume..b1",
                        display_name="boot1",
                        size_in_gbs=100,
                        vpus_per_gb=10,
                        lifecycle_state="AVAILABLE",
                        availability_domain="AD-1",
                        compartment_id=compartment_id,
                        is_hydrated=True,
                        time_created=None,
                    )
                ]
            )
        return SimpleNamespace(data=[])

    def list_volumes(self, compartment_id=None, **kwargs):
        return SimpleNamespace(data=[])


def _session(*, identity, compute=None, blockstorage=None) -> TenantSession:
    s = TenantSession.__new__(TenantSession)
    s.tenant = FakeTenant()
    s._identity = identity
    s._compute = compute
    s._blockstorage = blockstorage
    s._network = None
    s._last_tree_errors = []
    return s


@pytest.fixture(autouse=True)
def _passthrough_pagination(monkeypatch):
    # Forward positionals too, so a wrong call signature raises instead of being
    # silently accepted.
    def fake_list_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(oci.pagination, "list_call_get_all_results", fake_list_call, raising=False)


# ---------------------------------------------------------------------------
# 1. Compartment enumeration failure must reach read_incomplete


def test_list_compartments_strict_raises_instead_of_degrading():
    s = _session(identity=DenyingIdentity())
    # Default stays lenient: a picker is still usable with just the root.
    assert [c["id"] for c in s.list_compartments()] == ["ocid1.tenancy..t"]
    with pytest.raises(OCIClientError):
        s.list_compartments(strict=True)


def test_instances_tree_records_failed_compartment_enumeration():
    s = _session(identity=DenyingIdentity(), compute=ChildOnlyCompute())
    items = s.list_instances_tree(resolve_ips=False)
    # Root scan succeeded and is genuinely empty, so no exception — but the
    # enumeration failure must be on the record the quota snapshot reads.
    assert items == []
    assert s._last_tree_errors, "a failed ListCompartments left no trace"


def test_boot_and_block_volume_reads_flag_failed_enumeration():
    s = _session(
        identity=DenyingIdentity(),
        compute=ChildOnlyCompute(),
        blockstorage=ChildOnlyBlockstorage(),
    )
    s.list_availability_domains = lambda: ["AD-1"]  # type: ignore[method-assign]

    bv = s.list_boot_volumes(include_subcompartments=True, include_attachments=False)
    assert (bv.data or {}).get("errors"), "boot volume read hid the enumeration failure"

    blk = s.list_block_volumes(include_subcompartments=True, include_attachments=False)
    assert (blk.data or {}).get("errors"), "block volume read hid the enumeration failure"


def test_quota_snapshot_is_incomplete_when_compartments_cannot_be_listed():
    """The whole point: 4 OCPU / 24 GB running in a child compartment are invisible
    because the subtree could not be enumerated. The snapshot may undercount, but it
    must not claim the undercount is authoritative — quota_guard fails closed on
    read_incomplete, and without it a free tenancy is let past its cap forever."""
    s = _session(
        identity=DenyingIdentity(),
        compute=ChildOnlyCompute(),
        blockstorage=ChildOnlyBlockstorage(),
    )
    s.list_availability_domains = lambda: ["AD-1"]  # type: ignore[method-assign]
    s.estimate_object_storage_usage = lambda **kw: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, message="", data={}
    )

    result = s.get_free_quota_usage(free_only_mode=True, include_block=True)
    snapshot = result.data or {}
    assert snapshot["buckets"]["a1_ocpu"]["used"] == 0.0  # the undercount itself
    assert snapshot["read_incomplete"] is True


def test_quota_snapshot_stays_complete_when_enumeration_succeeds():
    """Guard against over-triggering: a healthy tenancy must not fail closed."""

    class OkIdentity:
        def list_compartments(self, root, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="ocid1.compartment..child",
                        name="child",
                        description="",
                        lifecycle_state="ACTIVE",
                    )
                ]
            )

    s = _session(
        identity=OkIdentity(),
        compute=ChildOnlyCompute(),
        blockstorage=ChildOnlyBlockstorage(),
    )
    s.list_availability_domains = lambda: ["AD-1"]  # type: ignore[method-assign]
    s.estimate_object_storage_usage = lambda **kw: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, message="", data={}
    )

    snapshot = (s.get_free_quota_usage(free_only_mode=True, include_block=True).data) or {}
    assert snapshot["read_incomplete"] is False
    assert snapshot["buckets"]["a1_ocpu"]["used"] == 4.0


# ---------------------------------------------------------------------------
# 2. Console connections


class ThrottledConsoleCompute:
    def __init__(self):
        self.created = 0

    def list_instance_console_connections(self, compartment_id, **kwargs):
        raise ServiceError(429, "TooManyRequests", {}, "Rate limited")

    def create_instance_console_connection(self, details):  # pragma: no cover - must not run
        self.created += 1
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.consoleconn..c"))


def test_list_console_connections_raises_instead_of_reporting_none():
    compute = ThrottledConsoleCompute()
    s = _session(identity=DenyingIdentity(), compute=compute)
    with pytest.raises(OCIClientError):
        s.list_console_connections("ocid1.instance..i", "ocid1.compartment..root")


def test_create_console_connection_aborts_when_stale_ones_cannot_be_listed():
    """OCI permits one active console connection per instance. A throttled read made
    the "delete the stale one first" loop a silent no-op, and the create then failed
    with a message about an existing connection instead of about the throttle."""
    compute = ThrottledConsoleCompute()
    s = _session(identity=DenyingIdentity(), compute=compute)
    result = s.create_console_connection(
        "ocid1.instance..i", "ocid1.compartment..root", "ssh-ed25519 AAAAC3Nz key"
    )
    assert result.ok is False
    assert compute.created == 0, "created a connection without knowing the slot was free"


# ---------------------------------------------------------------------------
# 3. ensure_default_network must not build a second public stack blind


class NetworkWithUnreadableSubnets:
    def __init__(self, *, subnet_state="AVAILABLE", subnets_raise=True):
        self.subnet_state = subnet_state
        self.subnets_raise = subnets_raise
        self.created_vcns = 0
        self.created_subnets = 0
        self.created_igws = 0

    def list_vcns(self, compartment_id=None, **kwargs):
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="ocid1.vcn..v1",
                    display_name="existing-vcn",
                    cidr_block="10.0.0.0/16",
                    compartment_id="ocid1.compartment..root",
                    ipv6_cidr_blocks=[],
                    lifecycle_state="AVAILABLE",
                )
            ]
        )

    def list_subnets(self, compartment_id=None, vcn_id=None, **kwargs):
        if self.subnets_raise:
            raise ServiceError(429, "TooManyRequests", {}, "Rate limited")
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="ocid1.subnet..s1",
                    display_name="existing-subnet",
                    cidr_block="10.0.0.0/24",
                    vcn_id="ocid1.vcn..v1",
                    availability_domain="",
                    compartment_id="ocid1.compartment..root",
                    prohibit_public_ip_on_vnic=False,
                    prohibit_internet_ingress=False,
                    ipv6_cidr_block="",
                    ipv6_cidr_blocks=[],
                    security_list_ids=[],
                    lifecycle_state=self.subnet_state,
                )
            ]
        )

    def get_vcn(self, vcn_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=vcn_id,
                display_name="existing-vcn",
                cidr_block="10.0.0.0/16",
                cidr_blocks=["10.0.0.0/16"],
                compartment_id="ocid1.compartment..root",
                ipv6_cidr_blocks=[],
                lifecycle_state="AVAILABLE",
            )
        )

    # Everything below lets the create branch RUN to completion. A fake that
    # exploded on the first create would make these tests pass against the
    # unfixed code for the wrong reason (an exception, caught and reported as
    # ok=False) instead of proving nothing was provisioned.

    def create_vcn(self, details):
        self.created_vcns += 1
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.vcn..new"))

    def create_subnet(self, details):
        self.created_subnets += 1
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.subnet..new"))

    def get_subnet(self, subnet_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=subnet_id,
                display_name="new-subnet",
                cidr_block="10.0.0.0/24",
                vcn_id="ocid1.vcn..v1",
                availability_domain="",
                compartment_id="ocid1.compartment..root",
                prohibit_public_ip_on_vnic=False,
                lifecycle_state="AVAILABLE",
            )
        )

    def list_internet_gateways(self, compartment_id, **kwargs):
        return SimpleNamespace(data=[])

    def create_internet_gateway(self, details):
        self.created_igws += 1
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.igw..new"))

    def get_internet_gateway(self, igw_id):
        return SimpleNamespace(
            data=SimpleNamespace(id=igw_id, display_name="igw", lifecycle_state="AVAILABLE")
        )

    def list_route_tables(self, compartment_id, **kwargs):
        return SimpleNamespace(data=[])

    def create_route_table(self, details):
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.rt..new"))

    def list_security_lists(self, compartment_id, **kwargs):
        return SimpleNamespace(data=[])

    def create_security_list(self, details):
        return SimpleNamespace(data=SimpleNamespace(id="ocid1.sl..new"))


def _network_session(network) -> TenantSession:
    s = _session(identity=DenyingIdentity())
    s._network = network
    return s


def test_ensure_default_network_refuses_to_create_when_subnets_unreadable():
    """A throttled ListSubnets used to read as "this VCN has no subnets", and the
    create branch then provisioned a duplicate subnet + IGW + route table + open
    security list under the existing VCN — unrequested and not undoable from here."""
    net = NetworkWithUnreadableSubnets(subnets_raise=True)
    s = _network_session(net)
    result = s.ensure_default_network(
        compartment_id="ocid1.compartment..root", create_if_missing=True
    )
    assert result.ok is False
    assert net.created_subnets == 0 and net.created_igws == 0 and net.created_vcns == 0


def test_ensure_default_network_waits_out_a_provisioning_subnet():
    """list_subnets keeps AVAILABLE only, so a subnet still coming up looks like
    none at all. Report "retry shortly" rather than stack a second one on top."""
    net = NetworkWithUnreadableSubnets(subnets_raise=False, subnet_state="PROVISIONING")
    s = _network_session(net)
    result = s.ensure_default_network(
        compartment_id="ocid1.compartment..root", create_if_missing=True
    )
    assert result.ok is False
    assert net.created_subnets == 0 and net.created_igws == 0


def test_ensure_default_network_still_uses_a_healthy_existing_subnet():
    net = NetworkWithUnreadableSubnets(subnets_raise=False, subnet_state="AVAILABLE")
    s = _network_session(net)
    result = s.ensure_default_network(
        compartment_id="ocid1.compartment..root", create_if_missing=True
    )
    assert result.ok is True
    assert (result.data or {})["created"] is False
    assert (result.data or {})["subnet"]["id"] == "ocid1.subnet..s1"
