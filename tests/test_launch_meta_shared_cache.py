"""Launch metadata is cached across API worker PROCESSES, not just within one.

A cold `fetch_launch_meta` lists images for every OS family and, on a tenancy
with no network, creates a VCN + subnet + gateway + route table and waits for
each. A minute or more is normal.

The cache was a module-level dict, i.e. per process, while the API runs
OCIBOT_API_WORKERS=2. So 加载配置 warmed one worker and the 创建 that followed had
an even chance of landing on the other, where the whole cold fetch happened
*inside the launch request* and overran the proxy timeout. That is the
intermittent 520 that "went away on retry" — by then some worker was warm.

A second process is simulated by clearing only the in-process dict, which is
exactly what a fresh worker sees.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_meta_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'm.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "meta-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "meta-jwt-secret-0123456789abcdef")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("sqlalchemy")

from web.backend.db import init_db  # noqa: E402
from web.backend import launch_service as ls  # noqa: E402


class _Session:
    """Counts how many times the expensive path actually runs."""

    def __init__(self):
        self.calls = 0
        self.tenant = SimpleNamespace(
            id="t1", region="ap-tokyo-1",
            tenancy_ocid="ocid1.tenancy.oc1..t", compartment_ocid="",
        )

    # --- the expensive surface fetch_launch_meta touches ---
    def list_compartments(self, *a, **k):
        self.calls += 1
        return [{"id": "ocid1.compartment.oc1..c1", "name": "root"}]

    # 现在按 compartment 取：可用域是租户级的，但**权限是按 compartment 判的**，
    # 写死 tenancy 根会让只授权到子 compartment 的密钥直接 404。
    def list_availability_domains(self, compartment_id=None):
        return ["AD-1"]

    def list_images(self, **k):
        return [{"id": "ocid1.image.oc1..img", "display_name": "Ubuntu"}]

    def list_custom_images(self, **k):
        return []

    def list_shapes(self, **k):
        return [{"shape": "VM.Standard.A1.Flex"}]

    def ensure_default_network(self, **k):
        # The genuinely slow one in real life: creates VCN/subnet/IGW and waits.
        return SimpleNamespace(
            ok=True, message="",
            data={
                "vcn": {"id": "vcn1"},
                "subnet": {"id": "sub1"},
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


def _simulate_other_worker():
    """A different process has an empty dict but sees the same database."""
    ls._META_CACHE.clear()


def test_cold_fetch_then_same_process_is_cached():
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    assert s.calls == 1
    meta = ls.fetch_launch_meta(s, tenant_id="t1")
    assert s.calls == 1
    assert meta["cached"] is True


def test_a_second_worker_process_does_not_repeat_the_cold_fetch():
    """The actual fix. Before it, this second call did the full minute of work
    inside whatever request happened to land on the cold worker."""
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    assert s.calls == 1

    _simulate_other_worker()
    meta = ls.fetch_launch_meta(s, tenant_id="t1")

    assert s.calls == 1, "second worker repeated the expensive fetch"
    assert meta["cached"] is True
    assert meta["preferred_subnet_id"] == "sub1"


def test_shared_hit_is_promoted_into_the_local_dict():
    """Otherwise every request on that worker keeps paying a database round trip."""
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    _simulate_other_worker()
    ls.fetch_launch_meta(s, tenant_id="t1")
    assert ls._META_CACHE, "shared hit was not promoted"


def test_force_bypasses_both_levels():
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    ls.fetch_launch_meta(s, tenant_id="t1", force=True)
    assert s.calls == 2


def test_clearing_removes_the_shared_copy_too():
    """Clearing only the dict would be undone by the next read promoting the
    stale document straight back — a tenant that changed region would keep
    serving the old network until the TTL expired."""
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    ls.clear_launch_meta_cache("t1")
    _simulate_other_worker()
    ls.fetch_launch_meta(s, tenant_id="t1")
    assert s.calls == 2, "stale shared entry survived the clear"


def test_expired_shared_entry_is_ignored(monkeypatch):
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id="t1")
    _simulate_other_worker()
    monkeypatch.setattr(ls, "_META_TTL", 0)
    ls.fetch_launch_meta(s, tenant_id="t1")
    assert s.calls == 2


def test_cache_write_does_not_join_the_callers_transaction():
    """The helpers must use their own session. Borrowing the request's and
    committing on it silently commits whatever else that request had pending —
    the first version of this did exactly that and cost an unrelated test a row.
    """
    src = Path(ls.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _store_shared_meta") : src.index("_META_MAX_ENTRIES")]
    assert "_cache_session()" in body
    assert "db: Any" not in body, "must not accept the caller's session"
