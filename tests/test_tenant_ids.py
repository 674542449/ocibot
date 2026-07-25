"""Regression tests: tenant ids must stay unique so multiple accounts can be
switched between. Colliding / empty ids previously overwrote each other in the
store's id-keyed dict, leaving only one usable account."""

from __future__ import annotations

import json
import uuid

from app.config_store import ConfigStore, TenantConfig

KEY = "-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n"


def _mk(name: str, tid: str = "") -> TenantConfig:
    return TenantConfig(
        id=tid,
        name=name,
        user_ocid="ocid1.user.oc1..aaaaaaaa" + "b" * 40,
        tenancy_ocid="ocid1.tenancy.oc1..aaaaaaaa" + "c" * 44,
        fingerprint="12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
        region="ap-tokyo-1",
        private_key_pem=KEY,
    )


def test_upsert_assigns_unique_ids_for_idless_tenants(tmp_path):
    store = ConfigStore(data_dir=tmp_path / "ocibot")
    store.upsert(_mk("A"), make_active=True)
    store.upsert(_mk("B"))
    store.upsert(_mk("C"))
    tenants = store.list_tenants()
    assert [t.name for t in tenants] == ["A", "B", "C"]
    assert len({t.id for t in tenants}) == 3  # all distinct, none dropped


def test_load_heals_duplicate_ids_on_disk(tmp_path):
    data_dir = tmp_path / "ocibot"
    store = ConfigStore(data_dir=data_dir)
    store.upsert(_mk("A"), make_active=True)
    store.upsert(_mk("B"))
    store.upsert(_mk("C"))

    # Corrupt the file so every tenant shares one id.
    path = data_dir / "tenants.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for item in raw["tenants"]:
        item["id"] = "same-id"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    reloaded = ConfigStore(data_dir=data_dir)
    tenants = reloaded.list_tenants()
    assert len(tenants) == 3  # healed into three distinct accounts
    assert len({t.id for t in tenants}) == 3

    # And the repair is persisted, so it survives the next launch.
    again = ConfigStore(data_dir=data_dir)
    assert len({t.id for t in again.list_tenants()}) == 3


def test_tenants_are_sorted_chronologically_not_alphabetically(tmp_path):
    """The list must follow the order accounts were added, not their names."""
    store = ConfigStore(data_dir=tmp_path / "ocibot")
    zebra = _mk("zebra-account")
    zebra.created_at = "2026-01-01T00:00:00+00:00"
    alpha = _mk("alpha-account")
    alpha.created_at = "2026-02-01T00:00:00+00:00"
    middle = _mk("middle-account")
    middle.created_at = "2026-03-01T00:00:00+00:00"
    store.upsert(zebra, make_active=True)
    store.upsert(alpha)
    store.upsert(middle)

    assert [t.name for t in store.list_tenants()] == [
        "zebra-account",
        "alpha-account",
        "middle-account",
    ]


def test_disabled_tenants_keep_their_chronological_position(tmp_path):
    store = ConfigStore(data_dir=tmp_path / "ocibot")
    first = _mk("first")
    first.created_at = "2026-01-01T00:00:00+00:00"
    second = _mk("second")
    second.created_at = "2026-02-01T00:00:00+00:00"
    second.enabled = False
    third = _mk("third")
    third.created_at = "2026-03-01T00:00:00+00:00"
    for t in (first, second, third):
        store.upsert(t)
    # A disabled account is not pushed to the bottom — order stays by time.
    assert [t.name for t in store.list_tenants()] == ["first", "second", "third"]


def test_region_area_and_sidebar_label():
    from app.formatting import region_area

    assert region_area("eu-amsterdam-1") == "阿姆斯特丹"
    assert region_area("ap-tokyo-1") == "东京"
    assert region_area("ap-osaka-1") == "大阪"
    assert region_area("ap-singapore-1") == "新加坡"
    assert region_area("ap-singapore-2") == "新加坡西"  # distinct from Singapore
    assert region_area("eu-zurich-1") == "苏黎世"
    assert region_area("us-sanjose-1") == "圣何塞"
    assert region_area("uk-london-1") == "伦敦"
    # Unknown regions fall back to the region id, not a useless placeholder.
    assert region_area("xx-atlantis-9") == "xx-atlantis-9"
    assert region_area("") == "未知"

    t = _mk("主力号")
    t.region = "eu-amsterdam-1"
    t.account_tier = "free"
    assert t.area_label() == "阿姆斯特丹"
    assert t.sidebar_label() == "阿姆斯特丹 - 免费 - 主力号"
    t.description = "备用  生产机"
    assert t.sidebar_label() == "阿姆斯特丹 - 免费 - 主力号 · 备用 生产机"


def test_tier_label_text():
    t = _mk("x")
    assert t.tier_label() == "未知"        # not detected yet
    t.account_tier = "free"
    assert t.tier_label() == "免费"
    t.account_tier = "paid"
    assert t.tier_label() == "已升级"


def test_account_tier_persists(tmp_path):
    data_dir = tmp_path / "ocibot"
    store = ConfigStore(data_dir=data_dir)
    t = _mk("acct")
    t.account_tier = "paid"
    store.upsert(t, make_active=True)
    assert ConfigStore(data_dir=data_dir).list_tenants()[0].account_tier == "paid"


def test_from_dict_regenerates_missing_id():
    t = TenantConfig.from_dict({"name": "x", "user_ocid": "u", "tenancy_ocid": "t",
                                "fingerprint": "f", "region": "r", "private_key_pem": KEY})
    assert t.id
    uuid.UUID(t.id)  # valid uuid
