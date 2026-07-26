"""Request-body size ceiling enforced on bytes actually received.

Checking Content-Length alone is not enough: a client may send
``Transfer-Encoding: chunked`` with no Content-Length, in which case Starlette
happily consumes and spools the entire body to a temp file before any route code
runs. This is a pure ASGI middleware (not BaseHTTPMiddleware) so it can wrap
``receive`` and count the real stream, aborting as soon as the cap is passed.

Why this has to live in middleware: FastAPI parses the multipart body
(``await request.form()`` in fastapi/routing.py) *before* it resolves
dependencies, so ``Depends(get_current_user)`` has not run yet — an
unauthenticated request already reaches the parser. The same ordering means the
ceiling is also what bounds the in-memory field flood: Starlette caps each
non-file part at 1MB but keeps every finished part, with max_fields defaulting to
1000, so without a total cap one request could retain ~1GB. FastAPI 0.139 offers
no per-route way to pass max_files/max_fields into request.form(), so the byte
ceiling here is the enforcement point for both the disk and memory variants.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


class BodyTooLarge(Exception):
    """Raised from the wrapped receive() once the byte cap is exceeded."""


class BodySizeLimitMiddleware:
    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Cheap path: an honest Content-Length over the cap is refused without
        # reading a single byte of the body.
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _send_413(send, self.max_bytes)
            return

        received = 0
        limit = self.max_bytes
        started = False
        too_large = False
        replaced = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    # Raising here stops the body from being consumed any further,
                    # which is the point: no more of it reaches the disk spool.
                    too_large = True
                    raise BodyTooLarge()
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started, replaced
            kind = message.get("type")
            if kind == "http.response.start":
                if too_large:
                    # Starlette's multipart parser catches our exception and turns
                    # it into a generic 400 "error parsing the body". Rewrite that
                    # into the accurate 413 before anything reaches the client.
                    replaced = True
                    await _send_413(send, self.max_bytes)
                    return
                started = True
            elif kind == "http.response.body" and replaced:
                return  # the app's body for the response we replaced
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            # The exception escaped the app (no parser swallowed it) and nothing
            # has been sent yet, so a clean 413 is still possible.
            if not started and not replaced:
                await _send_413(send, self.max_bytes)


def _declared_length(scope: dict[str, Any]) -> int | None:
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def _send_413(send: Callable[[dict[str, Any]], Awaitable[None]], max_bytes: int) -> None:
    body = json.dumps(
        {"detail": f"请求体过大（上限 {max_bytes // (1024 * 1024)}MB）"},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
