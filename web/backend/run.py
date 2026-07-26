#!/usr/bin/env python3
"""Production entrypoint: run uvicorn with a configurable worker count."""

from __future__ import annotations

import os


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    import uvicorn

    workers = max(1, int(os.environ.get("OCIBOT_API_WORKERS", "2") or 2))

    # SECURITY: uvicorn's ProxyHeadersMiddleware *overwrites* scope["client"] from
    # X-Forwarded-For for every peer it trusts. With forwarded_allow_ips="*" any
    # direct client could forge that header, so request.client.host became
    # attacker-controlled — which silently defeated the login/register rate limiter
    # (a fresh bucket per forged IP) even though OCIBOT_TRUST_PROXY defaults to 0.
    # Only honour proxy headers when the operator opted in, and only from the
    # addresses they nominate (loopback by default).
    trust_proxy = _truthy(os.environ.get("OCIBOT_TRUST_PROXY"))
    forwarded_allow_ips = (os.environ.get("OCIBOT_FORWARDED_ALLOW_IPS") or "").strip()
    if not forwarded_allow_ips:
        forwarded_allow_ips = "127.0.0.1,::1"

    # WebSSH session limits are per-process; multiple workers still serve HTTP fine.
    uvicorn.run(
        "web.backend.main:app",
        host=os.environ.get("OCIBOT_HOST", "0.0.0.0"),
        port=int(os.environ.get("OCIBOT_PORT", "8000") or 8000),
        workers=workers,
        proxy_headers=trust_proxy,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_keep_alive=30,
        ws="websockets",
    )


if __name__ == "__main__":
    main()
