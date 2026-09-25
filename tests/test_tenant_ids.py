"""Regression tests: tenant ids must stay unique so multiple accounts can be
switched between. Colliding / empty ids previously overwrote each other in the
store's id-keyed dict, leaving only one usable account."""

from __future__ import annotations

import json
import uuid

from app.config_store import ConfigStore, TenantConfig

from tests._keys import TEST_PEM

KEY = TEST_PEM


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

    # Oracle 官方中文名（出处见 app/formatting.py::_REGION_NAME_ZH），逐字。
    assert region_area("eu-amsterdam-1") == "荷兰西北部（阿姆斯特丹）"
    assert region_area("ap-tokyo-1") == "日本东部（东京）"
    assert region_area("ap-osaka-1") == "日本中部（大阪）"
    assert region_area("ap-singapore-1") == "新加坡（新加坡）"
    assert region_area("ap-singapore-2") == "新加坡西部（新加坡）"  # distinct from Singapore
    assert region_area("eu-zurich-1") == "瑞士北部（苏黎世）"
    assert region_area("us-sanjose-1") == "美国西部（圣何塞）"
    assert region_area("us-phoenix-1") == "美国西部（凤凰城）"
    assert region_area("uk-london-1") == "英国南部（伦敦）"
    assert region_area("uk-cardiff-1") == "英国西部（纽波特）"
    assert region_area("AP-Tokyo-1") == "日本东部（东京）"  # ids are case-insensitive
    # Unknown regions fall back to the region id, not a useless placeholder.
    assert region_area("xx-atlantis-9") == "xx-atlantis-9"
    assert region_area("") == "未知"


def test_region_names_are_never_guessed():
    """没有官方出处的区域必须原样显示标识符，而不是按城市名「猜」一个。

    以前是按城市子串模糊匹配的：ap-kulai-2 会被猜不出来还算好，更糟的是一个带
    tokyo 字样的新区域（比如 ap-tokyo-2）会被安上 ap-tokyo-1 的名字 —— 看着很像，
    其实是另一个区域。
    """
    from app.formatting import region_area

    # 标识符表的英文写法和中文官网不同，由操作者确认对应。
    assert region_area("ap-kulai-2") == "马来西亚西部（古来）"
    assert region_area("eu-madrid-3") == "西班牙中部 2（马德里）"
    # 同城市的新编号不能继承老区域的名字。
    assert region_area("ap-tokyo-2") == "ap-tokyo-2"
    assert region_area("eu-frankfurt-2") == "欧盟主权区域中部（法兰克福）"


def test_sidebar_label_uses_the_region_name():
    t = _mk("主力号")
    t.region = "eu-amsterdam-1"
    t.account_tier = "free"
    assert t.area_label() == "荷兰西北部（阿姆斯特丹）"
    assert t.sidebar_label() == "荷兰西北部（阿姆斯特丹） - 免费 - 主力号"
    t.description = "备用  生产机"
    assert t.sidebar_label() == "荷兰西北部（阿姆斯特丹） - 免费 - 主力号 · 备用 生产机"


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
