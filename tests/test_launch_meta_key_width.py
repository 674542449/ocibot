"""The launch cache's app_meta keys must fit the column, or nothing is stored.

`app_meta.key` is VARCHAR(64). SQLite ignores that width; PostgreSQL — what
production runs — rejects a longer value outright. The keys were built as
``launch_meta_status:<tenant uuid>|<region>|<tenancy OCID>``, about 146
characters, so on the real database EVERY write failed, and both writers swallow
their errors because a cache must never break a request.

Two shipped features were therefore inert:

* 0.4.73's cross-worker meta cache stored nothing, so a cold fetch could still
  land inside a 创建 request (the intermittent 520).
* 0.4.75's polling state never persisted. 「加载配置」 started the refresh and
  answered "running"; the poll two seconds later found no status row and no
  result, read that as "never started", and the page reported
  加载未完成，请重试 — while the refresh it had just started was running normally
  and finished a minute later. That is the reported "first click always fails,
  a later click works" behaviour: the later click found the in-process cache the
  first run had filled.

Every existing test passed because they run on SQLite with a stub tenancy OCID a
dozen characters long. So the checks here do two things the old tests could not:
assert the composed keys fit the declared column width, and drive the real flow
with PostgreSQL's width enforcement emulated as an ORM guard.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_keyw_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'k.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "keyw-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "keyw-jwt-secret-0123456789abcdef")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, select  # noqa: E402

from web.backend import launch_service as ls  # noqa: E402
from web.backend.db import init_db  # noqa: E402
from web.backend.models import AppMeta  # noqa: E402

# Realistic values, not the stubs the other launch tests use: a uuid tenant id, a
# region, and a full-length tenancy OCID. Together ~127 characters.
TENANT_ID = "0f9a2c1e-7b64-4d2a-9c3f-8e1b5a6d7c40"
TENANCY = "ocid1.tenancy.oc1..aaaaaaaaba3pv6wkcr4jqae5f44n2b2m2yt2j6rx32uzr4h25vqstifsfdsq"
KEY_WIDTH = AppMeta.__table__.c.key.type.length


class _Session:
    """The surface fetch_launch_meta touches, with a realistic tenancy OCID."""

    def __init__(self, delay: float = 0.0):
        self.tenant = SimpleNamespace(
            id=TENANT_ID,
            region="ap-tokyo-1",
            tenancy_ocid=TENANCY,
            compartment_ocid="",
        )
        self._delay = delay

    def list_compartments(self, *a, **k):
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
        # A first-time tenancy creates a VCN here; the delay stands in for that,
        # so the poll really is asked while the refresh is still running.
        time.sleep(self._delay)
        return SimpleNamespace(
            ok=True,
            message="",
            data={
                "vcn": {"id": "vcn1"},
                "subnet": {"id": "sub1"},
                "vcns": [{"id": "vcn1", "display_name": "vcn"}],
                "subnets_by_vcn": {"vcn1": [{"id": "sub1", "vcn_id": "vcn1"}]},
                "created": True,
            },
        )


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    ls.clear_launch_meta_cache(TENANT_ID)
    yield
    ls.clear_launch_meta_cache(TENANT_ID)


@pytest.fixture
def pg_width_enforced():
    """Emulate PostgreSQL: reject an over-long app_meta.key instead of ignoring it.

    Without this, every assertion below passes on SQLite whether the keys fit or
    not — which is exactly how the production-only failure got shipped twice.
    """

    def _guard(mapper, connection, target):
        if len(target.key or "") > KEY_WIDTH:
            raise ValueError(
                f"value too long for type character varying({KEY_WIDTH}): "
                f"{len(target.key)} characters"
            )

    event.listen(AppMeta, "before_insert", _guard)
    event.listen(AppMeta, "before_update", _guard)
    yield
    event.remove(AppMeta, "before_insert", _guard)
    event.remove(AppMeta, "before_update", _guard)


def _rows(prefix: str) -> list[str]:
    from web.backend.db import SessionLocal

    with SessionLocal() as db:
        return list(db.scalars(select(AppMeta.key).where(AppMeta.key.like(f"{prefix}%"))))


def test_the_module_budget_matches_the_actual_column():
    """_row_key sizes itself from _KEY_WIDTH; if the column ever changes, so must it."""
    assert ls._KEY_WIDTH == KEY_WIDTH


def test_both_row_keys_fit_the_column_for_a_real_tenancy():
    cache_key = ls.meta_cache_key(_Session(), TENANT_ID)
    assert len(cache_key) > KEY_WIDTH, "the raw key is what used to be stored"
    for prefix in (ls._SHARED_META_PREFIX, ls._STATUS_PREFIX):
        key = ls._row_key(prefix, cache_key)
        assert len(key) <= KEY_WIDTH, f"{key!r} is {len(key)} chars, column is {KEY_WIDTH}"


def test_key_stays_within_the_column_even_for_absurd_input():
    """A bound that depends on the caller's data is not a bound."""
    key = ls._row_key(ls._STATUS_PREFIX, f"{'t' * 400}|{'r' * 400}|{'o' * 400}")
    assert len(key) <= KEY_WIDTH


def test_key_still_starts_with_the_tenant_id_so_clearing_can_target_it():
    key = ls._row_key(ls._SHARED_META_PREFIX, ls.meta_cache_key(_Session(), TENANT_ID))
    assert key.startswith(f"{ls._SHARED_META_PREFIX}{TENANT_ID}|")


def test_key_distinguishes_regions_of_the_same_tenant():
    """Otherwise a 副区 row would read the home region's network and images."""
    a = _Session()
    b = _Session()
    b.tenant.region = "ap-osaka-1"
    assert ls._row_key(ls._STATUS_PREFIX, ls.meta_cache_key(a, TENANT_ID)) != ls._row_key(
        ls._STATUS_PREFIX, ls.meta_cache_key(b, TENANT_ID)
    )


def test_status_round_trips_under_the_column_limit(pg_width_enforced):
    cache_key = ls.meta_cache_key(_Session(), TENANT_ID)
    ls._write_status(cache_key, "running")
    assert ls._read_status(cache_key).get("state") == "running", "status row was not stored"
    ls._write_status(cache_key, "")
    assert ls._read_status(cache_key) == {}


def test_shared_meta_round_trips_under_the_column_limit(pg_width_enforced):
    s = _Session()
    ls.clear_launch_meta_cache(TENANT_ID)
    ls.fetch_launch_meta(s, tenant_id=TENANT_ID, force=True)
    ls._META_CACHE.clear()  # what a second worker process sees
    shared = ls._load_shared_meta(ls.meta_cache_key(s, TENANT_ID))
    assert shared is not None, "the cross-worker cache stored nothing"
    assert shared[1]["ads"] == ["AD-1"]


def test_the_poll_reports_running_not_idle_while_the_refresh_runs(pg_width_enforced):
    """The reported bug, end to end.

    A poll that answers 'idle' while the refresh is running is what the page turns
    into 加载未完成，请重试 on the very first click.
    """
    s = _Session(delay=1.5)
    assert ls.start_meta_refresh(s, TENANT_ID)["state"] == "running"
    time.sleep(0.3)
    assert ls.launch_meta_state(s, TENANT_ID)["state"] == "running"

    local = dict(ls._META_CACHE)
    ls._META_CACHE.clear()  # the poll landing on the other API worker
    assert ls.launch_meta_state(s, TENANT_ID)["state"] == "running"
    ls._META_CACHE.update(local)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ls.launch_meta_state(s, TENANT_ID)["state"] == "ready":
            break
        time.sleep(0.05)
    assert ls.launch_meta_state(s, TENANT_ID)["state"] == "ready"

    ls._META_CACHE.clear()
    state = ls.launch_meta_state(s, TENANT_ID)
    assert state["state"] == "ready", "the other worker could not read the result"
    assert state["meta"]["preferred_subnet_id"] == "sub1"


def test_clearing_removes_both_the_document_and_the_status_row(pg_width_enforced):
    s = _Session()
    ls.fetch_launch_meta(s, tenant_id=TENANT_ID, force=True)
    ls._write_status(ls.meta_cache_key(s, TENANT_ID), "error", "boom")
    assert _rows(ls._SHARED_META_PREFIX) and _rows(ls._STATUS_PREFIX)

    ls.clear_launch_meta_cache(TENANT_ID)

    assert not _rows(ls._SHARED_META_PREFIX), "stale document survived the clear"
    assert not _rows(ls._STATUS_PREFIX), "stale status survived the clear"


def test_clearing_also_sweeps_the_pre_0_4_77_key_format():
    """Those rows only exist on SQLite installs and nothing reads them now."""
    from web.backend.db import SessionLocal

    legacy = f"launch_meta:{TENANT_ID}|ap-tokyo-1|{TENANCY}"
    with SessionLocal() as db:
        db.add(AppMeta(key=legacy, value="{}"))
        db.commit()
    ls.clear_launch_meta_cache(TENANT_ID)
    assert not _rows("launch_meta:")
