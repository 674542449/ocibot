"""Block volume list / create shape tests (fake OCI clients)."""

from types import SimpleNamespace

from app.oci_client import TenantSession


class FakeBlock:
    def __init__(self):
        self.created = None
        self.updated = None
        self.deleted = None
        self.volumes = [
            SimpleNamespace(
                id="ocid1.volume.oc1..v1",
                display_name="data1",
                size_in_gbs=50,
                vpus_per_gb=10,
                lifecycle_state="AVAILABLE",
                availability_domain="AD-1",
                compartment_id="ocid1.compartment..c",
                time_created=None,
            )
        ]

    def list_volumes(self, **kwargs):
        return SimpleNamespace(data=list(self.volumes))

    def create_volume(self, details):
        self.created = details
        vol = SimpleNamespace(
            id="ocid1.volume.oc1..new",
            display_name=getattr(details, "display_name", None) or "new",
            size_in_gbs=details.size_in_gbs,
            lifecycle_state="PROVISIONING",
            availability_domain=details.availability_domain,
        )
        return SimpleNamespace(data=vol)

    def get_volume(self, volume_id):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=volume_id,
                lifecycle_state="AVAILABLE",
                size_in_gbs=50,
                availability_domain="AD-1",
                compartment_id="ocid1.compartment..c",
            )
        )

    def update_volume(self, volume_id, details):
        self.updated = (volume_id, details)
        return SimpleNamespace(data=SimpleNamespace(id=volume_id))

    def delete_volume(self, volume_id):
        self.deleted = volume_id


class FakeCompute:
    def __init__(self):
        self.attachments = []
        self.calls = []

    # Signature mirrors oci.core.ComputeClient.list_volume_attachments exactly:
    # ONE positional (compartment_id), everything else keyword. The previous fake
    # declared (self, ad, cid, ...), which accepted the app's buggy positional
    # call and hid the TypeError that made every volume look unattached.
    def list_volume_attachments(self, compartment_id, **kwargs):
        self.calls.append((compartment_id, kwargs))
        return SimpleNamespace(data=list(self.attachments))

    def get_instance(self, iid):
        return SimpleNamespace(
            data=SimpleNamespace(
                id=iid,
                compartment_id="ocid1.compartment..c",
                availability_domain="AD-1",
                display_name="inst",
            )
        )

    def attach_volume(self, details):
        return SimpleNamespace(
            data=SimpleNamespace(id="ocid1.volattach..a", lifecycle_state="ATTACHING")
        )

    def detach_volume(self, attachment_id):
        return None


class FakeTenant:
    compartment_ocid = "ocid1.compartment..c"
    tenancy_ocid = "ocid1.tenancy..t"
    account_tier = "free"


def _session():
    s = TenantSession.__new__(TenantSession)
    s.tenant = FakeTenant()
    s._blockstorage = FakeBlock()
    s._compute = FakeCompute()
    s.list_availability_domains = lambda: ["AD-1"]  # type: ignore
    s.list_compartments = lambda parent_id=None, subtree=True: [{"id": "ocid1.compartment..c"}]  # type: ignore
    s.resolve_compartment = lambda: "ocid1.compartment..c"  # type: ignore
    return s


def test_list_block_volumes_shape(monkeypatch):
    s = _session()

    # pagination helper just calls the function
    import oci

    # Forward positionals too, so a wrong call signature raises instead of being
    # silently accepted by a permissive fake.
    def fake_list_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(oci.pagination, "list_call_get_all_results", fake_list_call, raising=False)

    result = s.list_block_volumes(include_subcompartments=False, include_attachments=True)
    assert result.ok, result.message
    vols = result.data["volumes"]
    assert len(vols) == 1
    assert vols[0]["id"] == "ocid1.volume.oc1..v1"
    assert vols[0]["kind"] == "block"
    assert vols[0]["size_in_gbs"] == 50
    assert result.data["summary"]["count"] == 1


def test_attached_block_volume_is_reported_as_attached(monkeypatch):
    """An ATTACHED attachment must surface, or the UI can never offer 卸载.

    The volume-attachment lookup was passing availability_domain positionally,
    which raised TypeError into a bare `except Exception: atts = []`, so every
    block volume rendered as 未挂载 and the detach button (v-if="v.attachment_id")
    could not appear.
    """
    s = _session()
    s.compute.attachments = [
        SimpleNamespace(
            id="ocid1.volumeattachment.oc1..a1",
            volume_id="ocid1.volume.oc1..v1",
            instance_id="ocid1.instance.oc1..i1",
            lifecycle_state="ATTACHED",
            availability_domain="AD-1",
        )
    ]

    import oci

    def fake_list_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(oci.pagination, "list_call_get_all_results", fake_list_call, raising=False)

    result = s.list_block_volumes(include_subcompartments=False, include_attachments=True)
    assert result.ok, result.message
    vol = result.data["volumes"][0]
    assert vol["instance_id"] == "ocid1.instance.oc1..i1", vol
    assert vol["attachment_id"] == "ocid1.volumeattachment.oc1..a1", vol
    assert vol["attachment_state"] == "ATTACHED", vol
    assert result.data["summary"]["attached"] == 1, result.data["summary"]
    # The AD must have been sent as a keyword, matching the real SDK signature.
    assert s.compute.calls and "availability_domain" in s.compute.calls[0][1]


def test_create_block_volume():
    s = _session()
    result = s.create_block_volume(
        compartment_id="ocid1.compartment..c",
        availability_domain="AD-1",
        size_in_gbs=50,
        display_name="x",
    )
    assert result.ok, result.message
    assert s._blockstorage.created is not None
    assert result.data["id"] == "ocid1.volume.oc1..new"


def test_create_block_volume_rejects_small():
    s = _session()
    result = s.create_block_volume(
        compartment_id="c",
        availability_domain="AD-1",
        size_in_gbs=10,
    )
    assert not result.ok
