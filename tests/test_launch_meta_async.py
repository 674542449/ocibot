"""「加载配置」 runs in the background and is polled, not fetched in one request.

Building this metadata makes six paginated Oracle reads and, on a tenancy with no
network, creates a VCN and waits for it. How long that takes is a property of the
operator's Oracle account — it cannot be bounded from here. Cloudflare cuts any
single request at 100 seconds, so the page returned a gateway error and then
worked on the next click, because the first attempt kept running server-side and
filled the cache.

Parallelising the reads (0.4.74) helped and was still not enough. Staying under a
fixed ceiling is not something this code can promise, so the request stopped
being long instead: start, return at once, poll.
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

_TMP = tempfile.mkdtemp(prefix="ocibot_async_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "async-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "async-jwt-secret-0123456789abcdef")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("sqlalchemy")

from web.backend import launch_service as ls  # noqa: E402
from web.backend.db import init_db  # noqa: E402


class _Session:
    def __init__(self, *, delay=0.6, fail=False):
        self.tenant = SimpleNamespace(
            id="t1", region="ap-tokyo-1",
            tenancy_ocid="ocid1.tenancy.oc1..t", compartment_ocid="",
        )
        self._delay = delay
        self._fail = fail
        self.calls = 0
        self._lock = threading.Lock()

    def _slow(self):
        time.sleep(self._delay)
        if self._fail:
            raise RuntimeError("oracle unavailable")

    def list_compartments(self, *a, **k):
        with self._lock:
            self.calls += 1
        self._slow()
        return [{"id": "ocid1.compartment.oc1..c1", "name": "root"}]

    def list_availability_domains(self):
        self._slow()
        return ["AD-1"]

    def list_images(self, **k):
        self._slow()
        return [{"id": "ocid1.image.oc1..img", "display_name": "Ubuntu"}]

    def list_custom_images(self, **k):
        return []

    def list_shapes(self, **k):
        self._slow()
        return [{"shape": "VM.Standard.A1.Flex"}]

    def ensure_default_network(self, **k):
        self._slow()
        return SimpleNamespace(
            ok=True, message="",
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
    ls._write_status(ls.meta_cache_key(_Session(), "t1"), "")
    yield
    ls.clear_launch_meta_cache()
    ls._write_status(ls.meta_cache_key(_Session(), "t1"), "")


def _await_state(session, tenant="t1", want=("ready", "error"), timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = ls.launch_meta_state(session, tenant)
        if state["state"] in want:
            return state
        time.sleep(0.05)
    raise AssertionError(f"never reached {want}; last={ls.launch_meta_state(session, tenant)}")


def test_start_returns_immediately_and_does_not_block():
    """The whole point: the HTTP request must not wait for Oracle."""
    s = _Session(delay=1.0)
    started = time.monotonic()
    res = ls.start_meta_refresh(s, "t1")
    elapsed = time.monotonic() - started
    assert res["state"] == "running"
    assert elapsed < 0.3, f"start blocked for {elapsed:.2f}s"
    _await_state(s)


def test_polling_eventually_reports_ready_with_the_metadata():
    s = _Session(delay=0.3)
    ls.start_meta_refresh(s, "t1")
    state = _await_state(s)
    assert state["state"] == "ready"
    assert state["meta"]["ads"] == ["AD-1"]
    assert state["meta"]["preferred_subnet_id"] == "sub1"


def test_a_second_click_joins_the_run_instead_of_starting_another():
    """Otherwise an impatient operator doubles the Oracle calls for one page."""
    s = _Session(delay=0.8)
    ls.start_meta_refresh(s, "t1")
    second = ls.start_meta_refresh(s, "t1")
    assert second["state"] == "running"
    _await_state(s)
    assert s.calls == 1, f"{s.calls} concurrent fetches were started"


def test_failure_is_reported_rather_than_polled_forever():
    s = _Session(delay=0.1, fail=True)
    ls.start_meta_refresh(s, "t1")
    state = _await_state(s)
    assert state["state"] == "error"
    assert state["error"]


def test_polling_never_calls_oracle():
    """A page left open must not spend the tenancy's rate limit."""
    s = _Session(delay=0.2)
    ls.start_meta_refresh(s, "t1")
    _await_state(s)
    before = s.calls
    for _ in range(5):
        assert ls.launch_meta_state(s, "t1")["state"] == "ready"
    assert s.calls == before, "the status endpoint hit Oracle"


def test_non_forced_start_returns_the_cached_answer_at_once():
    s = _Session(delay=0.2)
    ls.start_meta_refresh(s, "t1")
    _await_state(s)
    again = ls.start_meta_refresh(s, "t1", force=False)
    assert again["state"] == "ready"
    assert again["meta"]["ads"] == ["AD-1"]


def test_state_is_idle_before_anything_has_run():
    s = _Session()
    assert ls.launch_meta_state(s, "t1")["state"] == "idle"
