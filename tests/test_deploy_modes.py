"""Deployment access modes: IP:port (plain) vs domain + bundled HTTPS.

`scripts/install.sh` writes six env keys as a SET, because they only make sense
together — COOKIE_SECURE=1 with no TLS in front means the browser silently stops
returning the session cookie, and TRUST_PROXY=1 with no proxy lets any client
declare its own rate-limit identity. Neither shows up as an error; both surface
as "logging in does nothing" or "one person's failed logins lock everyone out".

There is no shell test runner here, so these are text assertions over the shipped
deployment files. They are deliberately about the *pairings* rather than about
formatting: what must not silently drift is which value goes with which mode.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"
COMPOSE = ROOT / "docker-compose.yml"
CADDYFILE = ROOT / "deploy" / "Caddyfile"


def _body(text: str, func: str) -> str:
    """Extract a shell function body: `name() {` … up to the closing `\n}`.

    Anchored to the start of a line, otherwise asking for `compose` returns
    `detect_compose`'s body and the assertions silently check the wrong function.
    """
    match = re.search(rf"(?m)^{re.escape(func)}\(\) \{{", text)
    assert match, f"no such shell function: {func}"
    end = text.index("\n}", match.start())
    return text[match.start() : end]


@pytest.fixture(scope="module")
def install_sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_yml() -> str:
    return COMPOSE.read_text(encoding="utf-8")


# --------------------------------------------------------------------- pairings


def test_domain_mode_sets_the_https_group(install_sh: str) -> None:
    body = _body(install_sh, "apply_mode_domain")
    assert "set_env_kv OCIBOT_COOKIE_SECURE 1" in body
    assert "set_env_kv OCIBOT_TRUST_PROXY 1" in body
    assert "set_env_kv OCIBOT_BIND 127.0.0.1" in body, (
        "domain mode must pull the plain-HTTP port back to loopback, otherwise "
        "http://IP:8000 still serves the panel and bypasses TLS entirely"
    )
    assert "set_env_kv COMPOSE_PROFILES tls" in body
    assert 'set_env_kv OCIBOT_CORS_ORIGINS "https://$domain"' in body


def test_ip_mode_sets_the_plain_http_group(install_sh: str) -> None:
    body = _body(install_sh, "apply_mode_ip")
    assert "set_env_kv OCIBOT_COOKIE_SECURE 0" in body, (
        "a Secure cookie over plain HTTP is never returned by the browser: the "
        "operator logs in and lands back on the login page with no error"
    )
    assert "set_env_kv OCIBOT_TRUST_PROXY 0" in body
    assert "set_env_kv OCIBOT_BIND 0.0.0.0" in body
    assert 'set_env_kv COMPOSE_PROFILES ""' in body


def test_ip_mode_removes_the_tls_container(install_sh: str) -> None:
    # `compose up` leaves a running container that is outside the active profile
    # alone, so switching back would otherwise keep Caddy holding 80/443.
    body = _body(install_sh, "apply_mode_ip")
    assert "--profile tls rm -sf caddy" in body


def test_compose_wrapper_passes_the_profile_explicitly(install_sh: str) -> None:
    """Not a duplicate of the .env key: whether compose honours COMPOSE_PROFILES
    from an env file varies by version, and a missed profile is silent — the
    stack comes up healthy and the domain simply never answers."""
    body = _body(install_sh, "compose")
    assert "grep -qx 'COMPOSE_PROFILES=tls'" in body
    assert "profile_args=(--profile tls)" in body


def test_forwarded_allow_ips_is_never_wildcard(install_sh: str) -> None:
    # "*" would let a client that can reach uvicorn forge X-Forwarded-For and get
    # a fresh login rate-limit bucket per request.
    for match in re.findall(r"set_env_kv OCIBOT_FORWARDED_ALLOW_IPS (.+)", install_sh):
        assert "*" not in match, match


def test_uninstall_covers_the_profiled_service(install_sh: str) -> None:
    body = _body(install_sh, "do_uninstall")
    assert body.count("--profile tls down") == 2, (
        "both the plain `down` and the OCIBOT_PURGE_DATA `down -v` must name the "
        "profile, or uninstall leaves Caddy running on 80/443"
    )


# ----------------------------------------------------------------- domain input


@pytest.mark.parametrize(
    "raw",
    ["", "nodot", "-bad.com", "bad-.com", "a..b.com", "1.2.3.4", "ok.com\nOCIBOT_MASTER_KEY=x"],
)
def test_domain_charset_excludes_env_injection(raw: str) -> None:
    """The accepted charset is what stops a crafted "domain" writing a second
    line into web/.env — the value is interpolated straight into `OCIBOT_DOMAIN=`."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    body = _body(text, "normalize_domain")
    assert "*[!a-z0-9.-]*" in body
    # A newline is outside that class, so the multi-line case is covered by it.
    assert "\n" not in "abcdefghijklmnopqrstuvwxyz0123456789.-"


def test_normalize_domain_lowercases() -> None:
    # OCIBOT_CORS_ORIGINS is an exact string compare and browsers always send a
    # lowercase host in Origin, so Panel.Example.com would never match.
    body = _body(INSTALL_SH.read_text(encoding="utf-8"), "normalize_domain")
    assert "tr 'A-Z' 'a-z'" in body


# ------------------------------------------------------------------ compose/caddy


def test_caddy_is_behind_the_tls_profile(compose_yml: str) -> None:
    assert 'profiles: ["tls"]' in compose_yml, (
        "without a profile the HTTPS container would start on every install and "
        "collide with whatever already owns 80/443"
    )


def test_caddy_publishes_80_and_443(compose_yml: str) -> None:
    for port in ('"80:80"', '"443:443"'):
        assert port in compose_yml
    # 80 is not optional: it is how the ACME HTTP-01 challenge proves the domain.


def test_caddy_certificate_volume_is_declared(compose_yml: str) -> None:
    # Losing it re-issues on every rebuild, which walks into the Let's Encrypt
    # duplicate-certificate limit (5/week) and then blocks for a week.
    assert "ocibot_caddy_data:/data" in compose_yml
    assert re.search(r"(?m)^  ocibot_caddy_data:\s*$", compose_yml)


def test_caddyfile_proxies_to_the_api_service(compose_yml: str) -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "reverse_proxy api:8000" in text
    assert "{$OCIBOT_DOMAIN:localhost}" in text, (
        "an unset domain must fall back to a parseable site address; an empty "
        "one fails Caddyfile adaptation and the container restart-loops"
    )
    # Caddy sets X-Forwarded-For/-Proto/-Host and preserves Host by default.
    # origin_guard compares Host and the rate limiter reads X-Forwarded-For, so a
    # header_up override here would break one of them. Comments may name the
    # directive while explaining exactly that, so only directives are checked.
    directives = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("header_up" in ln for ln in directives)


def test_self_update_forwards_the_profile() -> None:
    """An in-panel update runs compose from a helper container; without
    COMPOSE_PROFILES it would not consider the HTTPS front end part of the stack."""
    from web.backend import self_update

    src = Path(self_update.__file__).read_text(encoding="utf-8")
    flags = src[src.index("def _compose_env_flags") : src.index("def _compose_base_args")]
    assert '"COMPOSE_PROFILES",' in flags
    assert '"OCIBOT_DOMAIN",' in flags
