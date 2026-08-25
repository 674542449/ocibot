"""Browser WebSSH terminal — WebSocket bridge to guest SSH.

Credentials arrive only in the first client frame and are never stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError

from web.backend.audit import write_audit
from web.backend.auth import COOKIE_NAME, decode_token
from web.backend.origin_guard import origin_allowed
from web.backend.db import SessionLocal
from web.backend.models import User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row
from web.backend.rate_limit import SlidingWindowLimiter
from web.backend.ssh_bridge import (
    resolve_instance_ssh_target,
    validate_ssh_auth,
)
from web.backend.ssh_hostkey import UNREACHABLE as HOSTKEY_UNREACHABLE
from web.backend.ssh_hostkey import (
    LEARNED,
    known_hosts_for,
    probe_host_key,
    remembered_key_type,
    verify_host_key,
)

log = logging.getLogger("ocibot.webssh")

router = APIRouter(tags=["webssh"])

# Concurrent session limits (process-local).
_MAX_PER_USER = 3
_MAX_PER_INSTANCE = 2
_IDLE_TIMEOUT_SEC = 30 * 60
_AUTH_TIMEOUT_SEC = 60

# Largest single client frame we will accept. uvicorn's ws_max_size defaults to
# 16 MiB and is set at server startup, not from here, so the ceiling that matters
# for this endpoint is enforced in the receive loop. Generous enough for pasting a
# whole script into the terminal, small enough that one frame cannot be a payload.
_MAX_FRAME_BYTES = 1024 * 1024

# Handshake throttle. Every accepted handshake spends ~3 OCI calls (GetInstance +
# ListVnicAttachments + GetVnic) resolving the target, and the concurrency caps
# above bound simultaneous sessions, not the rate of open/close cycles: a serial
# loop that connects, reads "ready" and disconnects never holds two slots at once
# and so used to burn OCI request budget as fast as the network allowed. That
# budget is shared with the capacity retry loop and is the operator's liability
# (CLAUDE.md), so the rate is capped per user here.
_MAX_HANDSHAKES_PER_MIN = 20
_handshake_limiter = SlidingWindowLimiter(max_hits=_MAX_HANDSHAKES_PER_MIN, window_sec=60)

_sessions_lock = asyncio.Lock()
_user_sessions: dict[str, int] = {}
_instance_sessions: dict[str, int] = {}


async def _acquire_user_slot(user_id: str) -> Optional[str]:
    """Reserve one of the caller's own session slots.

    Taken BEFORE the OCI lookup so a burst of parallel handshakes cannot fan out
    into unbounded GetInstance/GetVnic calls. Keyed on the caller, so claiming a
    slot proves nothing about the instance and can only throttle the caller.
    """
    async with _sessions_lock:
        u = _user_sessions.get(user_id, 0)
        if u >= _MAX_PER_USER:
            return f"同时 WebSSH 会话过多（每用户最多 {_MAX_PER_USER} 个）"
        _user_sessions[user_id] = u + 1
        return None


async def _release_user_slot(user_id: str) -> None:
    async with _sessions_lock:
        u = _user_sessions.get(user_id, 0) - 1
        if u <= 0:
            _user_sessions.pop(user_id, None)
        else:
            _user_sessions[user_id] = u


async def _acquire_instance_slot(instance_id: str) -> Optional[str]:
    """Reserve one of the instance's session slots.

    This counter is global across users, so it is deliberately taken only AFTER
    tenant ownership has been proven: otherwise any logged-in user who knows an
    OCID could park two sessions on somebody else's instance and lock its owner
    out of WebSSH.
    """
    async with _sessions_lock:
        i = _instance_sessions.get(instance_id, 0)
        if i >= _MAX_PER_INSTANCE:
            return f"该实例 WebSSH 会话过多（最多 {_MAX_PER_INSTANCE} 个）"
        _instance_sessions[instance_id] = i + 1
        return None


async def _release_instance_slot(instance_id: str) -> None:
    async with _sessions_lock:
        i = _instance_sessions.get(instance_id, 0) - 1
        if i <= 0:
            _instance_sessions.pop(instance_id, None)
        else:
            _instance_sessions[instance_id] = i


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket handshakes (CSWSH).

    CORS does not apply to WebSockets, so a cookie-authenticated WS endpoint must
    check Origin itself — otherwise any website the victim visits could open a
    terminal on their instances. Shares one policy with the REST middleware
    (origin_guard.py) so the two cannot drift apart again.
    """
    return origin_allowed(
        websocket.headers.get("origin") or "",
        host=websocket.headers.get("host") or "",
        forwarded_host=websocket.headers.get("x-forwarded-host") or "",
    )


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
    user_slot_held = False
    instance_slot_held = False
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

        # Throttle + reserve a slot BEFORE the OCI lookup below. _prepare() costs
        # ~3 Oracle API calls, and until these two gates moved above it a client
        # could spin handshakes and spend the tenancy's request budget at network
        # speed with the session caps never engaging.
        allowed, retry_after = _handshake_limiter.check(f"webssh:{user.id}")
        if not allowed:
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "message": f"WebSSH 连接过于频繁，请 {int(retry_after) + 1} 秒后再试",
                },
            )
            await websocket.close(code=4429)
            return

        err = await _acquire_user_slot(user.id)
        if err:
            await _send_json(websocket, {"type": "error", "message": err})
            await websocket.close(code=4409)
            return
        user_slot_held = True

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

        err = await _acquire_instance_slot(instance_id)
        if err:
            await _send_json(websocket, {"type": "error", "message": err})
            await websocket.close(code=4409)
            return
        instance_slot_held = True

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

        if len(raw) > _MAX_FRAME_BYTES:
            # Refuse before json.loads(): parsing a multi-megabyte frame costs far
            # more memory than the frame itself.
            await _send_json(websocket, {"type": "error", "message": "认证帧过大"})
            await websocket.close(code=4413)
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

        # Ask the probe for the key type we already pinned, so a host offering
        # several key types cannot look like a MITM merely because a different
        # algorithm won the negotiation this time (see host_key_alg_order).
        def _pinned_type() -> str:
            with SessionLocal() as db:
                return remembered_key_type(
                    db, owner_id=user.id, instance_id=instance_id, port=port
                )

        prefer_key_type = await asyncio.to_thread(_pinned_type)

        # Verify the host key BEFORE authenticating. probe_host_key() runs only the
        # SSH handshake, so on a mismatch we abort without ever transmitting the
        # user's password or private key.
        try:
            server_key = await probe_host_key(host, port, prefer_key_type=prefer_key_type)
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
            # Only a real fingerprint change is reported as a mismatch; a shut port
            # or a stopped sshd must not be presented as a possible attack.
            is_mismatch = hostkey.verdict != HOSTKEY_UNREACHABLE
            if is_mismatch:
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
                    "code": "hostkey_mismatch" if is_mismatch else "hostkey_unreachable",
                    "expected_fingerprint": hostkey.expected if is_mismatch else "",
                    "actual_fingerprint": hostkey.fingerprint,
                },
            )
            await websocket.close(code=4495 if is_mismatch else 4502)
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
                # encoding=None makes the session byte-transparent. asyncssh
                # otherwise decodes with errors='strict' and raises ProtocolError
                # on the first non-UTF-8 byte — and that is a CONNECTION-level
                # disconnect, not a channel error, so one `cat /bin/ls` or a
                # latin-1 locale killed the whole session.
                #
                # It also removes the in-band signalling hole: because everything
                # from the guest now leaves as a BINARY frame and only the panel's
                # own control messages are TEXT, guest output can no longer forge
                # a {"type":"error","code":"hostkey_mismatch"} frame and talk the
                # user into resetting a TOFU pin that exists to survive exactly
                # that kind of guest compromise.
                encoding=None,
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

        async def _end_session(message: str, code: int) -> None:
            """Tell the browser why the terminal stopped, then close."""
            try:
                await _send_json(websocket, {"type": "error", "message": message})
                await websocket.close(code=code)
            except Exception:  # noqa: BLE001
                pass

        async def _pump_ssh_to_ws() -> None:
            nonlocal last_activity
            assert process is not None
            try:
                while True:
                    data = await process.stdout.read(8192)
                    if not data:
                        # Remote shell exited. Silently breaking left the browser
                        # looking connected until the next keystroke hit a closed
                        # stdin and surfaced as "WebSSH 内部错误"; tell the client.
                        await _end_session("远程会话已结束", 1000)
                        return
                    last_activity = time.monotonic()
                    # Always binary: terminal bytes must never be able to occupy
                    # the TEXT channel the client reads control messages from.
                    await websocket.send_bytes(data)
            except Exception as exc:  # noqa: BLE001
                # A bare `return` here made real faults invisible — the main loop
                # stayed parked in receive() and the browser still looked
                # connected until the next keystroke. Log it and close.
                log.warning("webssh stdout pump ended: %r", exc)
                await _end_session(f"远程会话中断：{exc}", 1011)
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
                    # Merge stderr into the terminal stream (binary, as above).
                    await websocket.send_bytes(data)
            except Exception as exc:  # noqa: BLE001
                log.debug("webssh stderr pump ended: %r", exc)
                return

        async def _idle_watch() -> None:
            while True:
                await asyncio.sleep(30)
                if time.monotonic() - last_activity > _IDLE_TIMEOUT_SEC:
                    await _end_session("空闲超时，已断开 WebSSH", 4408)
                    return
                # 顺带重验凭据。
                #
                # 握手时 _user_from_websocket 把 user 拷成一个 SimpleNamespace 快照,
                # 之后主循环再没碰过数据库。于是「全设备退出」、改密码、管理员禁用
                # 账号,统统关不掉一个**正在跑的 root 终端** —— 而唯一的时限是
                # 30 分钟**空闲**超时,只要终端里有输出(top / tail -f / 一个
                # keep-alive)就永远不空闲,会话时长没有上界。
                # 只读一行 users 表,放在已有的看门狗协程里,不额外起 task。
                try:
                    with SessionLocal() as db:
                        fresh = db.get(User, user.id)
                        revoked = (
                            fresh is None
                            or not bool(fresh.is_active)
                            or int(fresh.token_version or 1) != int(user.token_version)
                        )
                except Exception:  # noqa: BLE001
                    # 数据库读不到不能成为踢人的理由 —— 那会让一次数据库抖动
                    # 断开所有人的终端。下一轮再看。
                    revoked = False
                if revoked:
                    await _end_session("登录状态已失效，终端已断开", 4401)
                    return

        async def _write_stdin(payload: Any) -> bool:
            """Forward client input; False once the remote side is gone."""
            try:
                if isinstance(payload, str):
                    # The channel is byte-transparent now, so a TEXT frame has to
                    # be encoded here. Handing the str straight to a bytes channel
                    # raised TypeError, which was not in this catch and escaped as
                    # a generic "WebSSH 内部错误".
                    payload = payload.encode("utf-8")
                process.stdin.write(payload)
                # Without drain() asyncssh appends every write to the channel's
                # unbounded _send_buf: a client that keeps typing while the remote
                # shell has stopped reading (`sleep 3000` fills the tty buffer and
                # the peer stops granting window) grows the SHARED api process's
                # RSS at network speed until the panel is OOM-killed for everyone.
                # _MAX_PER_USER caps sessions, not bytes. drain() blocks this task
                # on the channel's high-water mark, which in turn stops us reading
                # the socket, which is the only real backpressure available.
                await process.stdin.drain()
                return True
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad: the exception taxonomy here is a minefield —
                # BrokenPipeError/OSError from a dead socket, but also
                # asyncssh.ConnectionLost (not an OSError) and codec errors. Any
                # of them means this session is over; none of them is an internal
                # panel fault worth showing as one.
                log.debug("webssh stdin write ended: %r", exc)
                await _end_session("远程会话已结束", 1000)
                return False

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

                # Bound one frame before touching it. uvicorn's ws_max_size (16 MiB
                # by default) is configured at server start and not from here, so
                # without this the only limit on a single frame is that default.
                # len() on a str counts characters, which is a tight enough proxy.
                frame = message.get("text")
                if frame is None:
                    frame = message.get("bytes")
                if frame is not None and len(frame) > _MAX_FRAME_BYTES:
                    await _send_json(
                        websocket, {"type": "error", "message": "单帧数据过大，已断开"}
                    )
                    await websocket.close(code=4413)
                    break

                if "text" in message and message["text"] is not None:
                    text = message["text"]
                    # Control frames are JSON starting with {. Only the client can
                    # reach this branch — guest output leaves as binary — so the
                    # sniff cannot be triggered by the remote shell.
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
                    if not await _write_stdin(text):
                        break
                elif "bytes" in message and message["bytes"] is not None:
                    # Binary frames are raw keystrokes, forwarded verbatim with no
                    # control-message sniffing. A client that sends input this way
                    # (and control JSON as text) can type a literal
                    # {"type":"resize"} into the shell without it being eaten.
                    if not await _write_stdin(message["bytes"]):
                        break
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
        if user_slot_held and user is not None:
            await _release_user_slot(user.id)
        if instance_slot_held:
            await _release_instance_slot(instance_id)
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
