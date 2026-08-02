"""Monthly invoices and whether each was paid (OSP Gateway).

Usage and invoices answer different questions. Usage says what a month cost;
an invoice says what Oracle billed and whether it was settled. Only the invoice
service knows the payment state, which is the thing being asked for here.

The case that matters most for this panel: an Always Free / trial tenancy has no
subscription and therefore no invoices. Oracle answers that with a 404-flavoured
NotAuthorizedOrNotFound, which is the *correct* answer for such an account — so
it must read as "no bills", not as a failure.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_inv_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'i.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "invoice-master-key-0123456789abcd")
os.environ.setdefault("OCIBOT_JWT_SECRET", "invoice-jwt-secret-0123456789abcd")

pytest.importorskip("fastapi")

from datetime import datetime, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from app.oci_client import TenantSession  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"


class _Invoice:
    def __init__(self, **kw):
        self.invoice_id = kw.get("invoice_id", "inv-1")
        self.invoice_number = kw.get("invoice_number", "10001")
        self.invoice_status = kw.get("invoice_status", "CLOSED")
        self.is_paid = kw.get("is_paid", True)
        self.is_payment_failed = kw.get("is_payment_failed", False)
        self.invoice_type = kw.get("invoice_type", "USAGE")
        self.currency = kw.get("currency", "USD")
        self.invoice_amount = kw.get("invoice_amount", 12.5)
        self.invoice_amount_due = kw.get("invoice_amount_due", 0.0)
        self.time_invoice = kw.get("time_invoice", datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.time_invoice_due = kw.get("time_invoice_due", datetime(2026, 6, 30, tzinfo=timezone.utc))


def _session(items=None, raises: Exception | None = None) -> TenantSession:
    """A TenantSession with only what list_invoices touches."""
    s = TenantSession.__new__(TenantSession)
    s._config = {"region": "ap-tokyo-1"}  # type: ignore[attr-defined]
    s.tenant = MagicMock(tenancy_ocid="ocid1.tenancy.oc1..t", region="ap-tokyo-1")
    s._home_region = lambda: "ap-tokyo-1"  # type: ignore[attr-defined]

    client = MagicMock()
    if raises is not None:
        client.list_invoices.side_effect = raises
    else:
        client.list_invoices.return_value = MagicMock(data=MagicMock(items=items or []))

    import oci.osp_gateway as osp

    osp.InvoiceServiceClient = MagicMock(return_value=client)  # type: ignore[assignment]
    return s


# ---------------------------------------------------------------------------
# The OCI layer
# ---------------------------------------------------------------------------


def test_paid_invoice_is_reported_paid():
    r = _session([_Invoice(is_paid=True, invoice_status="CLOSED")]).list_invoices()
    assert r.ok is True
    inv = r.data["invoices"][0]
    assert inv["is_paid"] is True
    assert inv["invoice_number"] == "10001"
    assert inv["amount"] == 12.5


def test_open_invoice_is_reported_unpaid():
    r = _session(
        [_Invoice(is_paid=False, invoice_status="OPEN", invoice_amount_due=9.9)]
    ).list_invoices()
    inv = r.data["invoices"][0]
    assert inv["is_paid"] is False
    assert inv["status"] == "OPEN"
    assert inv["amount_due"] == 9.9


def test_status_is_the_fallback_when_is_paid_is_absent():
    """Some responses omit is_paid; CLOSED still means settled."""
    inv = _Invoice(invoice_status="CLOSED")
    inv.is_paid = None
    assert _session([inv]).list_invoices().data["invoices"][0]["is_paid"] is True

    inv2 = _Invoice(invoice_status="PAST_DUE")
    inv2.is_paid = None
    assert _session([inv2]).list_invoices().data["invoices"][0]["is_paid"] is False


def test_a_failed_payment_is_carried_through():
    """It is not merely unpaid — it needs action, so the UI must be able to say so."""
    r = _session([_Invoice(is_paid=False, invoice_status="OPEN", is_payment_failed=True)])
    assert r.list_invoices().data["invoices"][0]["is_payment_failed"] is True


def test_a_free_tenancy_reads_as_no_bills_not_as_an_error():
    """No subscription means no invoices. That is the right answer for an
    Always Free account, so it must not surface as a failure."""
    err = Exception("NotAuthorizedOrNotFound (404)")
    r = _session(raises=err).list_invoices()
    assert r.ok is True
    assert r.data["invoices"] == []
    assert r.data["unavailable"] is True
    assert "Always Free" in r.message


def test_a_real_failure_still_fails():
    r = _session(raises=Exception("ServiceError: 500 InternalServerError")).list_invoices()
    assert r.ok is False
    assert r.data["invoices"] == []


def test_empty_result_says_so_without_erroring():
    r = _session([]).list_invoices()
    assert r.ok is True and r.data["invoices"] == []
    assert "没有账单" in r.message


def test_timestamps_and_currency_are_serialisable():
    inv = _session([_Invoice()]).list_invoices().data["invoices"][0]
    assert inv["time_invoice"].startswith("2026-06-01")
    assert inv["time_due"].startswith("2026-06-30")
    assert inv["currency"] == "USD"


def test_limit_is_bounded():
    s = _session([])
    s.list_invoices(limit=9999)
    import oci.osp_gateway as osp

    kwargs = osp.InvoiceServiceClient.return_value.list_invoices.call_args.kwargs
    assert kwargs["limit"] == 100


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    init_db()
    username = "invoice-user"
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
                name="IT",
                region="ap-tokyo-1",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(_PEM),
            )
            db.add(tenant)
        db.commit()
        tid = tenant.id
    with TestClient(app) as c:
        assert (
            c.post(
                "/api/auth/login", json={"username": username, "password": "supersecret123"}
            ).status_code
            == 200
        )
        yield c, tid


def test_endpoint_returns_the_invoice_list(client, monkeypatch):
    c, tid = client
    stub = MagicMock()
    stub.list_invoices.return_value = type(
        "R", (), {"ok": True, "message": "已读取 1 张账单", "data": {"invoices": [{"is_paid": True}]}}
    )()
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: stub)
    r = c.get(f"/api/tenants/{tid}/invoices")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["invoices"] == [{"is_paid": True}]


def test_endpoint_does_not_5xx_when_oracle_refuses(client, monkeypatch):
    """Endpoint smoke rule: an OCI-facing route must answer, not blow up."""
    stub = MagicMock()
    stub.list_invoices.side_effect = RuntimeError("boom")
    c, tid = client
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: stub)
    r = c.get(f"/api/tenants/{tid}/invoices")
    assert r.status_code == 502, r.text
    assert "读取账单失败" in r.json()["detail"]
