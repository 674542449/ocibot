"""给脚本调用的 API（用 API 密钥，不用登录）。只能做「多出口 IP」相关的事。

    GET  /api/v1/tenants/{租户}/instances/{实例}/multi-ips                 列出辅助 IP 和公网 IP
    POST /api/v1/tenants/{租户}/instances/{实例}/multi-ips/{私网IP}/rotate  换这一个私网 IP 的公网 IP
    POST /api/v1/tenants/{租户}/instances/{实例}/multi-ips/rotate-all      一键换全部
    GET  /api/v1/tenants/{租户}/instances/{实例}/multi-ips/rotate          查进度 / 结果

请求头带 ``Authorization: Bearer <密钥>``。更换和网页上的「更换公网 IP」是同一套
（web/backend/ip_rotation.py）：先删旧保留 IP、等 Oracle 释放名额、再申请新的绑回同一个
私网 IP；同一台实例同时只跑一个。两个 rotate 接口可以带 ``?wait=秒数``（最多 600），
等它换完再返回，脚本里一条命令就能拿到「旧地址 → 新地址」。

网页更换时会核对「确认框里看到的公网 IP」；这里不核对 —— 脚本说的就是「把这个私网 IP
现在绑的公网 IP 换掉」，不管现在是哪个。
"""

from __future__ import annotations

import ipaddress
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.oci_client import _name_then_ip_key, safe_error_text
from web.backend import ip_rotation
from web.backend.api_keys import ApiCaller, get_api_caller
from web.backend.audit import write_audit
from web.backend.db import SessionLocal, get_db
from web.backend.oci_bridge import get_session_for_row
from web.backend.routers.instance_ops import _require_paid, _row

router = APIRouter(prefix="/v1", tags=["open-api"])

Caller = Annotated[ApiCaller, Depends(get_api_caller)]
Db = Annotated[Session, Depends(get_db)]
Wait = Annotated[int, Query(ge=0, le=600, description="等更换结束再返回，最多等这么多秒；0 = 立即返回")]

_BASE = "/tenants/{tenant_id}/instances/{instance_id}/multi-ips"
# 页面能看到的条目字段；OCID 之类的不给脚本。
_ITEM_FIELDS = ("private_ip", "old_ip", "new_ip", "name", "status", "message", "same_address")
_POLL_SEC = 2.0


def _session(db: Session, caller: ApiCaller, tenant_id: str):
    row = _row(db, caller.user.id, tenant_id)
    _require_paid(db, row)
    # 建会话会解密租户私钥、解析配置，失败时要和其他接口一样回 502 + 原因，而不是一个裸 500。
    try:
        return get_session_for_row(row)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=safe_error_text(exc)) from exc


def _job_out(view: dict | None) -> dict | None:
    if not view:
        return None
    return {**{k: view.get(k) for k in ("state", "phase", "message", "started_at", "finished_at")},
            "items": [{k: it.get(k) for k in _ITEM_FIELDS} for it in view.get("items") or []]}


def _wait(tenant_id: str, instance_id: str, seconds: int, view: dict | None) -> dict | None:
    """在请求里等任务结束（每 2 秒读一次面板数据库，不请求 Oracle）。"""
    deadline = time.monotonic() + seconds
    while view and view.get("state") == "running" and time.monotonic() < deadline:
        time.sleep(min(_POLL_SEC, max(0.0, deadline - time.monotonic())))
        # 每次用新的会话读：同一个会话在一个事务里可能一直读到旧的快照。
        with SessionLocal() as fresh:
            view = ip_rotation.job_view(ip_rotation.read_job(fresh, tenant_id, instance_id))
    return view


def _start(
    db: Session, caller: ApiCaller, tenant_id: str, instance_id: str, pick: Any, wait: int, label: str
) -> dict[str, Any]:
    session = _session(db, caller, tenant_id)
    try:
        info = session.get_instance(instance_id, resolve_ips=False)
        ids = session.secondary_private_ip_ids(instance_id, info.compartment_id)
        private_ip_ids = pick(ids)
        out = ip_rotation.start_rotation(
            db,
            session=session,
            owner_id=caller.user.id,
            tenant_id=tenant_id,
            instance_id=instance_id,
            compartment_id=info.compartment_id,
            private_ip_ids=private_ip_ids,
        )
    except HTTPException:
        raise
    except ip_rotation.RotationBusy as exc:
        raise HTTPException(status_code=409, detail=safe_error_text(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=safe_error_text(exc)) from exc
    write_audit(
        db,
        owner_id=caller.user.id,
        action="api.multi_ip.rotate",
        target=instance_id,
        detail={"tenant_id": tenant_id, "key": caller.key.prefix, "what": label,
                "ok": out["ok"], "message": out["message"]},
    )
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["message"])
    view = _wait(tenant_id, instance_id, wait, out["job"]) if wait else out["job"]
    state = (view or {}).get("state") or ""
    # 等到了结果就照结果说：脚本多半只看 ok，换失败（或任务中断）还报 true 会被当成换好了。
    # 没等或没等到（running）时 ok 只表示「已开始」。
    message = out["message"]
    if state != "running" and (view or {}).get("message"):
        message = view["message"]
    return {"ok": state in ("running", "done"), "message": message, "job": _job_out(view)}


@router.get(_BASE)
def api_list_multi_ips(tenant_id: str, instance_id: str, caller: Caller, db: Db) -> dict[str, Any]:
    """辅助私网 IP 和各自绑的公网 IP，顺序和面板「多出口 IP」表一样（按公网 IP 名字）。"""
    session = _session(db, caller, tenant_id)
    try:
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.list_multi_ips(instance_id, info.compartment_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=safe_error_text(exc)) from exc
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.message)
    data = result.data if isinstance(result.data, dict) else {}
    items = sorted(
        data.get("items", []),
        key=lambda i: _name_then_ip_key(i.get("public_ip_name") or "", i.get("private_ip") or ""),
    )
    return {
        "ok": True,
        "primary_private_ip": data.get("primary_private_ip", ""),
        "items": [
            {
                "private_ip": i.get("private_ip", ""),
                "public_ip": i.get("public_ip", ""),
                "name": i.get("public_ip_name", ""),
                "managed": bool(i.get("managed")),
            }
            for i in items
        ],
    }


@router.post(_BASE + "/{private_ip}/rotate")
def api_rotate_one(
    tenant_id: str, instance_id: str, private_ip: str, caller: Caller, db: Db, wait: Wait = 0
) -> dict[str, Any]:
    """换这一个辅助私网 IP 的公网 IP（私网 IP 不变）。没有公网 IP 的就直接分配一个。"""
    try:
        address = str(ipaddress.IPv4Address(private_ip.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"不是有效的 IPv4 地址：{private_ip}") from exc

    def pick(ids: dict[str, str]) -> list[str]:
        if address not in ids:
            raise HTTPException(
                status_code=404,
                detail=f"这台实例主网卡上没有辅助私网 IP {address}（主私网 IP 不能换）",
            )
        return [ids[address]]

    return _start(db, caller, tenant_id, instance_id, pick, wait, address)


@router.post(_BASE + "/rotate-all")
def api_rotate_all(tenant_id: str, instance_id: str, caller: Caller, db: Db, wait: Wait = 0) -> dict[str, Any]:
    """一键换这台实例全部辅助私网 IP 的公网 IP。"""

    def pick(ids: dict[str, str]) -> list[str]:
        if not ids:
            raise HTTPException(status_code=400, detail="这台实例没有辅助私网 IP")
        return list(ids.values())

    return _start(db, caller, tenant_id, instance_id, pick, wait, "all")


@router.get(_BASE + "/rotate")
def api_rotation_status(tenant_id: str, instance_id: str, caller: Caller, db: Db) -> dict[str, Any]:
    """最近一次更换的进度 / 结果。只读面板数据库，不请求 Oracle。"""
    _row(db, caller.user.id, tenant_id)
    return {"ok": True, "job": _job_out(ip_rotation.job_view(ip_rotation.read_job(db, tenant_id, instance_id)))}
