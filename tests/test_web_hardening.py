"""Regression tests for the web hardening pass.

Each test pins a specific weakness that was found and fixed:
- uvicorn was told to trust X-Forwarded-For from every peer, which made the
  login rate limiter trivially bypassable;
- the WebSSH WebSocket authenticated by cookie but never checked Origin;
- a wildcard CORS origin combined with cookie credentials would let any site
  read the API as the logged-in user;
- multipart uploads were fully materialized before any size check.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException

from web.backend.config import Settings
from web.backend.routers.webssh import websocket_origin_allowed
from web.backend.uploads import read_upload_limited


# ---------------------------------------------------------------------------
# Proxy header trust (run.py)
# ---------------------------------------------------------------------------


def _run_kwargs(monkeypatch, env: dict[str, str]) -> dict:
    """Invoke run.main() with uvicorn.run captured, returning its kwargs."""
    import uvicorn

    for key in ("OCIBOT_TRUST_PROXY", "OCIBOT_FORWARDED_ALLOW_IPS", "OCIBOT_API_WORKERS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    captured: dict = {}

    def fake_run(_app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    from web.backend import run as run_mod

    run_mod.main()
    return captured


def test_proxy_headers_off_by_default(monkeypatch):
    """Without OCIBOT_TRUST_PROXY, uvicorn must not rewrite client from XFF.

    forwarded_allow_ips="*" previously let any direct client forge
    X-Forwarded-For, so request.client.host — the login rate-limit bucket key —
    was attacker-chosen and the throttle could be bypassed per request.
    """
    kwargs = _run_kwargs(monkeypatch, {})
    assert kwargs["proxy_headers"] is False
    assert kwargs["forwarded_allow_ips"] != "*"


def test_proxy_headers_opt_in_defaults_to_loopback(monkeypatch):
    kwargs = _run_kwargs(monkeypatch, {"OCIBOT_TRUST_PROXY": "1"})
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1,::1"


def test_forwarded_allow_ips_is_configurable(monkeypatch):
    kwargs = _run_kwargs(
        monkeypatch,
        {"OCIBOT_TRUST_PROXY": "true", "OCIBOT_FORWARDED_ALLOW_IPS": "172.18.0.0/16"},
    )
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "172.18.0.0/16"


# ---------------------------------------------------------------------------
# CORS wildcard + credentials
# ---------------------------------------------------------------------------


def test_cors_wildcard_is_dropped():
    """Starlette answers wildcard+credentials by reflecting the caller's Origin."""
    settings = Settings(OCIBOT_CORS_ORIGINS="*")
    assert settings.cors_origin_list() == []
    assert settings.cors_wildcard_requested() is True


def test_cors_wildcard_dropped_but_explicit_kept():
    settings = Settings(OCIBOT_CORS_ORIGINS="https://panel.example.com, * ,https://b.example")
    assert settings.cors_origin_list() == ["https://panel.example.com", "https://b.example"]
    assert settings.cors_wildcard_requested() is True


def test_cors_normal_list_reports_no_wildcard():
    settings = Settings(OCIBOT_CORS_ORIGINS="https://panel.example.com")
    assert settings.cors_origin_list() == ["https://panel.example.com"]
    assert settings.cors_wildcard_requested() is False


def test_weak_secret_reasons_flags_defaults_and_short_values():
    # Values passed explicitly: other test modules seed OCIBOT_* in the
    # environment at import time, so Settings() alone is not a clean baseline.
    builtin = Settings(
        OCIBOT_MASTER_KEY="dev-only-change-me-ocibot-web-master-key",
        OCIBOT_JWT_SECRET="dev-only-jwt-secret-change-me",
    )
    assert len(builtin.weak_secret_reasons()) == 2

    short = Settings(OCIBOT_MASTER_KEY="short", OCIBOT_JWT_SECRET="short")
    assert len(short.weak_secret_reasons()) == 2

    strong = Settings(OCIBOT_MASTER_KEY="x" * 40, OCIBOT_JWT_SECRET="y" * 40)
    assert strong.weak_secret_reasons() == []


# ---------------------------------------------------------------------------
# WebSocket origin (CSWSH)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_ws_allows_same_origin():
    ws = _FakeWebSocket({"host": "panel.example.com", "origin": "https://panel.example.com"})
    assert websocket_origin_allowed(ws) is True


def test_ws_allows_same_origin_with_port():
    ws = _FakeWebSocket({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"})
    assert websocket_origin_allowed(ws) is True


def test_ws_rejects_cross_site_origin():
    """The core CSWSH case: a malicious page riding the victim's session cookie."""
    ws = _FakeWebSocket({"host": "panel.example.com", "origin": "https://evil.example"})
    assert websocket_origin_allowed(ws) is False


def test_ws_rejects_origin_prefix_confusion():
    ws = _FakeWebSocket(
        {"host": "panel.example.com", "origin": "https://panel.example.com.evil.example"}
    )
    assert websocket_origin_allowed(ws) is False


def test_ws_allows_missing_origin_for_cli_clients():
    """Browsers always send Origin; non-browser clients carry no victim cookie."""
    ws = _FakeWebSocket({"host": "panel.example.com"})
    assert websocket_origin_allowed(ws) is True


def test_ws_allows_configured_cors_origin(monkeypatch):
    """A separate dev frontend origin stays usable when explicitly allowlisted."""
    # The allowlist lookup now lives in origin_guard, shared with the REST
    # middleware, so that is the seam to stub.
    import web.backend.origin_guard as origin_guard_mod

    monkeypatch.setattr(
        origin_guard_mod,
        "get_settings",
        lambda: Settings(OCIBOT_CORS_ORIGINS="http://localhost:5173"),
    )
    ws = _FakeWebSocket({"host": "localhost:8000", "origin": "http://localhost:5173"})
    assert websocket_origin_allowed(ws) is True


def test_ws_rejects_garbage_origin():
    ws = _FakeWebSocket({"host": "panel.example.com", "origin": "not-a-url"})
    assert websocket_origin_allowed(ws) is False


# ---------------------------------------------------------------------------
# Bounded upload reads
# ---------------------------------------------------------------------------


class _FakeUpload:
    def __init__(self, data: bytes) -> None:
        self.file = io.BytesIO(data)


def test_read_upload_limited_returns_small_payload():
    up = _FakeUpload(b"hello")
    assert read_upload_limited(up, 1024, too_large_detail="too big") == b"hello"


def test_read_upload_limited_allows_exactly_the_limit():
    up = _FakeUpload(b"a" * 100)
    assert read_upload_limited(up, 100, too_large_detail="too big") == b"a" * 100


def test_read_upload_limited_rejects_oversized():
    up = _FakeUpload(b"a" * 5000)
    with pytest.raises(HTTPException) as exc:
        read_upload_limited(up, 1000, too_large_detail="备份文件过大（上限 20MB）")
    assert exc.value.status_code == 400
    assert "过大" in exc.value.detail


def test_read_upload_limited_stops_early_on_oversized():
    """The whole body must not be buffered before the limit is enforced."""
    payload = b"a" * (4 * 1024 * 1024)
    up = _FakeUpload(payload)
    with pytest.raises(HTTPException):
        read_upload_limited(up, 64 * 1024, too_large_detail="too big")
    # Reading stopped shortly after the limit rather than consuming everything.
    assert up.file.tell() < len(payload)


def test_hashed_assets_are_cached_immutably_and_index_is_not():
    """Vite writes content-hashed asset names, so those bytes can never change
    meaning — caching them for a year removes one conditional round trip per file
    per page load, which on a high-latency link is most of the wait.

    index.html must NOT get that treatment: it is unhashed and names the current
    bundle, so a cached copy would keep pointing at the previous deploy's chunks.
    """
    from fastapi.testclient import TestClient

    from web.backend.main import app

    with TestClient(app) as client:
        spa = client.get("/")
        assert spa.status_code == 200
        assert spa.headers.get("cache-control") == "no-cache"

        api = client.get("/api/health")
        assert api.status_code == 200
        # API responses keep whatever they had; the SPA rule must not leak onto them.
        assert api.headers.get("cache-control") != "no-cache"
