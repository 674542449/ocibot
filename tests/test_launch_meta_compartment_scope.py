"""Launch metadata must be read from the tenant's configured compartment.

A user hit intermittent `[404] NotAuthorizedOrNotFound` on 加载配置. The cause:
`fetch_launch_meta` computed `default_comp = tenant.compartment_ocid or
tenant.tenancy_ocid` and then asked the **tenancy root** for images, shapes and
availability domains anyway. A key whose IAM policy only covers a child
compartment 404s on the root, and `list_availability_domains` was the first hard
raise inside the parallel block — so the whole page failed with an error about
nothing the operator had touched.

Two secondary effects made it worse and are also pinned here:

* `list_images` has a built-in "too few results in the child, retry at the root"
  fallback, guarded by `compartment != tenancy_ocid`. Passing the root as the
  compartment makes that condition false, disabling the one path designed to
  survive exactly this.
* The sequential fallbacks further down (`if not images: list_images(...)`,
  `if not shapes: list_shapes()`) use `resolve_compartment()` — the correct
  child — but they are unreachable, because the parallel `.result()` raises
  first.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from web.backend import launch_service  # noqa: E402

TENANCY = "ocid1.tenancy.oc1.." + "a" * 44
CHILD = "ocid1.compartment.oc1.." + "c" * 40


class _Tenant:
    def __init__(self, compartment_ocid: str):
        self.id = "t1"
        self.name = "t"
        self.tenancy_ocid = TENANCY
        self.compartment_ocid = compartment_ocid
        self.region = "ap-tokyo-1"


class _Session:
    """Records which compartment each read was asked for."""

    def __init__(self, compartment_ocid: str):
        # fetch_launch_meta reads the tenant off the session, not from a parameter.
        self.tenant = _Tenant(compartment_ocid)
        self.asked: dict[str, list] = {"images": [], "shapes": [], "ads": [], "custom": [], "net": []}

    def list_images(self, compartment_id=None, operating_system=None, ubuntu_only=False):
        self.asked["images"].append(compartment_id)
        return [{"id": "img1", "display_name": "Ubuntu", "operating_system": "Canonical Ubuntu"}]

    def list_shapes(self, compartment_id=None):
        self.asked["shapes"].append(compartment_id)
        return [{"shape": "VM.Standard.A1.Flex", "is_flexible": True}]

    def list_availability_domains(self, compartment_id=None):
        self.asked["ads"].append(compartment_id)
        return ["AD-1"]

    def list_custom_images(self, compartment_id=None):
        self.asked["custom"].append(compartment_id)
        return []

    def list_compartments(self):
        return [{"id": TENANCY, "name": "(根) Tenancy"}]

    def ensure_default_network(self, compartment_id=None, create_if_missing=True):
        self.asked["net"].append(compartment_id)

        class _R:
            ok = True
            message = ""
            data = {"vcns": [{"id": "vcn1"}], "subnets_by_vcn": {"vcn1": [{"id": "sub1"}]}}

        return _R()


# 刻意用一个本文件专属的 tenant_id。
#
# fetch_launch_meta 成功后会把结果写进**跨进程共享**的缓存（app_meta 行），
# 而整套测试共用一个数据库。用通用的 "t1" 会和 test_launch_meta_key_width.py
# 撞上：按字母序本文件先跑，留下的缓存行让那边「清理后应该什么都不剩」的断言失败。
# 单独跑两个文件都通过、一起跑才炸，正是这种污染最典型的表现。
_TENANT_ID = "scope-probe-tenant"


def _fetch(monkeypatch, compartment_ocid: str) -> _Session:
    session = _Session(compartment_ocid)
    # force=True bypasses both cache levels, so every read really happens.
    launch_service.fetch_launch_meta(session, tenant_id=_TENANT_ID, force=True)
    return session


@pytest.fixture(autouse=True)
def _clean_cache():
    """写完就清干净，不给后面的模块留状态。"""
    yield
    try:
        launch_service.clear_launch_meta_cache(_TENANT_ID)
    except Exception:  # noqa: BLE001
        pass


def test_every_read_uses_the_configured_compartment(monkeypatch):
    """The regression: a key scoped to CHILD must never be asked about the root."""
    s = _fetch(monkeypatch, CHILD)

    for kind, seen in s.asked.items():
        assert seen, f"{kind} was never read"
        assert all(c == CHILD for c in seen), (
            f"{kind} was read from {seen!r}; a key scoped to the child compartment "
            f"404s on the tenancy root"
        )


def test_the_tenancy_root_is_still_used_when_no_compartment_is_configured(monkeypatch):
    """`default_comp` falls back to the tenancy, which stays correct."""
    s = _fetch(monkeypatch, "")

    for kind, seen in s.asked.items():
        assert all(c == TENANCY for c in seen), f"{kind} -> {seen!r}"


def test_images_are_not_asked_for_the_root_which_would_disable_their_own_fallback(monkeypatch):
    """`list_images` retries at the root only when it was given something else.

    Handing it the root up front makes `compartment != tenancy_ocid` false and
    silently removes that retry, so this asserts the input rather than the retry.
    """
    s = _fetch(monkeypatch, CHILD)
    assert TENANCY not in s.asked["images"]
