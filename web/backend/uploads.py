"""Bounded reads for multipart uploads.

``UploadFile.read()`` with no argument pulls the whole body into a single bytes
object *before* any size check can run, so a large upload from an authenticated
user turns into memory pressure on the API process. Read in chunks and stop as
soon as the limit is exceeded.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

_CHUNK = 64 * 1024


def read_upload_limited(file: UploadFile, max_bytes: int, *, too_large_detail: str) -> bytes:
    """Read at most ``max_bytes`` from ``file``; raise HTTP 400 past the limit.

    Synchronous on purpose: call it from a sync (threadpool) route handler so the
    read and everything after it stays off the event loop. Starlette has already
    buffered the multipart body by the time the handler runs, so this only bounds
    how much of it we materialize in memory.
    """
    limit = max(1, int(max_bytes))
    chunks: list[bytes] = []
    total = 0
    source = file.file
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=400, detail=too_large_detail)
        chunks.append(chunk)
    return b"".join(chunks)
