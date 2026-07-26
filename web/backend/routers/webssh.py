"""Browser WebSSH terminal — WebSocket bridge to guest SSH.

Credentials arrive only in the first client frame and are never stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy import select

from web.backend.audit import write_audit
from web.backend.auth import COOKIE_NAME, decode_token
from web.backend.config import get_settings
from web.backend.db import SessionLocal
from web.backend.models import User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row
from web.backend.ssh_bridge import (
    resolve_instance_ssh_target,
    validate_ssh_auth,
)
from web.backend.ssh_hostkey import (
    LEARNED,
    known_hosts_for,
    probe_host_key,
    verify_host_key,
)

log = logging.getLogger("ocibot.webssh")

router = APIRouter(tags=["webssh"])

# Concurrent session limits (process-local).
_MAX_PER_USER = 3
_MAX_PER_INSTANCE = 2
_IDLE_TIMEOUT_SEC = 30 * 60
_AUTH_TIMEOUT_SEC = 60

_sessions_lock = asyncio.Lock()
_user_sessions: dict[str, int] = {}
_instance_sessions: dict[str, int] = {}


async def _acquire_slot(user_id: str, instance_id: str) -> Optional[str]:
    async with _sessions_lock:
        u = _user_sessions.get(user_id, 0)
        i = _instance_sessions.get(instance_id, 0)
        if u >= _MAX_PER_USER:
            return f"同时 WebSSH 会话过多（每用户最多 {_MAX_PER_USER} 个）"
        if i >= _MAX_PER_INSTANCE:
            return f"该实例 WebSSH 会话过多（最多 {_MAX_PER_INSTANCE} 个）"
        _user_sessions[user_id] = u + 1
        _instance_sessions[instance_id] = i + 1
        return None


async def _release_slot(user_id: str, instance_id: str) -> None:
    async with _sessions_lock:
        u = _user_sessions.get(user_id, 0) - 1
        i = _instance_sessions.get(instance_id, 0) - 1
        if u <= 0:
            _user_sessions.pop(user_id, None)
        else:
            _user_sessions[user_id] = u
        if i <= 0:
            _instance_sessions.pop(instance_id, None)
        else:
            _instance_sessions[instance_id] = i


def _origin_host(origin: str) -> str:
    """Return the host[:port] part of an Origin header value, lowercased."""
    parsed = urlparse((origin or "").strip())
    return (parsed.netloc or "").strip().lower()


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket handshakes (CSWSH).

    CORS does not apply to WebSockets and SameSite=Lax is not a guarantee here
    (SameSite=None is a supported configuration), so a cookie-authenticated WS
    endpoint must check Origin itself — otherwise any website the victim visits
    could open a terminal on their instances.

    A missing Origin is allowed: browsers always send it on a WS handshake, while
    non-browser clients (which do not carry the victim's cookie) often omit it.
    """
    origin = (websocket.headers.get("origin") or "").strip()
    if not origin:
        return True
    origin_host = _origin_host(origin)
    if not origin_host:
        return False
    # Same-origin: compare host[:port] only. Behind a TLS-terminating proxy the
    # browser's Origin is https:// while this hop is plain ws://, so the scheme
    # cannot be compared reliably.
    host_header = (websocket.headers.get("host") or "").strip().lower()
    if host_header and origin_host == host_header:
        return True
    for allowed in get_settings().cors_origin_list():
        if origin_host == _origin_host(allowed):
            return True
    return False


def _user_from_websocket(websocket: WebSocket) -> Any:
    # Cookie only — never accept JWT from query string (leaks via logs/Referer).
    token = websocket.cookies.get(COOKIE_NAME) or ""
    if not token:
        # Optional Authorization: Bearer for non-browser clients (not in query).
        auth_header = (websocket.headers.get("authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        raise PermissionError("未登录")
    try:
        payload = decode_token(token)
    except JWTError as exc:
        raise PermissionError("令牌无效或已过期") from exc
    user_id = payload.get("sub")
    if not user_id:
        raise PermissionError("无效令牌")
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise PermissionError("用户不存在或已禁用")
        token_ver = int(payload.get("ver") or 1)
        if token_ver != int(user.token_version or 1):
            raise PermissionError("登录状态已失效，请重新登录")
        # Copy fields we need after the session closes (avoid detached ORM use).
        from types import SimpleNamespace

        return SimpleNamespace(  # type: ignore[return-value]
            id=user.id,
            username=user.username,
            is_active=bool(user.is_active),
            is_admin=bool(user.is_admin),
            token_version=int(user.token_version or 1),
        )


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


@router.websocket("/tenants/{tenant_id}/instances/{instance_id}/webssh")
async def webssh_endpoint(websocket: WebSocket, tenant_id: str, instance_id: str) -> None:
    # Cross-site handshakes are refused before the socket is even accepted, so a
    # malicious page cannot ride the victim's session cookie into a shell.
    if not websocket_origin_allowed(websocket):
        log.warning("webssh rejected cross-origin handshake: %r", websocket.headers.get("origin"))
        await websocket.close(code=4403)
        return

    await websocket.accept()
    user: Optional[Any] = None
    slot_held = False
    ssh_conn = None
    process = None
    started = time.monotonic()

    try:
        try:
            user = _user_from_websocket(websocket)
        except PermissionError as exc:
            await _send_json(websocket, {"type": "error", "message": str(exc)})
            await websocket.close(code=4401)
            return

        # Ownership + resolve target (sync OCI in thread)
        def _prepare():
            with SessionLocal() as db:
                row = get_owned_tenant(db, user.id, tenant_id)
                session = get_session_for_row(row)
                target = resolve_instance_ssh_target(session, instance_id)
                return target, row.name

        try:
            target, tenant_name = await asyncio.to_thread(_prepare)
        except LookupError:
            await _send_json(websocket, {"type": "error", "message": "租户不存在"})
            await websocket.close(code=4404)
            return
        except Exception as exc:  # noqa: BLE001
            await _send_json(websocket, {"type": "error", "message": str(exc)})
            await websocket.close(code=4400)
            return

        if str(target.lifecycle_state or "").upper() not in {"", "RUNNING"}:
            # Allow empty unknown; warn on non-running
            if str(target.lifecycle_state or "").upper() not in {"RUNNING"}:
                await _send_json(
                    websocket,
                    {
                        "type": "error",
                        "message": f"实例状态为 {target.lifecycle_state or '未知'}，WebSSH 需要 RUNNING",
                    },
                )
                await websocket.close(code=4400)
                return

        err = await _acquire_slot(user.id, instance_id)
        if err:
            await _send_json(websocket, {"type": "error", "message": err})
            await websocket.close(code=4409)
            return
        slot_held = True

        await _send_json(
            websocket,
            {
                "type": "ready",
                "message": "请发送认证信息（私钥或密码仅用于本次会话，不会保存）",
                "host_hint": target.public_ip or target.private_ip,
                "instance_name": target.display_name,
            },
        )

        # First frame: auth JSON
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await _send_json(websocket, {"type": "error", "message": "等待认证超时"})
            await websocket.close(code=4408)
            return

        try:
            auth_msg = json.loads(raw)
        except json.JSONDecodeError:
            await _send_json(websocket, {"type": "error", "message": "首帧必须是 JSON 认证信息"})
            await websocket.close(code=4400)
            return

        try:
            auth = validate_ssh_auth(
                username=str(auth_msg.get("username") or "ubuntu"),
                private_key_pem=auth_msg.get("private_key_pem"),
                password=auth_msg.get("password"),
                port=int(auth_msg.get("port") or 22),
            )
        except ValueError as exc:
            await _send_json(websocket, {"type": "error", "message": str(exc)})
            await websocket.close(code=4400)
            return

        cols = max(20, min(int(auth_msg.get("cols") or 120), 500))
        rows = max(5, min(int(auth_msg.get("rows") or 40), 200))
        host = target.host
        port = auth["port"]

        import asyncssh

        # Verify the host key BEFORE authenticating. probe_host_key() runs only the
        # SSH handshake, so on a mismatch we abort without ever transmitting the
        # user's password or private key.
        try:
            server_key = await probe_host_key(host, port)
        except Exception as exc:  # noqa: BLE001
            await _send_json(
                websocket, {"type": "error", "message": f"无法读取 SSH 主机密钥：{exc}"}
            )
            await websocket.close(code=4502)
            return

        def _check():
            with SessionLocal() as db:
                return verify_host_key(
                    db,
                    owner_id=user.id,
                    instance_id=instance_id,
                    port=port,
                    server_key=server_key,
                    host=host,
                    tenant_id=tenant_id,
                )

        hostkey = await asyncio.to_thread(_check)
        if not hostkey.ok:
            with SessionLocal() as db:
                write_audit(
                    db,
                    owner_id=user.id,
                    action="webssh.hostkey_mismatch",
                    target=instance_id,
                    detail={
                        "tenant_id": tenant_id,
                        "host": host,
                        "expected": hostkey.expected,
                        "actual": hostkey.fingerprint,
                    },
                )
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "message": hostkey.message(),
                    "code": "hostkey_mismatch",
                    "expected_fingerprint": hostkey.expected,
                    "actual_fingerprint": hostkey.fingerprint,
                },
            )
            await websocket.close(code=4495)
            return
        if hostkey.verdict == LEARNED:
            await _send_json(
                websocket,
                {
                    "type": "hostkey",
                    "message": hostkey.message(),
                    "fingerprint": hostkey.fingerprint,
                },
            )

        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "username": auth["username"],
            # Pin the exact key we just verified so the authenticated connection
            # cannot land on a different host than the probe did.
            "known_hosts": known_hosts_for(server_key),
            "login_timeout": 30,
        }
        if auth["private_key_pem"]:
            try:
                connect_kwargs["client_keys"] = [asyncssh.import_private_key(auth["private_key_pem"])]
            except Exception:
                await _send_json(websocket, {"type": "error", "message": "SSH 私钥无法解析"})
                await websocket.close(code=4400)
                return
        else:
            connect_kwargs["password"] = auth["password"]

        # Drop credential references ASAP
        auth_msg.clear()
        private_key_gone = auth.pop("private_key_pem", None)
        password_gone = auth.pop("password", None)
        del private_key_gone, password_gone

        try:
            ssh_conn = await asyncssh.connect(**connect_kwargs)
            process = await ssh_conn.create_process(
                term_type="xterm-256color",
                term_size=(cols, rows),
            )
        except Exception as exc:  # noqa: BLE001
            await _send_json(websocket, {"type": "error", "message": f"SSH 连接失败：{exc}"})
            await websocket.close(code=4502)
            return
        finally:
            connect_kwargs.pop("password", None)
            connect_kwargs.pop("client_keys", None)

        with SessionLocal() as db:
            write_audit(
                db,
                owner_id=user.id,
                action="webssh.connect",
                target=instance_id,
                detail={
                    "tenant_id": tenant_id,
                    "tenant_name": tenant_name,
                    "host": host,
                    "username": auth["username"],
                    "auth_mode": auth["auth_mode"],
                    "port": port,
                },
            )

        await _send_json(websocket, {"type": "connected", "host": host, "username": auth["username"]})

        last_activity = time.monotonic()

        async def _pump_ssh_to_ws() -> None:
            nonlocal last_activity
            assert process is not None
            try:
                while True:
                    data = await process.stdout.read(8192)
                    if not data:
                        break
                    last_activity = time.monotonic()
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
            except Exception:
                return

        async def _pump_stderr() -> None:
            nonlocal last_activity
            assert process is not None
            try:
                while True:
                    data = await process.stderr.read(4096)
                    if not data:
                        break
                    last_activity = time.monotonic()
                    # Merge stderr into terminal stream
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
            except Exception:
                return

        async def _idle_watch() -> None:
            nonlocal last_activity
            while True:
                await asyncio.sleep(30)
                if time.monotonic() - last_activity > _IDLE_TIMEOUT_SEC:
                    try:
                        await _send_json(websocket, {"type": "error", "message": "空闲超时，已断开 WebSSH"})
                        await websocket.close(code=4408)
                    except Exception:
                        pass
                    return

        ssh_task = asyncio.create_task(_pump_ssh_to_ws())
        err_task = asyncio.create_task(_pump_stderr())
        idle_task = asyncio.create_task(_idle_watch())

        try:
            while True:
                message = await websocket.receive()
                last_activity = time.monotonic()
                mtype = message.get("type")
                if mtype == "websocket.disconnect":
                    break
                if "text" in message and message["text"] is not None:
                    text = message["text"]
                    # Control frames are JSON starting with {
                    if text.startswith("{") and text.endswith("}"):
                        try:
                            ctrl = json.loads(text)
                        except json.JSONDecodeError:
                            ctrl = None
                        if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                            c = max(20, min(int(ctrl.get("cols") or cols), 500))
                            r = max(5, min(int(ctrl.get("rows") or rows), 200))
                            try:
                                process.change_terminal_size(c, r)
                            except Exception:
                                pass
                            continue
                        if isinstance(ctrl, dict) and ctrl.get("type") == "ping":
                            await _send_json(websocket, {"type": "pong"})
                            continue
                    process.stdin.write(text)
                elif "bytes" in message and message["bytes"] is not None:
                    process.stdin.write(message["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            for t in (ssh_task, err_task, idle_task):
                t.cancel()
            try:
                if process is not None:
                    process.stdin.write_eof()
            except Exception:
                pass
            try:
                if process is not None:
                    process.close()
                    await process.wait_closed()
            except Exception:
                pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("webssh error: %s", exc)
        try:
            await _send_json(websocket, {"type": "error", "message": "WebSSH 内部错误"})
        except Exception:
            pass
    finally:
        try:
            if ssh_conn is not None:
                ssh_conn.close()
                await ssh_conn.wait_closed()
        except Exception:
            pass
        if slot_held and user is not None:
            await _release_slot(user.id, instance_id)
        if user is not None:
            try:
                with SessionLocal() as db:
                    write_audit(
                        db,
                        owner_id=user.id,
                        action="webssh.disconnect",
                        target=instance_id,
                        detail={
                            "tenant_id": tenant_id,
                            "duration_sec": round(time.monotonic() - started, 1),
                        },
                    )
            except Exception:
                pass
