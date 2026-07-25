from types import SimpleNamespace

from app.oci_client import TenantSession


class FakeCompute:
    def __init__(self, ad="AD-1", bv_id="ocid1.bootvolume.oc1..bv"):
        self._ad = ad
        self._bv_id = bv_id

    def get_instance(self, _iid):
        return SimpleNamespace(data=SimpleNamespace(availability_domain=self._ad))

    def list_boot_volume_attachments(self, ad, compartment_id, instance_id=None):
        assert ad == self._ad
        return SimpleNamespace(data=[SimpleNamespace(boot_volume_id=self._bv_id)])


class FakeBlockstorage:
    def __init__(self, state="AVAILABLE", size=50, vpu=10):
        self.updated = None
        self._state = state
        self._size = size
        self._vpu = vpu

    def get_boot_volume(self, _bv_id):
        return SimpleNamespace(data=SimpleNamespace(
            lifecycle_state=self._state, size_in_gbs=self._size, vpus_per_gb=self._vpu, display_name="bv"
        ))

    def update_boot_volume(self, bv_id, details):
        self.updated = (bv_id, details)
        return SimpleNamespace(data=SimpleNamespace(id=bv_id))


def _session():
    s = TenantSession.__new__(TenantSession)
    s._compute = FakeCompute()
    s._blockstorage = FakeBlockstorage()
    return s


def test_resize_boot_volume_applies_vpu():
    s = _session()
    result = s.resize_boot_volume("ocid1.instance..x", "ocid1.compartment..c", vpus_per_gb=120, wait_for_volume=False)
    assert result.ok, result.message
    bv_id, details = s._blockstorage.updated
    assert bv_id == "ocid1.bootvolume.oc1..bv"
    assert details.vpus_per_gb == 120


def test_resize_boot_volume_applies_size_and_vpu():
    s = _session()
    result = s.resize_boot_volume("i", "c", size_in_gbs=200, vpus_per_gb=120, wait_for_volume=False)
    assert result.ok
    _bv, details = s._blockstorage.updated
    assert details.size_in_gbs == 200
    assert details.vpus_per_gb == 120


def test_resize_rejects_bad_vpu():
    s = _session()
    result = s.resize_boot_volume("i", "c", vpus_per_gb=25, wait_for_volume=False)
    assert not result.ok
    assert s._blockstorage.updated is None


def test_resize_requires_something_to_change():
    s = _session()
    result = s.resize_boot_volume("i", "c", wait_for_volume=False)
    assert not result.ok


def test_get_boot_volume_info():
    s = TenantSession.__new__(TenantSession)
    s._compute = FakeCompute()
    s._blockstorage = FakeBlockstorage(size=100, vpu=120)
    result = s.get_boot_volume_info("i", "c")
    assert result.ok
    assert result.data["size_in_gbs"] == 100
    assert result.data["vpus_per_gb"] == 120


def test_resolve_boot_volumes_parallel_fills_instance_info():
    from app.oci_client import InstanceInfo

    s = TenantSession.__new__(TenantSession)
    s._compute = FakeCompute()
    s._blockstorage = FakeBlockstorage(size=100, vpu=120)
    inst = InstanceInfo(
        id="i", display_name="n", lifecycle_state="RUNNING", region="r",
        availability_domain="AD-1", fault_domain="", shape="VM.Standard.A1.Flex",
        ocpus=1, memory_gb=6, time_created="", compartment_id="c", image_id="",
        freeform_tags={}, defined_tags={}, tenant_id="t", tenant_name="T",
    )
    s._resolve_boot_volumes_parallel([inst])
    assert inst.boot_volume_gb == 100
    assert inst.boot_vpus_per_gb == 120
    assert inst.disk_text() == "100 G"
    assert inst.disk_perf_text() == "120 超高"


class HydratingBlockstorage(FakeBlockstorage):
    """Volume reports not-hydrated for the first N polls, and update_boot_volume
    raises 409 hydrating conflicts for the first M attempts."""

    def __init__(self, polls_until_hydrated=2, conflicts=1):
        super().__init__()
        self._polls_left = polls_until_hydrated
        self._conflicts_left = conflicts
        self.update_attempts = 0

    def get_boot_volume(self, bv_id):
        hydrated = self._polls_left <= 0
        self._polls_left -= 1
        return SimpleNamespace(data=SimpleNamespace(
            lifecycle_state="AVAILABLE", size_in_gbs=50, vpus_per_gb=10,
            display_name="bv", is_hydrated=hydrated,
        ))

    def update_boot_volume(self, bv_id, details):
        self.update_attempts += 1
        if self._conflicts_left > 0:
            self._conflicts_left -= 1
            from app.oci_client import ServiceError

            raise ServiceError(409, "Conflict", {}, "Volume vpus may not be updated while hydrating.")
        self.updated = (bv_id, details)
        return SimpleNamespace(data=SimpleNamespace(id=bv_id))


def test_vpu_update_waits_for_hydration_and_retries_409(monkeypatch):
    import app.oci_client as oc

    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)
    s = TenantSession.__new__(TenantSession)
    s._compute = FakeCompute()
    s._blockstorage = HydratingBlockstorage(polls_until_hydrated=2, conflicts=1)
    result = s.resize_boot_volume("i", "c", vpus_per_gb=120, wait_for_volume=False)
    assert result.ok, result.message
    assert s._blockstorage.update_attempts == 2  # first 409, then success
    _bv, details = s._blockstorage.updated
    assert details.vpus_per_gb == 120


def test_vpu_update_gives_clear_message_when_hydration_never_finishes(monkeypatch):
    import app.oci_client as oc

    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)
    s = TenantSession.__new__(TenantSession)
    s._compute = FakeCompute()
    s._blockstorage = HydratingBlockstorage(polls_until_hydrated=10**9, conflicts=10**9)
    result = s.resize_boot_volume("i", "c", vpus_per_gb=120, wait_for_volume=False, hydration_timeout=0)
    assert not result.ok
    assert "hydrating" in result.message
