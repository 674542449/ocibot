"""Short-lived read cache behind the auto-loading pages (0.4.20).

Pages fetch on entry now, so without this every visit and tab switch would be a
fresh Oracle fan-out — and a 429 spent rendering a list is one the capacity retry
loop does not get. The risk it introduces is stale data, so what is pinned here is
the invalidation, not the hit rate.
"""

from __future__ import annotations

import time

import pytest

from web.backend import read_cache


@pytest.fixture(autouse=True)
def _clean():
    read_cache.clear()
    yield
    read_cache.clear()


def _counting_loader():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return f"value-{calls['n']}"

    return loader, calls


def test_second_read_is_served_from_cache():
    loader, calls = _counting_loader()
    key = read_cache.cache_key("t1", "instances")
    first, age1 = read_cache.get_or_load(key, loader)
    second, _age2 = read_cache.get_or_load(key, loader)
    assert first == second == "value-1"
    assert age1 == 0
    assert calls["n"] == 1


def test_force_bypasses_and_refills():
    """What 刷新 does: the user asked for current data, so they get it."""
    loader, calls = _counting_loader()
    key = read_cache.cache_key("t1", "instances")
    read_cache.get_or_load(key, loader)
    forced, age = read_cache.get_or_load(key, loader, force=True)
    assert forced == "value-2" and age == 0
    assert calls["n"] == 2
    # The forced result is now the cached one.
    again, _ = read_cache.get_or_load(key, loader)
    assert again == "value-2"
    assert calls["n"] == 2


def test_expired_entry_reloads():
    loader, calls = _counting_loader()
    key = read_cache.cache_key("t1", "instances")
    read_cache.get_or_load(key, loader, ttl=1)
    time.sleep(1.05)
    value, _ = read_cache.get_or_load(key, loader, ttl=1)
    assert value == "value-2"
    assert calls["n"] == 2


def test_query_parameters_are_part_of_the_key():
    """resolve_ips / include_subcompartments change the answer, so they must not
    share an entry."""
    loader, calls = _counting_loader()
    read_cache.get_or_load(read_cache.cache_key("t1", "instances", True, False), loader)
    read_cache.get_or_load(read_cache.cache_key("t1", "instances", True, True), loader)
    assert calls["n"] == 2


def test_tenants_never_share_an_entry():
    """Entries are keyed by tenant id and the handler authorizes before reading —
    a collision here would be one user's resources shown to another."""
    loader, _calls = _counting_loader()
    a, _ = read_cache.get_or_load(read_cache.cache_key("tenant-a", "instances"), loader)
    b, _ = read_cache.get_or_load(read_cache.cache_key("tenant-b", "instances"), loader)
    assert a != b


def test_invalidate_drops_every_family_for_one_tenant_only():
    loader, calls = _counting_loader()
    keys = [
        read_cache.cache_key("t1", "instances"),
        read_cache.cache_key("t1", "free-quota", True),
        read_cache.cache_key("t2", "instances"),
    ]
    for key in keys:
        read_cache.get_or_load(key, loader)
    assert calls["n"] == 3

    read_cache.invalidate("t1")
    read_cache.get_or_load(keys[0], loader)
    read_cache.get_or_load(keys[1], loader)
    assert calls["n"] == 5  # both t1 families reloaded
    read_cache.get_or_load(keys[2], loader)
    assert calls["n"] == 5  # t2 untouched


def test_invalidate_can_target_one_family():
    loader, calls = _counting_loader()
    inst = read_cache.cache_key("t1", "instances")
    quota = read_cache.cache_key("t1", "free-quota")
    read_cache.get_or_load(inst, loader)
    read_cache.get_or_load(quota, loader)
    read_cache.invalidate("t1", "instances")
    read_cache.get_or_load(inst, loader)
    assert calls["n"] == 3
    read_cache.get_or_load(quota, loader)
    assert calls["n"] == 3  # quota entry survived


def test_a_keyless_family_name_is_still_invalidated():
    """cache_key(t, name) has no trailing separator; a prefix-only match would miss it."""
    loader, calls = _counting_loader()
    key = read_cache.cache_key("t1", "account")
    read_cache.get_or_load(key, loader)
    read_cache.invalidate("t1", "account")
    read_cache.get_or_load(key, loader)
    assert calls["n"] == 2


def test_cache_is_bounded():
    loader, _ = _counting_loader()
    for i in range(read_cache._MAX_ENTRIES + 40):
        read_cache.get_or_load(read_cache.cache_key(f"t{i}", "instances"), loader)
    assert len(read_cache._CACHE) <= read_cache._MAX_ENTRIES
