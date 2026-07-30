"""Cross-site request policy for state-changing requests (CSRF).

Audit pass 10. Before this, every mutating REST endpoint was authorized by the
session cookie alone. `SameSite=Lax` was the only thing standing in the way, and
it is not enough on its own:

* `OCIBOT_COOKIE_SAMESITE=none` is a supported setting and removes the protection
  entirely;
* cookies are scoped to a *site*, not an origin — a panel on
  `panel.example.com` and an attacker-controlled `blog.example.com` are the same
  site, so the session cookie rides along on a cross-origin POST from there.

The WebSocket endpoint already checked Origin (pass 4, CSWSH). These tests pin
that the REST side now uses the *same* policy object, so the two cannot drift.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_origin_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'o.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "origin-test-master-key-0123456789ab")
os.environ.setdefault("OCIBOT_JWT_SECRET", "origin-test-jwt-secret-0123456789ab")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.config import Settings  # noqa: E402
from web.backend.origin_guard import UNSAFE_METHODS, origin_allowed  # noqa: E402

PANEL = "panel.example.com"


# ---------------------------------------------------------------------------
# Policy matrix
# ---------------------------------------------------------------------------


def test_same_origin_is_allowed():
    assert origin_allowed(f"https://{PANEL}", host=PANEL) is True


def test_cross_origin_is_rejected():
    assert origin_allowed("https://evil.example.net", host=PANEL) is False


def test_sibling_subdomain_is_rejected():
    """The case SameSite=Lax does NOT cover: same site, different origin."""
    assert origin_allowed("https://blog.example.com", host=PANEL) is False


def test_missing_origin_is_allowed():
    """curl and scripts omit it, and they carry no victim's cookie."""
    assert origin_allowed("", host=PANEL) is True


def test_opaque_origin_is_rejected():
    """A sandboxed iframe sends Origin: null."""
    assert origin_allowed("null", host=PANEL) is False
    assert origin_allowed("not-a-url", host=PANEL) is False


def test_scheme_mismatch_still_matches():
    """Behind a TLS-terminating proxy the browser says https:// while this hop is
    plain http://, so only host[:port] may be compared."""
    assert origin_allowed(f"https://{PANEL}", host=PANEL) is True


def test_port_is_part_of_the_comparison():
    # Not 5173/8080: those are in the default OCIBOT_CORS_ORIGINS dev allowlist
    # and would pass on that route instead, which is not what this pins.
    assert origin_allowed(f"https://{PANEL}:8443", host=PANEL) is False
    assert origin_allowed(f"https://{PANEL}:8443", host=f"{PANEL}:8443") is True


def test_forwarded_host_is_accepted(monkeypatch):
    """A proxy that puts the upstream name in Host must not break the panel."""
    assert (
        origin_allowed(f"https://{PANEL}", host="ocibot-api:8000", forwarded_host=PANEL)
        is True
    )


def test_allowlisted_cors_origin_is_accepted(monkeypatch):
    """A separate dev frontend origin stays usable when explicitly configured."""
    import web.backend.origin_guard as mod

    monkeypatch.setattr(
        mod, "get_settings", lambda: Settings(OCIBOT_CORS_ORIGINS="http://localhost:5173")
    )
    assert origin_allowed("http://localhost:5173", host="localhost:8000") is True


def test_only_mutating_methods_are_guarded():
    """A GET must never be rejected — no GET route here changes state, and the
    SPA shell itself is fetched cross-origin in some embeds."""
    assert UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
    assert "GET" not in UNSAFE_METHODS
    assert "OPTIONS" not in UNSAFE_METHODS  # CORS preflight


# ---------------------------------------------------------------------------
# End to end: the middleware is actually wired up
# ---------------------------------------------------------------------------

# 403 means the guard fired. 401/429 means the request reached the login handler
# (bad credentials / rate limited) — either way the guard let it through.
_PASSED_GUARD = {401, 429}


def _login(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/api/auth/login",
        json={"username": "origin-probe", "password": "not-a-real-password"},
        headers=headers,
    )


def test_middleware_rejects_a_cross_site_post():
    from web.backend.main import app

    with TestClient(app) as c:
        r = _login(c, {"origin": "https://evil.example.net"})
    assert r.status_code == 403, r.text
    # The message has to say what to do: if a reverse proxy rewrote Host, this
    # looks like the entire panel breaking and the fix is not guessable.
    assert "OCIBOT_CORS_ORIGINS" in r.json()["detail"]


def test_middleware_allows_a_same_origin_post():
    from web.backend.main import app

    with TestClient(app) as c:
        r = _login(c, {"origin": "http://testserver", "host": "testserver"})
    assert r.status_code in _PASSED_GUARD, r.text


def test_middleware_ignores_origin_on_reads():
    from web.backend.main import app

    with TestClient(app) as c:
        r = c.get("/api/health", headers={"origin": "https://evil.example.net"})
    assert r.status_code == 200, r.text


def test_escape_hatch_disables_the_check():
    """An operator locked out by a proxy quirk needs a way back in."""
    from web.backend.config import get_settings
    from web.backend.main import create_app

    os.environ["OCIBOT_ORIGIN_CHECK"] = "0"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            r = _login(c, {"origin": "https://evil.example.net"})
        assert r.status_code in _PASSED_GUARD, r.text
    finally:
        os.environ.pop("OCIBOT_ORIGIN_CHECK", None)
        get_settings.cache_clear()


def test_check_is_on_by_default():
    assert Settings().origin_check is True
