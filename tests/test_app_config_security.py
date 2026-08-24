"""App-level configuration hardening.

Each test pins a property that was actually broken:

- ``OCIBOT_REQUIRE_SECURE_SECRETS`` defaulted to 0, so following docker-compose's
  own quick start served a panel on the master key printed in the public repo —
  every stored OCI private key decryptable by anyone who reads the database.
- ``app_version`` / ``debug`` / ``jwt_algorithm`` carried no alias, so the bare
  env names ``APP_VERSION`` / ``DEBUG`` / ``JWT_ALGORITHM`` overrode them. A stray
  ``APP_VERSION`` makes /api/health report a version unrelated to the running
  code — exactly the failure the release rule exists to prevent, and
  tests/test_version_bump.py cannot see it.
- CSP ``connect-src`` listed bare ``ws: wss:``, i.e. any host, which turned the
  strongest exfiltration control in the policy into an open channel.
- ``/openapi.json`` handed out all 88 routes plus every schema unauthenticated.
- the body ceiling was the innermost middleware while its docstring claimed the
  edge.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_settings")

from web.backend.config import (  # noqa: E402
    Settings,
    insecure_secret_error,
)

_STRONG = {
    "OCIBOT_MASTER_KEY": "x" * 40,
    "OCIBOT_JWT_SECRET": "y" * 40,
}

# tests/conftest.py exports usable dev secrets for the whole suite, so a bare
# Settings() is NOT the shipped-default baseline — spell the defaults out.
_BUILTIN_DEFAULTS = {
    "OCIBOT_MASTER_KEY": "dev-only-change-me-ocibot-web-master-key",
    "OCIBOT_JWT_SECRET": "dev-only-jwt-secret-change-me",
}


# ---------------------------------------------------------------------------
# Fail closed on the shipped dev secrets
# ---------------------------------------------------------------------------


def test_secure_secret_check_defaults_to_on():
    """The whole point: no env var set must mean "refuse to serve"."""
    assert Settings(**_STRONG).require_secure_secrets is True


def test_builtin_default_secrets_are_refused():
    problem = insecure_secret_error(
        Settings(
            OCIBOT_MASTER_KEY="dev-only-change-me-ocibot-web-master-key-please-rotate",
            OCIBOT_JWT_SECRET="dev-only-jwt-secret-change-me-please-rotate",
        )
    )
    assert problem is not None
    # Both offenders must be named — an operator fixing one and restarting into
    # the same error twice is the bad outcome.
    assert "OCIBOT_MASTER_KEY" in problem
    assert "OCIBOT_JWT_SECRET" in problem


def test_short_secrets_are_refused_and_the_length_is_stated():
    problem = insecure_secret_error(
        Settings(OCIBOT_MASTER_KEY="short", OCIBOT_JWT_SECRET="short")
    )
    assert problem is not None
    assert "24" in problem


def test_strong_secrets_start_normally():
    assert insecure_secret_error(Settings(**_STRONG)) is None


def test_the_error_is_actionable():
    """This text is the ONLY thing an operator sees when the panel stops booting."""
    problem = insecure_secret_error(Settings(**_BUILTIN_DEFAULTS))
    assert problem is not None
    assert "openssl rand -hex 48" in problem  # how to make a good value
    assert "OCIBOT_REQUIRE_SECURE_SECRETS=0" in problem  # how to get back online now
    assert "web/.env" in problem  # where to put it


def test_escape_hatch_still_works():
    """An operator locked out by the new default must have one step back online."""
    insecure = Settings(OCIBOT_REQUIRE_SECURE_SECRETS=False, **_BUILTIN_DEFAULTS)
    assert insecure_secret_error(insecure) is None
    # ...and the weakness is still reported, so it cannot be silently forgotten.
    assert insecure.weak_secret_reasons()


# ---------------------------------------------------------------------------
# Env aliases: no generic name may reach a setting
# ---------------------------------------------------------------------------


def test_every_setting_has_an_explicit_alias():
    """pydantic-settings falls back to the bare field name when no alias is given.

    That is how APP_VERSION / DEBUG / JWT_ALGORITHM became overridable. Pin the
    invariant for every field so the next one added cannot repeat it.
    """
    missing = [
        name
        for name, field in Settings.model_fields.items()
        if not field.alias or not (field.alias.startswith("OCIBOT_") or field.alias == "DATABASE_URL")
    ]
    assert not missing, f"Settings fields without an explicit OCIBOT_* alias: {missing}"


def test_app_version_cannot_come_from_the_environment(monkeypatch):
    """/api/health's version must describe the code, not the runtime env."""
    for name in ("APP_VERSION", "OCIBOT_APP_VERSION"):
        monkeypatch.setenv(name, "9.9.9")
    assert Settings(**_STRONG).app_version != "9.9.9"


def test_jwt_algorithm_cannot_come_from_the_environment(monkeypatch):
    """AUDIT.md pass 10 records HS256 as hardcoded; make that true.

    It feeds both jwt.encode() and jwt.decode(algorithms=[...]).
    """
    for name in ("JWT_ALGORITHM", "OCIBOT_JWT_ALGORITHM"):
        monkeypatch.setenv(name, "HS512")
    assert Settings(**_STRONG).jwt_algorithm == "HS256"


def test_debug_only_answers_to_the_namespaced_name(monkeypatch):
    monkeypatch.delenv("OCIBOT_DEBUG", raising=False)
    monkeypatch.setenv("DEBUG", "1")
    assert Settings(**_STRONG).debug is False
    monkeypatch.setenv("OCIBOT_DEBUG", "1")
    assert Settings(**_STRONG).debug is True


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("fastapi")
    from web.backend.main import app as real_app

    return real_app


def test_csp_allows_websockets_only_to_this_origin(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        csp = client.get("/api/health").headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp
    # Bare ws:/wss: mean ANY host; CSP3 'self' already covers same-origin sockets,
    # which is all wsUrl() in web/frontend/src/api/client.ts ever builds.
    assert "ws:" not in csp
    assert "wss:" not in csp


def test_openapi_schema_is_not_served_unauthenticated(app):
    from fastapi.testclient import TestClient

    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
    with TestClient(app) as client:
        body = client.get("/openapi.json").text
    # With the SPA mounted the path falls through to index.html; either way no
    # route map may come back.
    assert '"paths"' not in body


def test_docs_can_be_turned_back_on_for_development(monkeypatch):
    """Disabling them must not make local API work painful."""
    pytest.importorskip("fastapi")
    from web.backend import main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(OCIBOT_DEBUG=True, **_STRONG))
    debug_app = main.create_app()
    assert debug_app.openapi_url == "/openapi.json"
    assert debug_app.docs_url == "/docs"


def test_body_ceiling_is_the_outermost_middleware(app):
    """add_middleware PREPENDS, so the last one registered wraps the rest.

    The cap has to be outermost or a middleware added later that reads the body
    (request logging, HMAC verification, a WAF shim) would run outside it and
    consume the whole request before a single byte is counted.
    """
    from web.backend.body_limit import BodySizeLimitMiddleware

    assert app.user_middleware[0].cls is BodySizeLimitMiddleware


def test_oversized_body_is_413_with_security_headers(app):
    """Outermost means security_headers no longer wraps the 413 — AUDIT pass 10
    verified that response carries the headers, so body_limit emits them itself."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.post("/api/auth/login", content=b"x" * (33 * 1024 * 1024))
    assert r.status_code == 413
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# What a misconfigured deployment actually serves
# ---------------------------------------------------------------------------


def test_bad_secrets_serve_503_everywhere_instead_of_crash_looping(monkeypatch):
    """run.py starts uvicorn with workers=2, so a startup exception is an import
    failure in a child the parent keeps respawning — scrolling tracebacks instead
    of one instruction. main.py catches it and mounts nothing but the reason."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web.backend import main

    monkeypatch.setattr(main, "_STARTUP_BLOCK", "SECRETS ARE BAD: do X")
    broken = main.create_app()

    with TestClient(broken) as client:
        health = client.get("/api/health")
        post = client.post("/api/tenants", json={})
    assert health.status_code == 503
    assert "SECRETS ARE BAD: do X" in health.json()["detail"]
    assert post.status_code == 503  # nothing is reachable, not just health
