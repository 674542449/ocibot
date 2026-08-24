"""SSH host key verification (trust on first use).

Every guest SSH path used ``known_hosts=None``, i.e. no host key verification at
all, so anything able to answer on the instance's address could impersonate it
and collect the SSH credentials the user typed. The original reason for skipping
verification was that OCI addresses rotate — so this keys the remembered
fingerprint on the *instance OCID* instead of the address, which survives IP
changes while still detecting a swapped host.

The probe uses ``asyncssh.get_server_host_key()``, which completes only the key
exchange: the fingerprint is checked *before* any credential is transmitted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from web.backend.models import SshHostKey

log = logging.getLogger("ocibot.hostkey")

# Verdicts from verify_host_key()
TRUSTED = "trusted"  # matches the remembered fingerprint
LEARNED = "learned"  # nothing remembered yet; stored now
MISMATCH = "mismatch"  # differs from what we remembered — refuse to continue
UNREACHABLE = "unreachable"  # could not read a key at all (port shut, sshd down)


@dataclass
class HostKeyCheck:
    verdict: str
    fingerprint: str
    key_type: str = ""
    expected: str = ""
    expected_key_type: str = ""
    server_key: Any = None

    @property
    def ok(self) -> bool:
        return self.verdict in (TRUSTED, LEARNED)

    def message(self) -> str:
        if self.verdict == LEARNED:
            return f"已记住该实例的 SSH 主机密钥指纹：{self.fingerprint}"
        if self.verdict == TRUSTED:
            return ""
        if self.verdict == UNREACHABLE:
            # Connectivity problem, NOT evidence of tampering. Saying "possible
            # MITM" here would train users to dismiss the real warning.
            return (
                f"无法读取 SSH 主机密钥：{self.expected}\n"
                "请确认实例已 RUNNING、22 端口已在 NSG/安全列表放行，且 sshd 已启动。"
            )
        type_note = ""
        if self.expected_key_type and self.key_type and self.expected_key_type != self.key_type:
            # The probe asks for the pinned key type first, so a different type
            # coming back means the server stopped offering the old one. Say so —
            # otherwise a key-type rotation reads as nothing but "possible MITM".
            type_note = (
                f"密钥类型也变了（记录 {self.expected_key_type}，本次 {self.key_type}），"
                "服务器可能已不再提供原类型的主机密钥。\n"
            )
        return (
            "SSH 主机密钥与首次连接时不一致，已中止连接（未发送任何凭据）。\n"
            f"记录的指纹：{self.expected}\n"
            f"本次的指纹：{self.fingerprint}\n"
            f"{type_note}"
            "若你重装了系统或重建了实例，请在实例详情页重置主机密钥后重连；"
            "否则这可能是中间人攻击。"
        )


def _default_host_key_algs() -> list[str]:
    try:
        from asyncssh.public_key import get_default_public_key_algs

        return [
            a.decode("ascii", "replace") if isinstance(a, bytes) else str(a)
            for a in get_default_public_key_algs()
        ]
    except Exception:  # noqa: BLE001
        return []


def _sig_algs_for_key_type(key_type: str) -> list[str]:
    """Signature algorithms that make the server present a ``key_type`` host key.

    Needed because the pinned *key* type and the negotiated *signature* algorithm
    are not the same string: an RSA host key is stored as ``ssh-rsa`` but modern
    sshd only offers it under ``rsa-sha2-256`` / ``rsa-sha2-512``, so asking for
    ``ssh-rsa`` alone would fail the KEX against the very server we pinned.
    """
    kt = (key_type or "").strip()
    if not kt:
        return []
    handler = None
    try:
        # asyncssh has no public alg -> key-class lookup; guarded so an upstream
        # rename degrades to "just ask for the key type itself", never a crash.
        from asyncssh.public_key import _public_key_alg_map

        handler = _public_key_alg_map.get(kt.encode("ascii", "replace"))
    except Exception:  # noqa: BLE001
        handler = None
    out: list[str] = []
    for alg in getattr(handler, "sig_algorithms", ()) or ():
        out.append(alg.decode("ascii", "replace") if isinstance(alg, bytes) else str(alg))
    if kt not in out:
        # ECDSA (and anything unmapped): the key algorithm IS the signature algorithm.
        out.append(kt)
    return out


def host_key_alg_order(prefer_key_type: str = "") -> list[str]:
    """Client host-key preference putting the already-pinned key type first.

    Only one fingerprint is remembered per (owner, instance, port), so whichever
    key type asyncssh happened to negotiate first became "the" identity of the
    host. A dual-key Ubuntu box (RSA + ed25519) then flips to MISMATCH — i.e. the
    「这可能是中间人攻击」 banner — for entirely benign reasons: sshd's
    HostKeyAlgorithms edited, one of the two keys regenerated, or an asyncssh
    upgrade reordering get_default_public_key_algs(). The last one would trip
    EVERY pinned instance at once right after the panel's own one-click update,
    which is exactly how users get trained to click through a real warning.

    Re-requesting the remembered type keeps the comparison like-for-like. The rest
    of the defaults stay in the list behind it so a server that genuinely dropped
    that key type still completes the KEX and is reported as a mismatch, rather
    than failing the handshake and being mislabelled "unreachable".
    """
    defaults = _default_host_key_algs()
    preferred = [
        a for a in _sig_algs_for_key_type(prefer_key_type) if not defaults or a in defaults
    ]
    if not preferred:
        return []  # nothing pinned yet (or unrecognised): use asyncssh's own order
    return preferred + [a for a in defaults if a not in preferred]


async def probe_host_key(
    host: str, port: int = 22, *, timeout: float = 20.0, prefer_key_type: str = ""
) -> Any:
    """Return the server's host key without authenticating.

    Only the SSH handshake runs, so no username, password or private key is sent.
    """
    import asyncio

    import asyncssh

    kwargs: dict[str, Any] = {"port": int(port or 22)}
    algs = host_key_alg_order(prefer_key_type)
    if algs:
        kwargs["server_host_key_algs"] = algs
    return await asyncio.wait_for(asyncssh.get_server_host_key(host, **kwargs), timeout=timeout)


def remembered_key_type(db: Session, *, owner_id: str, instance_id: str, port: int = 22) -> str:
    """Key type pinned for this target, so the probe can ask for the same one."""
    row = db.scalar(
        select(SshHostKey).where(
            SshHostKey.owner_id == owner_id,
            SshHostKey.instance_id == instance_id,
            SshHostKey.port == int(port or 22),
        )
    )
    return str(getattr(row, "key_type", "") or "") if row is not None else ""


def fingerprint_of(server_key: Any) -> tuple[str, str]:
    """(fingerprint, key_type) for a probed key."""
    if server_key is None:
        return "", ""
    try:
        fingerprint = str(server_key.get_fingerprint())
    except Exception:  # noqa: BLE001
        fingerprint = ""
    key_type = ""
    for attr in ("algorithm", "get_algorithm"):
        value = getattr(server_key, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                value = None
        if value:
            key_type = value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
            break
    return fingerprint, key_type


def verify_host_key(
    db: Session,
    *,
    owner_id: str,
    instance_id: str,
    port: int,
    server_key: Any,
    host: str = "",
    tenant_id: str = "",
) -> HostKeyCheck:
    """Compare a probed key against the remembered one, learning it on first use."""
    fingerprint, key_type = fingerprint_of(server_key)
    if not fingerprint:
        # Cannot identify the key (e.g. a KEX method with no host key). Treat as
        # untrusted rather than silently proceeding.
        return HostKeyCheck(verdict=MISMATCH, fingerprint="", expected="(无法读取主机密钥)")

    row = db.scalar(
        select(SshHostKey).where(
            SshHostKey.owner_id == owner_id,
            SshHostKey.instance_id == instance_id,
            SshHostKey.port == int(port or 22),
        )
    )
    if row is None:
        db.add(
            SshHostKey(
                owner_id=owner_id,
                tenant_id=tenant_id or "",
                instance_id=instance_id,
                port=int(port or 22),
                fingerprint=fingerprint,
                key_type=key_type,
                last_host=host or "",
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # Two first-time connections raced (two tabs, or a double click) and
            # both saw no row. The unique constraint on
            # (owner_id, instance_id, port) rejects the loser, which would other-
            # wise surface as an internal error on a perfectly legitimate action.
            # Re-read and treat the winner's row as authoritative.
            db.rollback()
            row = db.scalar(
                select(SshHostKey).where(
                    SshHostKey.owner_id == owner_id,
                    SshHostKey.instance_id == instance_id,
                    SshHostKey.port == int(port or 22),
                )
            )
            if row is None:
                # Constraint fired but nothing is there — do not guess, fail closed.
                return HostKeyCheck(
                    verdict=MISMATCH, fingerprint=fingerprint, expected="(主机密钥记录写入冲突)"
                )
        else:
            log.info("hostkey learned instance=%s fp=%s", instance_id, fingerprint)
            return HostKeyCheck(
                verdict=LEARNED, fingerprint=fingerprint, key_type=key_type, server_key=server_key
            )

    if row.fingerprint != fingerprint:
        log.warning(
            "hostkey MISMATCH instance=%s expected=%s got=%s",
            instance_id,
            row.fingerprint,
            fingerprint,
        )
        return HostKeyCheck(
            verdict=MISMATCH,
            fingerprint=fingerprint,
            key_type=key_type,
            expected=row.fingerprint,
            expected_key_type=str(row.key_type or ""),
            server_key=server_key,
        )

    dirty = False
    if host and row.last_host != host:
        row.last_host = host
        dirty = True
    if key_type and not row.key_type:
        # Rows learned before the probe asked for a specific type have no type on
        # record; fill it in so the next probe can request the same one.
        row.key_type = key_type
        dirty = True
    if dirty:
        db.commit()
    return HostKeyCheck(
        verdict=TRUSTED, fingerprint=fingerprint, key_type=key_type, server_key=server_key
    )


def check_instance_host_key(
    db: Session,
    *,
    owner_id: str,
    instance_id: str,
    host: str,
    port: int = 22,
    tenant_id: str = "",
    timeout: float = 20.0,
) -> HostKeyCheck:
    """Probe + verify from synchronous (threadpool) route handlers."""
    import asyncio

    # Ask for the key type we already pinned, so a dual-key host cannot flip to
    # MISMATCH just because the negotiated algorithm changed (see host_key_alg_order).
    prefer = remembered_key_type(db, owner_id=owner_id, instance_id=instance_id, port=port)

    async def _probe() -> Any:
        return await probe_host_key(host, port, timeout=timeout, prefer_key_type=prefer)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                server_key = pool.submit(lambda: asyncio.run(_probe())).result(
                    timeout=timeout + 10
                )
        else:
            server_key = asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001
        # Still not OK (we will not connect without a verified key), but reported as
        # a reachability failure rather than as tampering.
        return HostKeyCheck(verdict=UNREACHABLE, fingerprint="", expected=str(exc))

    return verify_host_key(
        db,
        owner_id=owner_id,
        instance_id=instance_id,
        port=port,
        server_key=server_key,
        host=host,
        tenant_id=tenant_id,
    )


def forget_host_key(db: Session, *, owner_id: str, instance_id: str, port: Optional[int] = None) -> int:
    """Drop remembered key(s) so the next connection re-learns. Returns rows removed."""
    stmt = select(SshHostKey).where(
        SshHostKey.owner_id == owner_id, SshHostKey.instance_id == instance_id
    )
    if port is not None:
        stmt = stmt.where(SshHostKey.port == int(port))
    rows = list(db.scalars(stmt).all())
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def known_hosts_for(server_key: Any) -> tuple[list[Any], list[Any], list[Any]]:
    """asyncssh ``known_hosts`` value pinning exactly the verified key.

    asyncssh accepts (trusted_host_keys, trusted_ca_keys, revoked_keys) and then
    enforces it during the handshake, so the authenticated connection cannot end
    up on a different host than the one we probed.
    """
    return ([server_key], [], []) if server_key is not None else ([], [], [])
