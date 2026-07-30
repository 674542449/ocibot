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


def origin_allowed(origin: str, *, host: str = "", forwarded_host: str = "") -> bool:
    """True when `origin` may make a credentialed state-changing request.

    Compares host[:port] only, never the scheme: behind a TLS-terminating proxy
    the browser's Origin is ``https://`` while this hop is plain ``http://``, so a
    scheme comparison would reject every legitimate request.

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
    source = origin_host(origin)
    if not source:
        return False  # malformed / opaque origin ("null" from a sandboxed frame)
    for candidate in (host, forwarded_host):
        candidate = (candidate or "").strip().lower()
        if candidate and source == candidate:
            return True
    for allowed in get_settings().cors_origin_list():
        if source == origin_host(allowed):
            return True
    return False
