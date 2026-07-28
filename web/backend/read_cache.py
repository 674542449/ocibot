"""Short-lived cache for read-only OCI queries.

Pages auto-load their data on entry (0.4.20). Without this, every page visit, tab
switch and back-navigation would be a fresh fan-out against Oracle — and the OCI
API is rate limited, with capacity retry already fighting for that budget: a 429
spent on rendering a list is one the 抢机 loop does not get.

Rules that keep it honest:
  * the caller resolves and authorizes the tenant BEFORE looking here — entries are
    keyed by tenant id only, so serving one to a user who does not own that tenant
    would be a data leak;
  * an explicit 刷新 sends ``force=1`` and bypasses (then refills) the entry;
  * anything that mutates a tenant's resources calls ``invalidate``, so a stale list
    cannot outlive the change that made it wrong;
  * the TTL is short enough that a missed invalidation self-heals.

Per-process, like the SessionManager: with several API workers each keeps its own,
which only means a cache miss, never a wrong answer.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

# Short on purpose: this exists to absorb navigation bursts, not to serve stale data.
DEFAULT_TTL_SEC = 60
_MAX_ENTRIES = 256

# key -> (stored_at_monotonic, value)
_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.RLock()


def _evict_locked(now: float) -> None:
    for key in [k for k, (ts, _) in _CACHE.items() if now - ts >= DEFAULT_TTL_SEC]:
        _CACHE.pop(key, None)
    while len(_CACHE) >= _MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


def cache_key(tenant_id: str, name: str, *parts: Any) -> str:
    return "|".join([tenant_id or "-", name, *[str(p) for p in parts]])


def get_or_load(
    key: str,
    loader: Callable[[], Any],
    *,
    force: bool = False,
    ttl: int = DEFAULT_TTL_SEC,
) -> tuple[Any, int]:
    """Return ``(value, age_seconds)``; ``age_seconds`` is 0 for a fresh load.

    ``loader`` runs outside the lock: it performs network I/O that can take seconds,
    and holding the lock across it would serialize every tenant's page load behind
    the slowest one. Two concurrent misses therefore both load — wasteful but
    correct, and far better than a global stall.
    """
    now = time.monotonic()
    if not force:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1], int(now - hit[0])

    value = loader()
    with _LOCK:
        _evict_locked(time.monotonic())
        _CACHE[key] = (time.monotonic(), value)
    return value, 0


def invalidate(tenant_id: str, name: Optional[str] = None) -> None:
    """Drop cached reads for a tenant (optionally one family) after a mutation."""
    prefix = f"{tenant_id or '-'}|" + (f"{name}|" if name else "")
    with _LOCK:
        for key in [k for k in _CACHE if k.startswith(prefix) or k == prefix.rstrip("|")]:
            _CACHE.pop(key, None)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
