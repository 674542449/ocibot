#!/usr/bin/env python3
"""Production entrypoint: run uvicorn with a configurable worker count."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    workers = max(1, int(os.environ.get("OCIBOT_API_WORKERS", "2") or 2))
    # WebSSH session limits are per-process; multiple workers still serve HTTP fine.
    uvicorn.run(
        "web.backend.main:app",
        host=os.environ.get("OCIBOT_HOST", "0.0.0.0"),
        port=int(os.environ.get("OCIBOT_PORT", "8000") or 8000),
        workers=workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=30,
        ws="websockets",
    )


if __name__ == "__main__":
    main()
