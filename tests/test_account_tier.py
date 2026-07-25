"""Regression tests for Always Free vs PAYG classification.

Tier comes from the tenancy's subscription record. Billed spend is deliberately
not used: an upgraded (PAYG) tenancy that only runs Always Free resources bills
nothing, so spend cannot tell the two apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import oci.tenant_manager_control_plane as tmcp

from app.oci_client import TenantSession

SSL_EOF = (
    "OCIConnectionPool(host='usageapi.eu-zurich-1.oci.oraclecloud.com', port=443): "
    "Max retries exceeded with url: /20200107/usage (Caused by SSLError(SSLEOFError(8, "
    "'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')))"
)


def _session(payment_model, subscription_tier="", monkeypatch=None, home_region="eu-amsterdam-1"):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        tenancy_ocid="ocid1.tenancy..t", region="eu-amsterdam-1", name="OCI-eu-amsterdam-1"
    )
    s._config = {
        "user": "u", "fingerprint": "f", "tenancy": "ocid1.tenancy..t",
        "region": "eu-amsterdam-1", "key_file": "k",
    }
    s._identity = SimpleNamespace(
        get_tenancy=lambda t: SimpleNamespace(
            data=SimpleNamespace(name="T", home_region_key="AMS", description="")
        ),
        list_region_subscriptions=lambda t: SimpleNamespace(
            data=[SimpleNamespace(region_name=home_region, is_home_region=True)]
        ),
    )
    s._limits = SimpleNamespace(list_limit_values=lambda t, service_name=None: SimpleNamespace(data=[]))

    class FakeSubscription:
        def __init__(self, cfg, **_kw):
            self.region = cfg["region"]

        def list_subscriptions(self, compartment_id=None, entity_version=None):
            if payment_model is None:
                raise RuntimeError("no subscription access")
            return SimpleNamespace(
                data=SimpleNamespace(items=[SimpleNamespace(payment_model=payment_model, id="sub1")])
            )

        def get_subscription(self, subscription_id=None, entity_version=None):
            return SimpleNamespace(
                data=SimpleNamespace(subscription_tier=subscription_tier, promotion=None)
            )

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    return s


def test_upgraded_payg_is_detected_without_any_spend(monkeypatch):
    """The reported bug: an upgraded account with no paid usage showed 未升级."""
    s = _session("Pay as you go", monkeypatch=monkeypatch)
    data = s.get_account_status().data
    assert data["tier_code"] == "paid"
    assert "未升级" not in data["tier"]


def test_detect_account_tier_is_subscription_only(monkeypatch):
    """Sidebar probe must not touch Service Limits / get_tenancy."""
    s = _session("Pay as you go", monkeypatch=monkeypatch)
    called = {"tenancy": 0, "limits": 0}

    def _boom_tenancy(*_a, **_k):
        called["tenancy"] += 1
        raise AssertionError("get_tenancy should not run for tier-only probe")

    def _boom_limits(*_a, **_k):
        called["limits"] += 1
        raise AssertionError("list_limit_values should not run for tier-only probe")

    s._identity = SimpleNamespace(
        get_tenancy=_boom_tenancy,
        list_region_subscriptions=lambda t: SimpleNamespace(data=[]),
    )
    s._limits = SimpleNamespace(list_limit_values=_boom_limits)
    data = s.detect_account_tier().data
    assert data["tier_code"] == "paid"
    assert called["tenancy"] == 0
    assert called["limits"] == 0


def test_genuine_free_tier_subscription(monkeypatch):
    s = _session("Free Tier", subscription_tier="FREETIER", monkeypatch=monkeypatch)
    data = s.get_account_status().data
    assert data["tier_code"] == "free"
    assert "Always Free" in data["tier"]


def test_no_subscription_access_is_undetermined_not_free(monkeypatch):
    """Without evidence we must not claim the account is un-upgraded."""
    s = _session(None, monkeypatch=monkeypatch)
    data = s.get_account_status().data
    assert data["tier_code"] == "unknown"
    assert "无法确定" in data["tier"]
    assert "未升级" not in data["tier"]
    assert "inspect subscriptions" in data["tier_reason"]  # tells the user the fix


def test_ssl_eof_is_treated_as_a_network_error():
    s = TenantSession.__new__(TenantSession)
    assert s._is_network_error(Exception(SSL_EOF))
    assert not s._is_network_error(Exception("NotAuthorizedOrNotFound"))


def test_subscription_lookup_falls_back_to_tenant_region_on_tls_failure(monkeypatch):
    """Home-region endpoint TLS reset must not break tier detection."""
    s = _session("Pay as you go", monkeypatch=monkeypatch, home_region="eu-zurich-1")
    assert s._account_api_regions() == ["eu-zurich-1", "eu-amsterdam-1"]
    tried = []

    class FakeSubscription:
        def __init__(self, cfg, **_kw):
            self.region = cfg["region"]
            tried.append(cfg["region"])

        def list_subscriptions(self, compartment_id=None, entity_version=None):
            if self.region == "eu-zurich-1":
                raise RuntimeError(SSL_EOF)
            return SimpleNamespace(
                data=SimpleNamespace(items=[SimpleNamespace(payment_model="Pay as you go", id="s1")])
            )

        def get_subscription(self, subscription_id=None, entity_version=None):
            return SimpleNamespace(data=SimpleNamespace(subscription_tier="", promotion=None))

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    assert s.get_account_status().data["tier_code"] == "paid"
    assert tried == ["eu-zurich-1", "eu-amsterdam-1"]


def test_unreachable_subscription_endpoints_report_network_not_free(monkeypatch):
    s = _session("Pay as you go", monkeypatch=monkeypatch, home_region="eu-zurich-1")

    class FakeSubscription:
        def __init__(self, cfg, **_kw):
            pass

        def list_subscriptions(self, compartment_id=None, entity_version=None):
            raise RuntimeError(SSL_EOF)

    monkeypatch.setattr(tmcp, "SubscriptionClient", FakeSubscription)
    data = s.get_account_status().data
    assert data["tier_code"] == "unknown"
    assert "网络不可达" in data["tier_reason"]


def test_billing_api_is_gone():
    """Monthly billing/cost query was removed — no Usage API surface should remain."""
    assert not hasattr(TenantSession, "get_monthly_cost_summary")
    assert not hasattr(TenantSession, "get_cost_summary")
    assert not hasattr(TenantSession, "_detect_billed_spend")
