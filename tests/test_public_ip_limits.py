"""「账号用量」页的公网 IP 配额：服务名和限额名都问 Oracle，不写死。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.oci_client import TenantSession


class _FakeLimits:
    def __init__(self, services, values, availability=None, fail_services=False):
        self.services = services
        self.values = values
        self.availability = availability or {}
        self.fail_services = fail_services
        self.asked: list[tuple] = []

    def list_services(self, compartment_id, **_kw):
        if self.fail_services:
            raise RuntimeError("NotAuthorized")
        return SimpleNamespace(data=self.services, has_next_page=False, next_page=None, headers={})

    def list_limit_values(self, compartment_id, service_name, **_kw):
        self.asked.append(("values", service_name))
        return SimpleNamespace(data=self.values, has_next_page=False, next_page=None, headers={})

    def get_resource_availability(self, service_name, limit_name, compartment_id, **kw):
        self.asked.append(("avail", service_name, limit_name, kw.get("availability_domain")))
        if limit_name not in self.availability:
            raise RuntimeError("not supported for this limit")
        used, available = self.availability[limit_name]
        return SimpleNamespace(data=SimpleNamespace(used=used, available=available))


def _svc(name, description=""):
    return SimpleNamespace(name=name, description=description)


def _val(name, value, ad="", scope="REGION"):
    return SimpleNamespace(name=name, value=value, availability_domain=ad, scope_type=scope)


def _session(limits):
    s = TenantSession.__new__(TenantSession)
    s._limits = limits
    return s


@pytest.fixture(autouse=True)
def _direct_pagination(monkeypatch):
    monkeypatch.setattr(
        "oci.pagination.list_call_get_all_results",
        lambda fn, *args, **kw: fn(*args, **{k: v for k, v in kw.items() if k != "retry_strategy"}),
    )


def test_only_public_ip_limits_with_usage_when_available():
    limits = _FakeLimits(
        services=[_svc("compute"), _svc("vcn", "Virtual Cloud Network")],
        values=[
            _val("reserved-public-ip-count", 50),
            _val("vcn-count", 50),
            _val("ephemeral-public-ip-count", 2),
        ],
        availability={"reserved-public-ip-count": (8, 42)},
    )
    rows = _session(limits)._public_ip_limits("tenancy")
    assert [r["name"] for r in rows] == ["ephemeral-public-ip-count", "reserved-public-ip-count"]
    reserved = rows[1]
    assert (reserved["value"], reserved["used"], reserved["available"]) == (50, 8, 42)
    # 不支持读已用数的那条：只给上限，不报错
    assert rows[0]["used"] is None and rows[0]["value"] == 2
    assert ("values", "vcn") in limits.asked


def test_the_network_service_is_found_by_description_if_not_named_vcn():
    limits = _FakeLimits(
        services=[_svc("compute"), _svc("networking", "Virtual Cloud Network (VCN)")],
        values=[_val("reserved-public-ip-count", 50)],
    )
    rows = _session(limits)._public_ip_limits("tenancy")
    assert [r["name"] for r in rows] == ["reserved-public-ip-count"]
    assert ("values", "networking") in limits.asked


def test_an_unreadable_limits_service_gives_an_empty_list_not_an_error():
    limits = _FakeLimits(services=[], values=[], fail_services=True)
    assert _session(limits)._public_ip_limits("tenancy") == []


def test_no_network_service_means_no_rows():
    limits = _FakeLimits(services=[_svc("compute")], values=[_val("reserved-public-ip-count", 50)])
    assert _session(limits)._public_ip_limits("tenancy") == []
