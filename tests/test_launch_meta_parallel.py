"""Building the launch metadata runs its independent OCI reads concurrently.

「加载配置」 makes six independent round trips — compartments, ADs, shapes, custom
images, one image list per OS family, and the default-network scan. Serially the
wall time is their SUM, and each is paginated; ensure_default_network also walks
VCNs and subnets per compartment. On a tenancy of any size that ran past the
100-second ceiling a proxy puts on a single request, which is why 加载配置
returned a gateway error and then worked on the second click: the first attempt
kept running server-side and filled the cache.

This does not reduce the number of OCI calls — the same requests are made, so it
costs no extra rate limit. It only stops them queueing behind each other.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_par_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'p.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "par-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "par-jwt-secret-0123456789abcdef")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("sqlalchemy")

from app.oci_client import OCIClientError  # noqa: E402
from web.backend import launch_service as ls  # noqa: E402
from web.backend.db import init_db  # noqa: E402

_DELAY = 0.25


class _Session:
    """Every OCI read sleeps, so serial and parallel are clearly distinguishable."""

    def __init__(self, *, network_ok=True, failing=None):
        self.tenant = SimpleNamespace(
            id="t1", region="ap-tokyo-1",
            tenancy_ocid="ocid1.tenancy.oc1..t", compartment_ocid="",
        )
        self._network_ok = network_ok
        self._failing = failing or set()
        self._lock = threading.Lock()
        self.peak_concurrency = 0
        self._active = 0

    def _work(self, name):
        with self._lock:
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)
        try:
            time.sleep(_DELAY)
            if name in self._failing:
                raise RuntimeError(f"{name} exploded")
        finally:
            with self._lock:
                self._active -= 1

    def list_compartments(self, *a, **k):
        self._work("compartments")
        return [{"id": "ocid1.compartment.oc1..c1", "name": "root"}]

    # 现在按 compartment 取：可用域是租户级的，但**权限是按 compartment 判的**，
    # 写死 tenancy 根会让只授权到子 compartment 的密钥直接 404。
    def list_availability_domains(self, compartment_id=None):
        self._work("ads")
        return ["AD-1"]

    def list_images(self, **k):
        # Two distinct call shapes: the per-family lookup passes
        # operating_system, the fallback afterwards does not. Only the former was
        # ever wrapped in a try/except, so they need separate failure switches.
        self._work("images_family" if k.get("operating_system") else "images_fallback")
        return [{"id": "ocid1.image.oc1..img", "display_name": "Ubuntu 24.04"}]

    def list_custom_images(self, **k):
        self._work("custom")
        return []

    def list_shapes(self, **k):
        self._work("shapes")
        return [{"shape": "VM.Standard.A1.Flex"}]

    def ensure_default_network(self, **k):
        self._work("network")
        return SimpleNamespace(
            ok=self._network_ok,
            message="" if self._network_ok else "无法准备默认网络",
            data={
                "vcn": {"id": "vcn1"}, "subnet": {"id": "sub1"},
                "vcns": [{"id": "vcn1", "display_name": "vcn"}],
                "subnets_by_vcn": {"vcn1": [{"id": "sub1", "vcn_id": "vcn1"}]},
                "created": False,
            },
        )


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    ls.clear_launch_meta_cache()
    yield
    ls.clear_launch_meta_cache()


def test_reads_actually_overlap():
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1", force=True)
    assert s.peak_concurrency > 1, "the reads still ran one at a time"


def test_wall_time_is_far_below_the_serial_sum():
    """Six-plus sleeps of _DELAY each. Serial would be >= 7x; parallel should be
    near one. The bound is loose so this does not turn flaky on a busy machine,
    while still failing outright if the concurrency is removed."""
    s = _Session()
    started = time.monotonic()
    ls.fetch_launch_meta(s, tenant_id="t1", force=True)
    elapsed = time.monotonic() - started
    serial = 7 * _DELAY
    assert elapsed < serial * 0.6, f"{elapsed:.2f}s is close to the serial {serial:.2f}s"


def test_meta_content_is_unchanged():
    s = _Session()
    meta = ls.fetch_launch_meta(s, tenant_id="t1", force=True)
    assert meta["ads"] == ["AD-1"]
    assert meta["compartments"][0]["id"] == "ocid1.compartment.oc1..c1"
    assert meta["preferred_vcn_id"] == "vcn1"
    assert meta["preferred_subnet_id"] == "sub1"
    assert meta["images_by_os"]["ubuntu"], "ubuntu family missing"
    assert "custom" in meta["images_by_os"]
    assert any(sh["shape"] == "VM.Standard.A1.Flex" for sh in meta["all_shapes"])


def test_a_failing_image_family_is_still_tolerated():
    """The per-family lookup was individually wrapped before; moving it into a
    thread must not turn a tolerated failure into a 502 for the whole page.
    The unwrapped fallback then supplies the list, exactly as it did before."""
    s = _Session(failing={"images_family"})
    meta = ls.fetch_launch_meta(s, tenant_id="t1", force=True)
    assert meta["images_by_os"]["ubuntu"], "the fallback should have filled this"
    assert meta["images_by_os"]["oracle_linux"] == []


def test_both_image_paths_failing_still_propagates():
    """Unchanged from before: the fallback was never wrapped, so if it fails too
    the page reports an error rather than offering an empty image list."""
    s = _Session(failing={"images_family", "images_fallback"})
    with pytest.raises(RuntimeError):
        ls.fetch_launch_meta(s, tenant_id="t1", force=True)


def test_a_failing_compartment_read_still_propagates():
    """This one was NOT wrapped before. Futures swallow exceptions until
    .result() is called, so it would be easy to accidentally start ignoring it."""
    s = _Session(failing={"compartments"})
    with pytest.raises(RuntimeError):
        ls.fetch_launch_meta(s, tenant_id="t1", force=True)


def test_network_failure_still_raises_oci_client_error():
    s = _Session(network_ok=False)
    with pytest.raises(OCIClientError):
        ls.fetch_launch_meta(s, tenant_id="t1", force=True)
