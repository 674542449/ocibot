"""Inbound cross-site request policy (CSRF / CSWSH).

One policy, two callers: the REST middleware in ``main.py`` and the WebSSH
handshake in ``routers/webssh.py``. They used to disagree — the WebSocket checked
``Origin`` and the REST API did not — which left the state-changing endpoints
resting entirely on the ``SameSite`` cookie attribute.

``SameSite=Lax`` (the default) is not sufficient on its own:

* ``SameSite=none`` is a supported configuration here, and it disables the
  browser-side protection completely.
* Even on Lax, cookies are shared across *sites*, not origins. A panel on
  ``panel.example.com`` and an attacker-controlled ``blog.example.com`` are the
  same site, so the session cookie rides along on a cross-origin POST from that
  other host.

So the server checks ``Origin`` itself. Browsers send it on every
POST/PUT/PATCH/DELETE (same-origin included, per fetch), which is exactly the set
of requests that change state.

A missing ``Origin`` is allowed: non-browser clients (curl, scripts) omit it and
they do not carry a victim's cookie, so rejecting them would break automation
without gaining anything.
"""

from __future__ import annotations

from urllib.parse import urlparse

from web.backend.config import get_settings

# Requests that can change state. GET/HEAD/OPTIONS are excluded: OPTIONS is the
# CORS preflight (CORSMiddleware answers it) and no GET route here mutates.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def origin_host(origin: str) -> str:
    """Return the host[:port] part of an Origin header value, lowercased."""
    parsed = urlparse((origin or "").strip())
    return (parsed.netloc or "").strip().lower()


# Ports that carry no information because they are implied by the scheme. A
# browser omits them from Origin ("https://panel.example.com", never ":443")
# while a proxy may or may not keep them in Host, so they must be normalized away
# before comparing — otherwise a perfectly ordinary nginx that forwards
# `Host: panel.example.com:443` fails every write with a 403.
_DEFAULT_PORTS = {"80", "443"}
_SCHEME_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


def _split_netloc(netloc: str, *, scheme: str = "") -> tuple[str, str]:
    """Return (hostname, port) with a scheme-default port normalized to ''.

    Only the trailing ``:digits`` is treated as a port, so an IPv6 literal
    ("[::1]") keeps its colons.
    """
    netloc = (netloc or "").strip().lower()
    hostname, port = netloc, ""
    head, sep, tail = netloc.rpartition(":")
    if sep and tail.isdigit():
        # "[::1]" alone leaves tail="1]", which is not all digits, so an IPv6
        # literal without a port is not mistaken for one.
        hostname, port = head, tail
    if port and (port == _SCHEME_PORTS.get(scheme) or (not scheme and port in _DEFAULT_PORTS)):
        port = ""
    return hostname, port


def _same_origin(source: tuple[str, str], candidate: str) -> bool:
    """Compare a parsed origin against a Host-style ``host[:port]`` value.

    The port is compared only when the candidate actually carries one. A proxy
    using nginx's `$host` strips it, and rejecting on a port the proxy withheld
    would lock the operator out of their own panel — the hostname match is what
    carries the security value here.
    """
    if not candidate:
        return False
    c_host, c_port = _split_netloc(candidate)
    if not c_host or c_host != source[0]:
        return False
    return not c_port or c_port == source[1]


def origin_allowed(origin: str, *, host: str = "", forwarded_host: str = "") -> bool:
    """True when `origin` may make a credentialed state-changing request.

    Compares hostname (and port where known), never the scheme: behind a
    TLS-terminating proxy the browser's Origin is ``https://`` while this hop is
    plain ``http://``, so a scheme comparison would reject every legitimate
    request.

    `forwarded_host` (X-Forwarded-Host) is consulted as well because a reverse
    proxy may pass the upstream name in Host instead of the public domain. Both
    are attacker-suppliable headers, but that is not a weakness here: the check
    exists to stop a *browser* being used as a confused deputy, and a browser
    cannot forge either header. A non-browser client that sets them arbitrarily
    could just as easily omit Origin entirely.
    """
    origin = (origin or "").strip()
    if not origin:
        return True  # not a browser-initiated cross-site request
    parsed = urlparse(origin)
    source = _split_netloc(parsed.netloc or "", scheme=(parsed.scheme or "").lower())
    if not source[0]:
        return False  # malformed / opaque origin ("null" from a sandboxed frame)
    for candidate in (host, forwarded_host):
        if _same_origin(source, (candidate or "").strip().lower()):
            return True
    for allowed in get_settings().cors_origin_list():
        allowed_parsed = urlparse(allowed.strip())
        if source == _split_netloc(
            allowed_parsed.netloc or "", scheme=(allowed_parsed.scheme or "").lower()
        ):
            return True
    return False
