"""Regression tests for multi-account switching (stale-load discard).

These construct the real Tk app headlessly; skipped automatically if no display
is available.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.config_store import ConfigStore, TenantConfig

KEY = "-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n"


def _mk(name: str) -> TenantConfig:
    return TenantConfig(
        id=f"{name}-{uuid.uuid4()}",
        name=name,
        user_ocid="ocid1.user.oc1..aaaaaaaa" + "b" * 40,
        tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44,
        fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
        region="ap-tokyo-1",
        private_key_pem=KEY,
    )


@pytest.fixture(scope="module")
def app():
    # One Tk root for the whole module: creating multiple tk.Tk() roots in a
    # single process corrupts Tcl state, so we seed once and share.
    import tempfile

    tmp = tempfile.mkdtemp(prefix="ocibot_switch_")
    prev = {k: os.environ.get(k) for k in ("APPDATA", "XDG_CONFIG_HOME")}
    os.environ["APPDATA"] = tmp
    os.environ["XDG_CONFIG_HOME"] = tmp
    from pathlib import Path

    store = ConfigStore(data_dir=Path(tmp) / "ocibot")
    ta, tb = _mk("AAAA"), _mk("BBBB")
    store._tenants[ta.id] = ta
    store._tenants[tb.id] = tb
    store._active_tenant_id = ta.id
    store.save()

    try:
        from app.gui import OCIBotApp
        application = OCIBotApp()
    except Exception as exc:  # pragma: no cover - headless CI without a display
        pytest.skip(f"Tk unavailable: {exc}")
    application.ids = {"AAAA": ta.id, "BBBB": tb.id}
    yield application
    try:
        application._on_close()
    except Exception:
        pass
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_is_current_load_gen_and_target(app):
    app._load_gen = 5
    app._selected_tenant_id = "B"
    assert app._is_current_load(5) is True
    assert app._is_current_load(4) is False           # older generation discarded
    assert app._is_current_load(5, "B") is True
    assert app._is_current_load(5, "A") is False       # switched away -> discard


def test_switch_clears_stale_and_reloads(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "refresh_instances", lambda: calls.append(app._selected_tenant_id))
    a, b = app.ids["AAAA"], app.ids["BBBB"]

    # Currently showing A's data.
    app._selected_tenant_id = a
    app._loaded_scope = a
    app._loading = False
    app._instances = [object()]

    app._select_tenant(b)
    assert app._selected_tenant_id == b
    assert app._instances == []          # stale A rows cleared immediately
    assert app._loaded_scope is None
    assert calls == [b]                  # a fresh load was kicked off


def test_reclick_same_tenant_does_not_reload(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "refresh_instances", lambda: calls.append(1))
    b = app.ids["BBBB"]
    app._selected_tenant_id = b
    app._loaded_scope = b               # B's data is what's actually displayed
    app._loading = False
    app._instances = [object()]

    app._select_tenant(b)
    assert calls == []                  # no redundant reload


def _seed_instance(app):
    from app.oci_client import InstanceInfo

    inst = InstanceInfo(
        id="i1", display_name="web-1", lifecycle_state="RUNNING", region="ap-tokyo-1",
        availability_domain="AD-1", fault_domain="", shape="VM.Standard.A1.Flex",
        ocpus=2, memory_gb=12, time_created="", compartment_id="c", image_id="",
        freeform_tags={"ocibot_ssh_user": "root"}, defined_tags={}, tenant_id="t",
        tenant_name="T", private_ip="10.0.0.5", public_ip="152.70.1.2",
        ipv6_addresses=["2603:c020::1"], boot_volume_gb=50, boot_vpus_per_gb=10,
    )
    app._instances = [inst]
    app._apply_filter()
    return inst


def test_ip_cells_hold_the_raw_address(app):
    _seed_instance(app)
    values = app.tree.item("i1", "values")
    # chk,state,name,tenant,shape,ocpu,mem,disk,diskperf,pubip,privip,ipv6,ad
    assert values[5] == "2"                # ocpu
    assert values[6] == "12 G"             # memory
    assert values[7] == "50 G"             # disk
    assert values[8] == "10 平衡"           # disk performance
    assert values[9] == "152.70.1.2"        # public ipv4
    assert values[10] == "10.0.0.5"         # private ipv4
    assert values[11] == "2603:c020::1"     # ipv6


def test_clicking_ip_cell_copies_that_address(app, monkeypatch):
    _seed_instance(app)
    copied = []
    monkeypatch.setattr(app, "_copy_text", lambda text, label: copied.append((label, text)))
    monkeypatch.setattr(app.tree, "identify", lambda what, x, y: "cell")
    monkeypatch.setattr(app.tree, "identify_row", lambda y: "i1")

    class _Event:
        x = y = 0

    for column, expected in (("#10", "152.70.1.2"), ("#11", "10.0.0.5"), ("#12", "2603:c020::1")):
        monkeypatch.setattr(app.tree, "identify_column", lambda x, c=column: c)
        copied.clear()
        app._on_tree_click(_Event())
        assert copied and copied[0][1] == expected


def test_right_click_menu_copy_actions(app, monkeypatch):
    _seed_instance(app)
    copied = []
    monkeypatch.setattr(app, "_copy_text", lambda text, label: copied.append(text))
    app._tree_menu_target = "i1"
    for field, expected in (
        ("public_ip", "152.70.1.2"),
        ("private_ip", "10.0.0.5"),
        ("ipv6", "2603:c020::1"),
        ("id", "i1"),
        ("ssh", "ssh root@152.70.1.2"),
    ):
        copied.clear()
        app._copy_row_field(field, "x")
        assert copied == [expected]


def test_sidebar_shows_region_tier_and_name(app):
    """Sidebar rows read: '区域 - 免费 / 已升级 / 未知 - 显示名称'."""
    tenants = app.store.list_tenants()
    tenants[0].account_tier = "paid"
    tenants[0].region = "eu-amsterdam-1"
    tenants[1].account_tier = "free"
    tenants[1].region = "ap-tokyo-1"
    app._refresh_tenant_list()
    rows = [app.tenant_listbox.get(i) for i in range(app.tenant_listbox.size())]
    assert rows[0] == f"阿姆斯特丹 - 已升级 - {tenants[0].name}"
    assert rows[1] == f"东京 - 免费 - {tenants[1].name}"


def test_sidebar_marks_disabled_and_unknown_tier(app):
    tenants = app.store.list_tenants()
    tenants[0].account_tier = ""
    tenants[0].region = "us-ashburn-1"
    tenants[1].account_tier = "free"
    tenants[1].region = "ap-singapore-2"
    tenants[1].enabled = False
    app._refresh_tenant_list()
    rows = [app.tenant_listbox.get(i) for i in range(app.tenant_listbox.size())]
    assert rows[0] == f"阿什本 - 未知 - {tenants[0].name}"
    assert rows[1] == f"新加坡西 - 免费 - {tenants[1].name}（停用）"
    tenants[1].enabled = True  # restore for other tests


def test_tree_column_name_mapping(app):
    assert app._tree_column_name("#1") == "chk"
    assert app._tree_column_name("#6") == "ocpu"
    assert app._tree_column_name("#10") == "pubip"
    assert app._tree_column_name("#11") == "privip"
    assert app._tree_column_name("#12") == "ipv6"
    assert app._tree_column_name("#99") == ""   # out of range is safe


def test_switch_reloads_even_if_previous_still_loading(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "refresh_instances", lambda: calls.append(app._selected_tenant_id))
    a, b = app.ids["AAAA"], app.ids["BBBB"]
    # A selected and still loading (loaded_scope not yet A).
    app._selected_tenant_id = a
    app._loaded_scope = None
    app._loading = True
    app._instances = []

    app._select_tenant(b)
    assert app._selected_tenant_id == b
    assert calls == [b]                  # switch is honored, not blocked by _loading
