"""Creating several identical instances in one submit (`count`).

The rule that matters: Always Free caps are tenancy-wide TOTALS, so the guard has
to validate count × the config. Checking one instance and then creating four
would wave a batch through at four times the allowance — the guard would be
decorative for exactly the case it exists to stop.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_count_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'c.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "count-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "count-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from app import free_quota  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"


# ---------------------------------------------------------------------------
# Guard arithmetic (pure)
# ---------------------------------------------------------------------------

# Nothing used yet: the whole 4 OCPU / 24 GB / 200 GB allowance is free.
_EMPTY = {"usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0}}


def _guard(count: int, *, ocpus=1, memory=6, boot=50, usage=None):
    return free_quota.validate_launch_against_quota(
        shape="VM.Standard.A1.Flex",
        ocpus=ocpus,
        memory_in_gbs=memory,
        boot_volume_size_in_gbs=boot,
        boot_volume_vpus_per_gb=10,
        free_only_mode=True,
        account_tier="free",
        usage=usage or _EMPTY,
        count=count,
    )


def test_a_batch_that_exactly_fills_the_allowance_is_allowed():
    """4 × (1 OCPU / 6 GB / 50 GB) = 4 / 24 / 200 — the whole free tier."""
    assert _guard(4).ok is True


def test_one_instance_too_many_is_blocked():
    """The fifth would be 5 OCPU. Checking a single instance would have allowed
    it — this is the case the count-aware guard exists for."""
    g = _guard(5)
    assert g.ok is False
    assert any("A1" in m for m in g.error_messages())


def test_cpu_is_summed_across_the_batch():
    g = _guard(2, ocpus=4, memory=24, boot=50)
    assert g.ok is False
    assert any("8 OCPU" in m for m in g.error_messages())


def test_boot_volume_is_summed_across_the_batch():
    """Disk is the limit a batch hits first and the easiest to overlook."""
    g = _guard(4, ocpus=1, memory=6, boot=100)  # 400 GB > 200
    assert g.ok is False
    assert any("块存储" in m for m in g.error_messages())
    assert any("4 × 100" in m for m in g.error_messages()), g.error_messages()


def test_count_one_matches_the_old_single_instance_behaviour():
    single = free_quota.validate_launch_against_quota(
        shape="VM.Standard.A1.Flex",
        ocpus=4,
        memory_in_gbs=24,
        boot_volume_size_in_gbs=100,
        boot_volume_vpus_per_gb=10,
        free_only_mode=True,
        account_tier="free",
        usage=_EMPTY,
    )
    assert single.ok is _guard(1, ocpus=4, memory=24, boot=100).ok is True


def test_projection_reports_the_batch():
    g = _guard(3, ocpus=1, memory=6, boot=50)
    assert g.projected["count"] == 3
    assert g.projected["units"]["a1_ocpu"] == 3
    assert g.projected["units_per_instance"]["a1_ocpu"] == 1
    assert g.projected["boot_gb_total"] == 150


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    init_db()
    username = "launch-count-user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).one_or_none()
        if tenant is None:
            tenant = Tenant(
                owner_id=user.id,
                name="CT",
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
        assert (
            c.post(
                "/api/auth/login", json={"username": username, "password": "supersecret123"}
            ).status_code
            == 200
        )
        yield c, tenant_id


class _R:
    def __init__(self, ok=True, message="创建成功", data=None, work_request_id="wr"):
        self.ok = ok
        self.message = message
        self.data = data or {}
        self.work_request_id = work_request_id


def _stub_launch(monkeypatch, *, results=None, auth_mode="key"):
    """Stub everything around the batch loop so the loop itself is what is tested."""
    calls: list[dict] = []
    session = MagicMock()

    def _launch(payload, root_password="", custom_user_data="", idempotency_key=""):
        calls.append(
            {
                "payload": dict(payload),
                "root_password": root_password,
                "idempotency_key": idempotency_key,
            }
        )
        if results:
            return results[min(len(calls) - 1, len(results) - 1)]
        return _R(data={"instance_id": f"ocid1.instance.oc1..n{len(calls)}"})

    session.launch_from_payload.side_effect = _launch
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    monkeypatch.setattr(instances_router, "fetch_launch_meta", lambda *a, **k: {})
    monkeypatch.setattr(
        instances_router,
        "build_launch_request",
        lambda body, meta=None: {
            "payload": {
                "display_name": body.get("display_name") or "instance",
                "shape": body.get("shape"),
                "compartment_id": "ocid1.compartment.oc1..c",
                "auth_mode": auth_mode,
                "boot_volume_vpus_per_gb": 10,
            },
            "root_password": body.get("root_password") or ("Generated-1234" if auth_mode == "password" else ""),
            "custom_user_data": "",
            "as_retry": bool(body.get("as_retry")),
            "fallback_configs": [],
        },
    )
    monkeypatch.setattr(instances_router, "prepare_launch_network", lambda s, p, **k: p)
    monkeypatch.setattr(instances_router, "enforce_secondary_region", lambda *a, **k: "")
    monkeypatch.setattr(instances_router, "enforce_launch_quota", lambda *a, **k: None)
    monkeypatch.setattr(instances_router, "format_guard_warnings", lambda g: [])
    monkeypatch.setattr(instances_router, "schedule_post_launch_adjustments", lambda *a, **k: None)
    return calls


def _body(**over) -> dict:
    body = {
        "display_name": "web",
        "shape": "VM.Standard.A1.Flex",
        "image_id": "ocid1.image.oc1..i",
        "ocpus": 1,
        "memory_in_gbs": 6,
        "boot_volume_size_in_gbs": 50,
    }
    body.update(over)
    return body


def test_three_instances_are_created_with_numbered_names(client, monkeypatch):
    c, tid = client
    calls = _stub_launch(monkeypatch)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=3))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["created_count"] == 3 and d["requested_count"] == 3
    assert [x["payload"]["display_name"] for x in calls] == ["web-1", "web-2", "web-3"]
    assert len(d["instances"]) == 3


def test_a_single_instance_keeps_its_exact_name(client, monkeypatch):
    """Suffixing a lone instance would rename every existing workflow's machine."""
    c, tid = client
    calls = _stub_launch(monkeypatch)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=1))
    assert r.status_code == 200, r.text
    assert calls[0]["payload"]["display_name"] == "web"


def test_a_partial_batch_is_not_reported_as_ok(client, monkeypatch):
    """Two of four created must not look like a clean success."""
    c, tid = client
    results = [
        _R(data={"instance_id": "ocid1.instance.oc1..n1"}),
        _R(data={"instance_id": "ocid1.instance.oc1..n2"}),
        _R(ok=False, message="Out of host capacity", data={"capacity": True}),
    ]
    calls = _stub_launch(monkeypatch, results=results)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=4))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["created_count"] == 2
    assert d["requested_count"] == 4
    assert "2/4" in d["message"]
    # Stopped at the failure instead of burning the 4th call on an AD that is out.
    assert len(calls) == 3


def test_total_failure_still_reports_the_reason(client, monkeypatch):
    c, tid = client
    _stub_launch(monkeypatch, results=[_R(ok=False, message="Out of host capacity")])
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=2))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["created_count"] == 0
    assert "capacity" in d["message"].lower()


def test_auto_generated_passwords_differ_per_instance(client, monkeypatch):
    """One password across a batch means one leak hands over every machine."""
    c, tid = client
    calls = _stub_launch(monkeypatch, auth_mode="password")
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=3))
    assert r.status_code == 200, r.text
    passwords = [x["root_password"] for x in calls]
    assert all(passwords), passwords
    assert len(set(passwords)) == 3, passwords


def test_an_operator_supplied_password_is_reused(client, monkeypatch):
    """They typed one on purpose; handing back three different ones would be wrong."""
    c, tid = client
    calls = _stub_launch(monkeypatch, auth_mode="password")
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=3, root_password="MyOwnPass123!"))
    assert r.status_code == 200, r.text
    assert {x["root_password"] for x in calls} == {"MyOwnPass123!"}


def test_capacity_retry_refuses_a_batch(client, monkeypatch):
    """One job per tenant, one machine per job — creating a single instance
    silently would look like the count field was ignored."""
    c, tid = client
    _stub_launch(monkeypatch)
    r = c.post(f"/api/tenants/{tid}/launch", json=_body(count=3, as_retry=True))
    assert r.status_code == 400, r.text
    assert "1 台" in r.json()["detail"]


def test_count_is_bounded(client, monkeypatch):
    c, tid = client
    _stub_launch(monkeypatch)
    assert c.post(f"/api/tenants/{tid}/launch", json=_body(count=0)).status_code == 422
    assert c.post(f"/api/tenants/{tid}/launch", json=_body(count=99)).status_code == 422


# ---------------------------------------------------------------------------
# The UI must surface every generated password, and must explain a lost response.
# No JS test runner here, so these assert against the Vue source.
# ---------------------------------------------------------------------------

_LAUNCH_VIEW = (
    Path(__file__).resolve().parents[1] / "web" / "frontend" / "src" / "views" / "LaunchView.vue"
)


def test_batch_passwords_all_reach_the_screen():
    """A batch generates a separate password per machine after the first and the
    server returns them all in `instances`. The reveal panel was fed from the
    scalar `root_password`, which only ever held the FIRST one — creating three
    password-mode machines showed one password and dropped the other two.

    They are still recoverable from the instance list (each lives in that
    instance's OCI tag), so this was not permanent loss — but the screen that
    exists specifically to hand them over was showing a third of them.
    """
    src = _LAUNCH_VIEW.read_text(encoding="utf-8")
    assert "pendingPasswords" in src
    assert "data.instances" in src, "must read the per-instance list, not just the head"
    assert "v-for=\"p in pendingPasswords\"" in src


def test_lost_response_is_explained_for_gateway_statuses():
    """Cloudflare answers an overrun with 520/524 and an HTML body, so the API
    interceptor finds no `detail` and the user got a bare "Request failed with
    status code 520" — no hint that the launch may have succeeded, and every
    reason to retry blindly. Only the status-less client timeout used to be
    handled."""
    src = _LAUNCH_VIEW.read_text(encoding="utf-8")
    assert "_GATEWAY_STATUSES" in src
    for status in (502, 504, 520, 524):
        assert str(status) in src, f"{status} not treated as a lost response"
    # The multi-line guidance is useless if the box collapses newlines.
    assert 'class="error-box" style="white-space: pre-line"' in src
