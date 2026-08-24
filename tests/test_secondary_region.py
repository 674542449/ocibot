"""副区 (secondary region) support: subscribing a tenancy to another region, and
creating instances there.

Two things are load-bearing here:

* A secondary region is modelled as its own tenant row (``parent_tenant_id`` points
  at the primary). An OCI session is bound to exactly one region, so this is what
  makes every existing per-tenant page work in the new region unchanged.
* Always Free exists **only in the home region**. The per-region usage snapshot
  cannot see that — read from a fresh 副区 it reports zero usage, i.e. a full free
  allowance that does not exist. So the free-cap guard is *replaced* there by an
  explicit region gate driven by the tenant's own ``free_only_mode`` flag.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_subregion_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'r.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "subregion-master-key-0123456789ab")
os.environ.setdefault("OCIBOT_JWT_SECRET", "subregion-jwt-secret-0123456789ab")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
import web.backend.routers.tenants as tenants_router  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402
from web.backend.quota_guard import (  # noqa: E402
    enforce_secondary_region,
    is_secondary_region,
    region_pair,
)

from tests._keys import TEST_PEM, TEST_PEM_PKCS1

# 必须是**能真正解析**的 PEM：TenantConfig.validate() 现在用
# load_pem_private_key 解析私钥，标记形状的假串会被正确拒绝。
_PEM = TEST_PEM


class _Result:
    def __init__(self, ok=True, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data or {}
        self.work_request_id = ""


def _fake_session(region: str, home: str) -> SimpleNamespace:
    return SimpleNamespace(tenant=SimpleNamespace(region=region), home_region=lambda: home)


# ---------------------------------------------------------------- region gate


def test_home_region_session_is_not_secondary():
    session = _fake_session("ap-tokyo-1", "ap-tokyo-1")
    assert is_secondary_region(session) is False
    assert enforce_secondary_region(session, free_only_mode=True) == ""


def test_unreadable_region_falls_back_to_home_region():
    """A stub / unreachable home-region lookup must not block every launch."""
    assert region_pair(_fake_session("ap-tokyo-1", "")) == ("", "")
    assert region_pair(MagicMock()) == ("", "")
    assert is_secondary_region(MagicMock()) is False
    assert enforce_secondary_region(MagicMock(), free_only_mode=True) == ""


def test_free_only_tenant_cannot_create_in_a_secondary_region():
    session = _fake_session("ap-osaka-1", "ap-tokyo-1")
    assert is_secondary_region(session) is True
    with pytest.raises(HTTPException) as excinfo:
        enforce_secondary_region(session, free_only_mode=True)
    assert excinfo.value.status_code == 400
    assert "ap-osaka-1" in str(excinfo.value.detail)


def test_opting_into_billing_allows_a_secondary_region_with_a_warning():
    session = _fake_session("ap-osaka-1", "ap-tokyo-1")
    note = enforce_secondary_region(session, free_only_mode=False)
    assert "ap-osaka-1" in note and "计费" in note


# ------------------------------------------------------- subscribe_region call


def _subscribe_self(subscribed=(("ap-tokyo-1", "NRT", True),), catalog=(("ap-osaka-1", "KIX"),)):
    """Minimal stand-in for a TenantSession, enough for the unbound method."""
    from app.oci_client import OperationResult

    return SimpleNamespace(
        tenant=SimpleNamespace(
            tenancy_ocid="ocid1.tenancy.oc1..aaaa", region="ap-tokyo-1"
        ),
        list_subscribed_regions=lambda: OperationResult(
            ok=True,
            message="",
            data={
                "home_region": "ap-tokyo-1",
                "regions": [
                    {"region_name": n, "region_key": k, "status": "READY", "is_home_region": h}
                    for n, k, h in subscribed
                ],
            },
        ),
        list_all_regions=lambda: OperationResult(
            ok=True,
            message="",
            data={"regions": [{"region_name": n, "region_key": k} for n, k in catalog]},
        ),
        _config_for_region=lambda region: {"region": region},
        _home_region=lambda: "ap-tokyo-1",
    )


@pytest.fixture()
def captured_subscribe(monkeypatch):
    """Capture what create_region_subscription is actually called with."""
    pytest.importorskip("oci")
    import app.oci_client as oci_client

    seen: dict = {}

    class _FakeIdentity:
        def __init__(self, config, **kwargs):
            seen["config"] = config

        def create_region_subscription(self, details, tenancy_id):
            seen["region_key"] = details.region_key
            seen["tenancy_id"] = tenancy_id

    monkeypatch.setattr(oci_client, "IdentityClient", _FakeIdentity)
    return seen


def test_region_key_is_sent_exactly_as_oracle_spells_it(captured_subscribe):
    """Regression: the key was lowercased on the way in, and CreateRegionSubscription
    resolves the region BY that key — "kix" is not an entity, so Oracle answered
    [404] EntityNotFound, which reads like a permissions problem."""
    from app.oci_client import TenantSession

    result = TenantSession.subscribe_region(_subscribe_self(), "ap-osaka-1")
    assert result.ok, result.message
    assert captured_subscribe["region_key"] == "KIX"
    assert captured_subscribe["tenancy_id"] == "ocid1.tenancy.oc1..aaaa"
    # The subscription only works against the home-region endpoint.
    assert captured_subscribe["config"]["region"] == "ap-tokyo-1"


@pytest.mark.parametrize("given", ["ap-osaka-1", "AP-OSAKA-1", "KIX", "kix"])
def test_region_can_be_named_by_id_or_key_in_any_case(captured_subscribe, given):
    from app.oci_client import TenantSession

    result = TenantSession.subscribe_region(_subscribe_self(), given)
    assert result.ok, result.message
    assert captured_subscribe["region_key"] == "KIX"


def test_already_subscribed_region_skips_the_oracle_mutation(captured_subscribe):
    from app.oci_client import TenantSession

    session = _subscribe_self(subscribed=(("ap-tokyo-1", "NRT", True), ("ap-osaka-1", "KIX", False)))
    result = TenantSession.subscribe_region(session, "ap-osaka-1")
    assert result.ok and result.data["already"] is True
    assert "region_key" not in captured_subscribe


def test_each_failing_step_names_itself():
    """All three OCI calls can answer 404; an unattributed message cannot be acted on."""
    from app.oci_client import OperationResult, TenantSession

    session = _subscribe_self()
    session.list_subscribed_regions = lambda: OperationResult(ok=False, message="[404] EntityNotFound")
    assert "读取已开通区域失败" in TenantSession.subscribe_region(session, "ap-osaka-1").message

    session = _subscribe_self()
    session.list_all_regions = lambda: OperationResult(ok=False, message="[404] EntityNotFound")
    assert "读取区域清单失败" in TenantSession.subscribe_region(session, "ap-osaka-1").message

    session = _subscribe_self()
    assert "未知区域" in TenantSession.subscribe_region(session, "no-such-region-9").message


def test_permission_hint_is_added_to_a_404_from_the_subscribe_call(monkeypatch):
    pytest.importorskip("oci")
    import app.oci_client as oci_client
    from oci.exceptions import ServiceError

    class _Denied:
        def __init__(self, config, **kwargs):
            pass

        def create_region_subscription(self, details, tenancy_id):
            raise ServiceError(404, "EntityNotFound", {}, "Entity not found")

    monkeypatch.setattr(oci_client, "IdentityClient", _Denied)
    result = oci_client.TenantSession.subscribe_region(_subscribe_self(), "ap-osaka-1")
    assert result.ok is False
    assert "KIX" in result.message
    assert "Administrators" in result.message and "PAYG" in result.message


# ---------------------------------------------------------------- API surface


@pytest.fixture(scope="module")
def client():
    init_db()
    username = "subregion-user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        else:
            user.password_hash = hash_password("supersecret123")
            user.is_active = True
        tenant = Tenant(
            owner_id=user.id,
            name="Primary",
            region="ap-tokyo-1",
            user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(_PEM),
        )
        db.add(tenant)
        db.commit()
        tenant_id = tenant.id

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": username, "password": "supersecret123"})
        assert r.status_code == 200, r.text
        yield c, tenant_id


def _stub_regions(monkeypatch, *, subscribed=("ap-tokyo-1",)):
    """Stub the tenants router's OCI session with a small region catalogue."""
    catalog = {
        "ap-tokyo-1": "NRT",
        "ap-osaka-1": "KIX",
        "eu-frankfurt-1": "FRA",
        "ap-seoul-1": "ICN",
    }
    session = MagicMock()
    session.home_region.return_value = "ap-tokyo-1"
    session.list_subscribed_regions.return_value = _Result(
        True,
        "",
        {
            "home_region": "ap-tokyo-1",
            "regions": [
                {
                    "region_name": name,
                    "region_key": catalog[name],
                    "status": "READY",
                    "is_home_region": name == "ap-tokyo-1",
                }
                for name in subscribed
            ],
        },
    )
    session.list_all_regions.return_value = _Result(
        True, "", {"regions": [{"region_name": n, "region_key": k} for n, k in catalog.items()]}
    )

    def _subscribe(region: str):
        if region in subscribed:
            return _Result(True, "该区域已开通", {"region_name": region, "already": True})
        if region not in catalog:
            return _Result(False, f"未知区域：{region}")
        return _Result(True, "已提交开通", {"region_name": region, "already": False})

    session.subscribe_region.side_effect = _subscribe
    monkeypatch.setattr(tenants_router, "get_session_for_row", lambda row: session)
    return session


def test_regions_endpoint_splits_subscribed_from_available(client, monkeypatch):
    c, tid = client
    _stub_regions(monkeypatch)
    d = c.get(f"/api/tenants/{tid}/regions").json()
    assert d["ok"] is True
    assert d["home_region"] == "ap-tokyo-1"
    assert [r["region_name"] for r in d["subscribed"]] == ["ap-tokyo-1"]
    assert d["subscribed"][0]["is_home_region"] is True
    # The home region already has a panel row — its own tenant.
    assert d["subscribed"][0]["tenant_id"] == tid
    available = {r["region_name"] for r in d["available"]}
    assert "ap-osaka-1" in available and "ap-tokyo-1" not in available
    # Localized labels come from app.formatting.region_area.
    assert next(r for r in d["available"] if r["region_name"] == "ap-osaka-1")["region_label"] == "大阪"


def test_subscribe_requires_explicit_confirmation(client, monkeypatch):
    """An OCI region subscription cannot be undone, so it is never implicit."""
    c, tid = client
    session = _stub_regions(monkeypatch)
    r = c.post(f"/api/tenants/{tid}/regions/subscribe", json={"region": "ap-osaka-1"})
    assert r.status_code == 400
    session.subscribe_region.assert_not_called()


def test_subscribing_adds_a_linked_secondary_tenant(client, monkeypatch):
    c, tid = client
    _stub_regions(monkeypatch)
    d = c.post(
        f"/api/tenants/{tid}/regions/subscribe",
        json={"region": "eu-frankfurt-1", "confirm": True},
    ).json()
    assert d["ok"] is True, d
    child = d["tenant"]
    assert child["region"] == "eu-frankfurt-1"
    assert child["parent_tenant_id"] == tid
    assert child["region_label"] == "法兰克福"
    # Always Free does not reach a 副区, so the row is created as billable —
    # otherwise the free-cap guard would refuse every launch in it.
    assert child["free_only_mode"] is False

    with SessionLocal() as db:
        row = db.get(Tenant, child["id"])
        assert row is not None
        # Credentials are the same tenancy's, copied so the row can stand alone.
        parent = db.get(Tenant, tid)
        assert row.tenancy_ocid == parent.tenancy_ocid
        assert row.private_key_encrypted == parent.private_key_encrypted

    # Listing again shows it as subscribed AND already present in the panel.
    _stub_regions(monkeypatch, subscribed=("ap-tokyo-1", "eu-frankfurt-1"))
    listed = c.get(f"/api/tenants/{tid}/regions").json()
    entry = next(r for r in listed["subscribed"] if r["region_name"] == "eu-frankfurt-1")
    assert entry["tenant_id"] == child["id"]
    assert "eu-frankfurt-1" not in {r["region_name"] for r in listed["available"]}


def test_subscribing_an_already_subscribed_region_only_adds_the_row(client, monkeypatch):
    """The normal path for someone who subscribed in the Oracle console first."""
    c, tid = client
    _stub_regions(monkeypatch, subscribed=("ap-tokyo-1", "ap-osaka-1"))
    d = c.post(
        f"/api/tenants/{tid}/regions/subscribe",
        json={"region": "ap-osaka-1", "confirm": True},
    ).json()
    assert d["ok"] is True, d
    assert d["already_subscribed"] is True
    assert d["tenant"]["region"] == "ap-osaka-1"

    # Repeating it must not create a second row for the same region.
    again = c.post(
        f"/api/tenants/{tid}/regions/subscribe",
        json={"region": "ap-osaka-1", "confirm": True},
    ).json()
    assert again["tenant"]["id"] == d["tenant"]["id"]
    with SessionLocal() as db:
        rows = db.query(Tenant).filter(Tenant.region == "ap-osaka-1").all()
        assert len(rows) == 1


def test_deleting_a_primary_removes_its_secondary_rows(client, monkeypatch):
    """副区 rows share the primary's credentials; leaving them orphaned would give
    the user a tenant they cannot re-key."""
    c, _tid = client
    _stub_regions(monkeypatch)
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "subregion-user").one()
        parent = Tenant(
            owner_id=user.id,
            name="Doomed",
            region="ap-tokyo-1",
            user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..doomed",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(_PEM),
        )
        db.add(parent)
        db.flush()
        child = Tenant(
            owner_id=user.id,
            name="Doomed · 大阪",
            region="ap-osaka-1",
            parent_tenant_id=parent.id,
            user_ocid=parent.user_ocid,
            tenancy_ocid=parent.tenancy_ocid,
            fingerprint=parent.fingerprint,
            private_key_encrypted=parent.private_key_encrypted,
            free_only_mode=False,
        )
        db.add(child)
        db.commit()
        parent_id, child_id = parent.id, child.id

    r = c.delete(f"/api/tenants/{parent_id}")
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(Tenant, parent_id) is None
        assert db.get(Tenant, child_id) is None


def _secondary_tenant(owner_username: str = "subregion-user") -> str:
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == owner_username).one()
        row = (
            db.query(Tenant)
            .filter(Tenant.owner_id == user.id, Tenant.region == "ap-osaka-1")
            .one()
        )
        return row.id


def _stub_launch_session(monkeypatch, region: str, home: str = "ap-tokyo-1"):
    session = MagicMock()
    session.tenant = SimpleNamespace(region=region)
    session.home_region.return_value = home
    session.get_free_quota_usage.return_value = _Result(
        True, "", {"account_tier": "paid", "usage": {}, "remaining": {}, "buckets": {}}
    )
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    return session


def test_quota_precheck_reports_a_secondary_region_instead_of_empty_gauges(
    client, monkeypatch
):
    """The 副区 usage snapshot is per-region: it would read as a full free
    allowance. The pre-check says "not applicable" rather than showing that."""
    c, _tid = client
    sub_id = _secondary_tenant()
    session = _stub_launch_session(monkeypatch, "ap-osaka-1")
    d = c.post(
        f"/api/tenants/{sub_id}/launch-quota-check",
        json={"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..i",
              "ocpus": 4, "memory_in_gbs": 24},
    ).json()
    assert d["secondary_region"] is True
    assert d["blocked"] is False  # the row opted into billing when it was created
    assert d["warnings"] and "计费" in d["warnings"][0]
    assert d["buckets"] == {}
    session.get_free_quota_usage.assert_not_called()


def test_db_link_gates_the_region_when_the_oracle_lookup_fails(client, monkeypatch):
    """The probe compares session region to home region — if Oracle cannot answer,
    both collapse to "home" and the free-cap guard would run on a 副区's (empty)
    per-region usage. The tenant row's own parent link covers that."""
    c, _tid = client
    sub_id = _secondary_tenant()
    session = MagicMock()
    session.tenant = SimpleNamespace(region="ap-osaka-1")
    session.home_region.side_effect = RuntimeError("region subscriptions unreadable")
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)

    d = c.post(
        f"/api/tenants/{sub_id}/launch-quota-check",
        json={"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..i",
              "ocpus": 4, "memory_in_gbs": 24},
    ).json()
    assert d["secondary_region"] is True, d
    assert d["region"] == "ap-osaka-1"
    assert d["warnings"] and "计费" in d["warnings"][0]
    session.get_free_quota_usage.assert_not_called()


def test_free_only_secondary_tenant_is_blocked_by_the_precheck(client, monkeypatch):
    c, _tid = client
    sub_id = _secondary_tenant()
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": True})
        db.commit()
    try:
        _stub_launch_session(monkeypatch, "ap-osaka-1")
        d = c.post(
            f"/api/tenants/{sub_id}/launch-quota-check",
            json={"shape": "VM.Standard.A1.Flex", "image_id": "ocid1.image.oc1..i",
                  "ocpus": 4, "memory_in_gbs": 24},
        ).json()
        assert d["blocked"] is True, d
        assert any("副区" in e for e in d["errors"]), d["errors"]
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": False})
            db.commit()


def test_launch_into_a_free_only_secondary_tenant_is_refused(client, monkeypatch):
    """Enforcement, not just the pre-check — the two share enforce_secondary_region."""
    c, _tid = client
    sub_id = _secondary_tenant()
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": True})
        db.commit()
    try:
        session = _stub_launch_session(monkeypatch, "ap-osaka-1")
        monkeypatch.setattr(
            instances_router,
            "fetch_launch_meta",
            lambda s, *, tenant_id, force=False: {
                "ads": ["AD-1"],
                "default_compartment": "ocid1.compartment.oc1..c1",
                "preferred_subnet_id": "sub1",
                "preferred_vcn_id": "vcn1",
                "subnets_by_vcn": {},
            },
        )
        r = c.post(
            f"/api/tenants/{sub_id}/launch",
            json={
                "shape": "VM.Standard.A1.Flex",
                "image_id": "ocid1.image.oc1..img",
                "auth_mode": "key",
                "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfake",
                "ocpus": 4,
                "memory_in_gbs": 24,
            },
        )
        assert r.status_code == 400, r.text
        assert "副区" in r.json()["detail"]
        session.launch_from_payload.assert_not_called()
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": False})
            db.commit()


def test_rotating_the_primary_key_updates_its_secondary_rows(client, monkeypatch):
    """副区 rows hold a COPY of the primary's credentials — the same Oracle API key
    by construction. Without propagation, rotating the primary's key left every
    secondary region authenticating with the old one until it failed as a 401."""
    c, tid = client
    sub_id = _secondary_tenant()
    new_pem = TEST_PEM_PKCS1  # 另一把、同样能解析的密钥

    r = c.patch(
        f"/api/tenants/{tid}",
        json={"private_key_pem": new_pem, "fingerprint": "aa:" * 15 + "aa"},
    )
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        parent = db.get(Tenant, tid)
        child = db.get(Tenant, sub_id)
        assert child.private_key_encrypted == parent.private_key_encrypted
        assert child.fingerprint == parent.fingerprint
        # Still linked, still its own region.
        assert child.parent_tenant_id == tid
        assert child.region == "ap-osaka-1"


# ------------------------------------------------- storage guards in a 副区


def _stub_storage_session(monkeypatch, region: str = "ap-osaka-1", home: str = "ap-tokyo-1"):
    """A 副区 session for the storage router, with a FULL free-storage snapshot.

    200 GB already used means any free-cap check would refuse — so if the request
    succeeds, the 副区 gate really did replace that check rather than stack with it.
    """
    import web.backend.routers.storage as storage_router

    session = MagicMock()
    session.tenant = SimpleNamespace(region=region)
    session.home_region.return_value = home
    session.resolve_compartment.return_value = "ocid1.compartment.oc1..c1"
    session.get_free_quota_usage.return_value = _Result(
        True,
        "",
        {
            "account_tier": "",
            "usage": {"block_storage_gb": 200.0},
            "remaining": {"block_storage_gb": 0.0},
            "buckets": {},
        },
    )
    session.create_block_volume.return_value = _Result(True, "已创建", {"id": "v1"})
    monkeypatch.setattr(storage_router, "get_session_for_row", lambda row: session)
    return session


def test_secondary_region_volume_creation_skips_the_free_storage_cap(client, monkeypatch):
    """The snapshot says the free 200GB is fully used, but that allowance does not
    exist in a 副区 and the snapshot only counts one region anyway."""
    c, _tid = client
    sub_id = _secondary_tenant()
    session = _stub_storage_session(monkeypatch)
    r = c.post(
        f"/api/tenants/{sub_id}/block-volumes",
        json={"availability_domain": "AD-1", "size_in_gbs": 500},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    session.create_block_volume.assert_called_once()


def test_free_only_secondary_tenant_cannot_create_volumes(client, monkeypatch):
    c, _tid = client
    sub_id = _secondary_tenant()
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": True})
        db.commit()
    try:
        session = _stub_storage_session(monkeypatch)
        r = c.post(
            f"/api/tenants/{sub_id}/block-volumes",
            json={"availability_domain": "AD-1", "size_in_gbs": 60},
        )
        # The 400 must survive the router's broad except -> 502 wrapper.
        assert r.status_code == 400, r.text
        assert "副区" in r.json()["detail"]
        session.create_block_volume.assert_not_called()
    finally:
        with SessionLocal() as db:
            db.query(Tenant).filter(Tenant.id == sub_id).update({"free_only_mode": False})
            db.commit()


def test_home_region_tenant_still_hits_the_free_storage_cap(client, monkeypatch):
    """The gate must not have disabled the cap for ordinary tenants."""
    c, tid = client
    session = _stub_storage_session(monkeypatch, region="ap-tokyo-1", home="ap-tokyo-1")
    r = c.post(
        f"/api/tenants/{tid}/block-volumes",
        json={"availability_domain": "AD-1", "size_in_gbs": 500},
    )
    assert r.status_code == 400, r.text
    assert "块存储" in r.json()["detail"]
    session.create_block_volume.assert_not_called()


def test_long_primary_name_does_not_overflow_the_child_name_column(client, monkeypatch):
    """Tenant.name is VARCHAR(128). PostgreSQL raises on overflow, and by then the
    Oracle subscription — which cannot be undone — has already been made."""
    c, _tid = client
    _stub_regions(monkeypatch, subscribed=("ap-tokyo-1", "ap-seoul-1"))
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "subregion-user").one()
        parent = Tenant(
            owner_id=user.id,
            name="X" * 128,
            region="ap-tokyo-1",
            user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
            tenancy_ocid="ocid1.tenancy.oc1..longname",
            fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            private_key_encrypted=encrypt_text(_PEM),
        )
        db.add(parent)
        db.commit()
        parent_id = parent.id

    d = c.post(
        f"/api/tenants/{parent_id}/regions/subscribe",
        json={"region": "ap-seoul-1", "confirm": True},
    ).json()
    assert d["ok"] is True, d
    assert len(d["tenant"]["name"]) <= 128
    assert d["tenant"]["name"].endswith("首尔")
