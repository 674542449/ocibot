"""多出口 IP「一键更换公网 IP」的后台任务。

换一批 IP 要几分钟（先删旧 IP、等 Oracle 释放名额、再申请绑定），不能放在一个
HTTP 请求里等，所以在后台线程里跑。进度存在 AppMeta 里，而不是内存：面板以
2 个 worker 进程运行（run.py），只放内存的话，页面查进度可能落到另一个进程，
看到的是「没有任务」。

存进数据库还有一个用处：旧 IP 删掉之后，它的名字 / 区间 / 地址池就问不到 Oracle
了。任务中途断掉（面板重启）时，下一次补分配靠这份记录沿用原来的名字。

Oracle 那一侧的步骤在 app/oci_client.py 的 plan_multi_ip_rotation / rotate_multi_ips。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.oci_client import safe_error_text
from web.backend.audit import write_audit
from web.backend.db import SessionLocal
from web.backend.meta import get_meta, set_meta

_LOG = logging.getLogger("ocibot.ip_rotation")

KEY_PREFIX = "ip_rotate:"
# 运行中的任务每做一步、每查一轮都会刷新 updated_at。这么久没动静，就是跑它的进程
# 没了（面板重启 / 容器重建）—— 不能让这条记录永远挡住下一次更换。
STALE_SEC = 300

# 页面能看到的条目字段。compartment_id / pool_id / 各种 OCID 只在服务端用。
_ITEM_FIELDS = ("private_ip_id", "private_ip", "old_ip", "new_ip", "name", "status", "message")


class RotationBusy(Exception):
    """同一台实例已有一个更换任务在跑。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_key(tenant_id: str, instance_id: str) -> str:
    # AppMeta.key 最长 64；实例 OCID 本身就比这长，所以取哈希。
    digest = hashlib.sha256(f"{tenant_id}:{instance_id}".encode()).hexdigest()[:40]
    return KEY_PREFIX + digest


def read_job(db: Session, tenant_id: str, instance_id: str) -> Optional[dict[str, Any]]:
    raw = get_meta(db, job_key(tenant_id, instance_id)) or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _age_sec(job: dict[str, Any]) -> float:
    try:
        updated = datetime.fromisoformat(str(job.get("updated_at") or ""))
    except ValueError:
        return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds()


def is_running(job: Optional[dict[str, Any]]) -> bool:
    return bool(job) and job.get("state") == "running" and _age_sec(job) < STALE_SEC


def job_view(job: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """给页面看的样子：去掉服务端字段；进程没了的「运行中」改报「已中断」。"""
    if not job:
        return None
    view = {k: job.get(k) or "" for k in ("state", "phase", "message", "started_at", "updated_at", "finished_at")}
    if job.get("state") == "running" and not is_running(job):
        view["state"] = "interrupted"
        view["message"] = (
            "任务中断了（面板可能重启过）。没有公网 IP 的辅助 IP，勾选后再点「更换公网 IP」即可补上。"
        )
    view["items"] = [
        {**{k: it.get(k) or "" for k in _ITEM_FIELDS}, "same_address": bool(it.get("same_address"))}
        for it in job.get("items") or []
        if isinstance(it, dict)
    ]
    return view


def _save(db: Session, key: str, job: dict[str, Any]) -> None:
    job["updated_at"] = _now_iso()
    set_meta(db, key, json.dumps(job, ensure_ascii=False))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _persist(key: str, job: dict[str, Any]) -> None:
    try:
        with SessionLocal() as db:
            _save(db, key, job)
    except Exception:  # noqa: BLE001 - 进度没存上不能打断正在进行的更换
        _LOG.exception("saving IP rotation progress failed")


def _hints(job: Optional[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """上一次任务记下的名字 / 区间 / 地址池，按私网 IP 索引。"""
    out: dict[str, dict[str, str]] = {}
    for it in (job or {}).get("items") or []:
        if isinstance(it, dict) and it.get("private_ip_id"):
            out[str(it["private_ip_id"])] = {
                "name": str(it.get("name") or ""),
                "compartment_id": str(it.get("compartment_id") or ""),
                "pool_id": str(it.get("pool_id") or ""),
            }
    return out


def start_rotation(
    db: Session,
    *,
    session: Any,
    owner_id: str,
    tenant_id: str,
    instance_id: str,
    compartment_id: str,
    private_ip_ids: list[str],
) -> dict[str, Any]:
    """先在请求里做完只读的规划（选错了当场报），再把更换交给后台线程。"""
    previous = read_job(db, tenant_id, instance_id)
    if is_running(previous):
        raise RotationBusy("这台实例已经有一个更换任务在进行，请等它结束")
    plan = session.plan_multi_ip_rotation(
        instance_id, compartment_id, private_ip_ids, hints=_hints(previous)
    )
    if not plan.ok:
        return {"ok": False, "message": plan.message, "job": job_view(previous)}
    items = list((plan.data or {}).get("items") or [])
    job: dict[str, Any] = {
        "state": "running",
        "phase": "plan",
        "owner_id": owner_id,
        "tenant_id": tenant_id,
        "instance_id": instance_id,
        "started_at": _now_iso(),
        "finished_at": "",
        "message": "",
        "items": items,
    }
    key = job_key(tenant_id, instance_id)
    _save(db, key, job)
    # 线程启动之后它会改 job，所以先把要返回的内容取好。
    view = job_view(job)
    count = sum(1 for it in items if it.get("status") == "pending")
    threading.Thread(
        target=_run, args=(session, key, job), name=f"ip-rotate-{instance_id[-8:]}", daemon=True
    ).start()
    return {"ok": True, "message": f"已开始更换 {count} 个公网 IP", "job": view}


def _run(session: Any, key: str, job: dict[str, Any]) -> None:
    def progress(phase: str, items: list[dict]) -> None:
        job["phase"] = phase
        job["items"] = items
        _persist(key, job)

    try:
        result = session.rotate_multi_ips(job["items"], on_progress=progress)
        job["items"] = (result.data or {}).get("items", job["items"])
        job["state"] = "done" if result.ok else "failed"
        job["message"] = result.message
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("IP rotation crashed for %s", job.get("instance_id"))
        job["state"] = "failed"
        job["message"] = (
            f"更换中断：{safe_error_text(exc)}。没有公网 IP 的辅助 IP，勾选后再点「更换公网 IP」即可补上。"
        )
    job["phase"] = "done"
    job["finished_at"] = _now_iso()
    _persist(key, job)
    try:
        with SessionLocal() as db:
            write_audit(
                db,
                owner_id=job.get("owner_id"),
                action="multi_ip.rotate_done",
                target=str(job.get("instance_id") or ""),
                detail={
                    "tenant_id": job.get("tenant_id"),
                    "ok": job["state"] == "done",
                    "message": job["message"],
                    "changed": [
                        f"{it.get('old_ip') or '—'} → {it.get('new_ip')}"
                        for it in job["items"]
                        if it.get("status") == "done"
                    ][:64],
                },
            )
    except Exception:  # noqa: BLE001
        _LOG.exception("writing IP rotation audit failed")
