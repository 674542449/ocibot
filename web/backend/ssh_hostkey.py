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
        return (
            "SSH 主机密钥与首次连接时不一致，已中止连接（未发送任何凭据）。\n"
            f"记录的指纹：{self.expected}\n"
            f"本次的指纹：{self.fingerprint}\n"
            "若你重装了系统或重建了实例，请在实例详情页重置主机密钥后重连；"
            "否则这可能是中间人攻击。"
        )


async def probe_host_key(host: str, port: int = 22, *, timeout: float = 20.0) -> Any:
    """Return the server's host key without authenticating.

    Only the SSH handshake runs, so no username, password or private key is sent.
    """
    import asyncio

    import asyncssh

    return await asyncio.wait_for(
        asyncssh.get_server_host_key(host, port=int(port or 22)), timeout=timeout
    )


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
            server_key=server_key,
        )

    if host and row.last_host != host:
        row.last_host = host
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

    async def _probe() -> Any:
        return await probe_host_key(host, port, timeout=timeout)

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
