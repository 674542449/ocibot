"""Backup / restore encrypted tenant ZIP (web-side)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config_store import (
    BACKUP_ENVELOPE_VERSION,
    TenantConfig,
    decode_backup_payload,
    encode_backup_payload,
)
from web.backend.auth import get_current_user
from web.backend.crypto_util import decrypt_text, encrypt_text
from web.backend.db import get_db
from web.backend.models import Tenant, User
from web.backend.audit import write_audit
from web.backend.uploads import read_upload_limited

router = APIRouter(prefix="/backup", tags=["backup"])

# 这份归档 = 名下每一个甲骨文租户的**可直接使用的 API 私钥**。拿到它的人不需要
# 面板、不需要账号密码、不需要两步验证，直接就能调 OCI API 开机、删机、看账单。
# 界面上必须把这句话说清楚——它不是「配置备份」，它是凭据本身。
EXPORT_WARNING = (
    "此文件包含所有租户的甲骨文 API 私钥明文，"
    "持有它等同于持有这些甲骨文账号本身（无需面板账号或两步验证即可直接调用 OCI API）。"
    "请使用长口令，妥善离线保存，不要放在网盘 / 下载目录 / 聊天记录里。"
)

# 口令下限。原来是 6 位、无复杂度要求，而 zip 那层 WinZip AES 的 KDF 被规范
# 钉死在 PBKDF2-**SHA1、1000 轮**（为了 7-Zip/WinRAR 互通，改不了），hashcat
# 13600 模式一块普通显卡 ~10 MH/s——6 位小写+数字是分钟级的事。现在 zip 里面
# 还套了一层 390 000 轮 SHA256 的信封，但 KDF 再强也救不回一个 6 位口令，
# 所以下限本身必须抬上来。
_MIN_EXPORT_PASSWORD = 12
# 导入侧原本是 max_length=512、导出侧完全没有上限：一个 600 字符的长口令能
# 导出成功，回头导入时被自己的校验拒掉——工具做出了自己打不开的文件。两边取同一个值。
_MAX_PASSWORD = 512
# 导入侧的下限只能停在 6：所有 0.4.x 早期归档都是用 6 位口令做的，抬高下限
# 等于宣布那些备份作废，而备份作废只会在真要用它的那天被发现。
_MIN_IMPORT_PASSWORD = 6

# 归档字段 -> tenants 表列宽。原来只有 name/description/color/password_changed_at
# 四个字段被截断，user_ocid / tenancy_ocid / fingerprint / region / compartment_ocid
# 是原样落库的，而 cfg.validate() 对它们只管格式、不管长度。SQLite 会默默溢出，
# PostgreSQL 直接 raise —— 而 db.flush() 在循环里、没有任何保护，所以一行坏数据
# 会把此前已经恢复好的全部租户一起回滚掉，返回一个空白 500。
# 这里不做静默截断：截短一个 OCID 只会存下一份「看起来配好了、其实连不上」的
# 凭据，跳过并说明原因才是诚实的。
_FIELD_LIMITS: dict[str, int] = {
    "user_ocid": 128,
    "tenancy_ocid": 128,
    "fingerprint": 128,
    "region": 64,
    "compartment_ocid": 128,
}


class RestoreResult(BaseModel):
    imported: int
    tenant_ids: list[str] = Field(default_factory=list)
    message: str = ""
    # 原来只有 imported，跳过的行既不计数也不说明原因，于是「已导入 200 个租户」
    # 可以在一个都不可用的情况下照样返回。
    skipped: int = 0
    skipped_reasons: list[str] = Field(default_factory=list)


class ExportRequest(BaseModel):
    # 下限交给下面的 _password_strength_error 说人话（Field 的 min_length 只会
    # 给出一条 422 的 pydantic 报错，操作者看不懂该改什么）。
    password: str = Field(max_length=_MAX_PASSWORD)


def _password_strength_error(password: str) -> str:
    """Why this export password is not good enough, or "" if it is."""
    if len(password) < _MIN_EXPORT_PASSWORD:
        return (
            f"备份密码至少需要 {_MIN_EXPORT_PASSWORD} 位。{EXPORT_WARNING}"
        )
    classes = 0
    classes += any(c.islower() for c in password)
    classes += any(c.isupper() for c in password)
    classes += any(c.isdigit() for c in password)
    classes += any(not c.isalnum() for c in password)
    if classes < 2:
        return "备份密码请至少混合两类字符（小写字母 / 大写字母 / 数字 / 符号）"
    if len(set(password)) < 5:
        return "备份密码重复字符过多，请换一个更随机的口令"
    return ""


def _notice_header(payload: dict[str, Any]) -> str:
    """Percent-encoded JSON for the response header.

    The body is the zip itself, so anything the UI needs to say about the export
    (how many rows were skipped, and the warning about what the file is) has to
    ride along in a header. HTTP headers are latin-1 only and this text is
    Chinese, hence the encoding — the browser side does
    ``JSON.parse(decodeURIComponent(h))``.
    """
    return quote(json.dumps(payload, ensure_ascii=False))


@router.post("/export")
def export_encrypted_zip(
    body: ExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    """Download an encrypted ZIP of all tenants, **including their private keys**.

    The resulting file is equivalent to the Oracle tenancies themselves: anyone
    holding it and the password can call the OCI API as the account owner without
    ever touching this panel. Treat it as the credential it is.

    Two layers of protection, on purpose. The zip is WinZip AES-256 so ordinary
    tools can open it, but that format pins PBKDF2-HMAC-**SHA1 at 1000 rounds**
    for interoperability, which is far too cheap to be the only barrier around
    plaintext private keys. Inside the zip the payload is therefore sealed again
    with PBKDF2-HMAC-SHA256 at 390 000 rounds (format version 2).

    Password is accepted in the POST body only — never as a query string
    (query strings land in access logs / browser history).

    Rows whose stored key cannot be decrypted (the master key was rotated) are
    exported as a named skip rather than aborting the whole download: this
    endpoint IS the rescue action for that incident, so it must not be the second
    thing that breaks in it.
    """
    import pyzipper

    password = (body.password or "").strip()
    strength_error = _password_strength_error(password)
    if strength_error:
        raise HTTPException(status_code=400, detail=strength_error)

    rows = db.scalars(select(Tenant).where(Tenant.owner_id == user.id).order_by(Tenant.name)).all()
    if not rows:
        raise HTTPException(status_code=400, detail="没有可备份的租户")

    tenants_payload: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        try:
            pem = decrypt_text(row.private_key_encrypted) if row.private_key_encrypted else ""
        except ValueError as exc:
            # crypto_util.decrypt_text raises ValueError when OCIBOT_MASTER_KEY no
            # longer matches the stored ciphertext, and main.py registers no handler
            # for it — so this used to be a blank 500 that killed the export of every
            # healthy row along with the broken one.
            skipped.append({"id": row.id, "name": row.name, "reason": str(exc)})
            continue
        tenants_payload.append(
            {
                "id": row.id,
                "name": row.name,
                "user_ocid": row.user_ocid,
                "tenancy_ocid": row.tenancy_ocid,
                "fingerprint": row.fingerprint,
                "region": row.region,
                "compartment_ocid": row.compartment_ocid or "",
                "description": row.description or "",
                "enabled": bool(row.enabled),
                "color": row.color or "#3B82F6",
                "private_key_pem": pem,
                "password_changed_at": row.password_changed_at or "",
                "password_expiry_days": int(row.password_expiry_days or 0),
                "account_tier": row.account_tier or "",
                "free_only_mode": bool(getattr(row, "free_only_mode", True)),
                # 副区 link. Ids are reissued on restore, so it is remapped there
                # via the exported "id" above.
                "parent_tenant_id": getattr(row, "parent_tenant_id", "") or "",
            }
        )
    if not tenants_payload:
        names = "、".join(item["name"] for item in skipped[:10])
        raise HTTPException(
            status_code=500,
            detail=(
                f"所有 {len(skipped)} 个租户的私钥都无法解密（{names}），备份内容会是空的。"
                "这通常是 OCIBOT_MASTER_KEY 变了或 .env 没有加载：请先恢复原来的主密钥再导出。"
            ),
        )

    content = encode_backup_payload(
        {"version": 1, "tenants": tenants_payload}, password
    )

    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("tenants.json", content)
        # Whoever opens this archive months from now is the person who most needs
        # to be told what they are holding, and they are not looking at the panel.
        zf.writestr(
            "README-first.txt",
            (
                "OCIBot 租户备份\n"
                "================\n\n"
                f"{EXPORT_WARNING}\n\n"
                f"tenants.json 为加密信封（格式版本 {BACKUP_ENVELOPE_VERSION}）：\n"
                "  PBKDF2-HMAC-SHA256 / 390000 轮 + Fernet，口令与本 zip 相同。\n"
                "  只能通过面板的「导入备份」还原，用解压工具打开只会看到密文。\n"
                f"\n本备份包含 {len(tenants_payload)} 个租户。\n"
                # 漏掉的租户必须写进压缩包本身。
                #
                # 这份清单以前只出现在 X-OCIBot-Backup-Notice 响应头里，而前端一个
                # 读它的地方都没有 —— 主密钥轮换之后，导出会安静地少掉几个租户，
                # 界面照样显示「备份已下载」。几个月后拿着这个文件去恢复的人，正是
                # 最需要知道「少了谁」的人，而他手边只有这个 zip。
                + (
                    "".join(
                        [
                            f"\n⚠ 另有 {len(skipped)} 个租户**未包含**在本备份中"
                            "（私钥无法解密，通常是 OCIBOT_MASTER_KEY 变了）：\n"
                        ]
                        + [f"  - {item['name']}（{item['reason']}）\n" for item in skipped]
                        + ["恢复这些租户需要原来的主密钥，或者重新填一次 API Key。\n"]
                    )
                    if skipped
                    else ""
                )
            ).encode("utf-8"),
        )
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"ocibot-backup-{stamp}.zip"
    write_audit(
        db,
        owner_id=user.id,
        action="backup.export",
        target=filename,
        detail={
            "tenant_count": len(tenants_payload),
            "skipped": [item["name"] for item in skipped],
            "format": BACKUP_ENVELOPE_VERSION,
        },
    )
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-OCIBot-Backup-Notice": _notice_header(
                {
                    "format": BACKUP_ENVELOPE_VERSION,
                    "exported": len(tenants_payload),
                    "skipped": skipped,
                    "warning": EXPORT_WARNING,
                }
            ),
        },
    )


@router.post("/import", response_model=RestoreResult)
def import_encrypted_zip(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form(max_length=_MAX_PASSWORD)],
    file: UploadFile = File(...),
) -> RestoreResult:
    """Restore tenants from an AES ZIP.

    Reads both archive formats: the version-2 encrypted envelope written today,
    and the plain JSON that every archive made before it used. Dropping the old
    format would invalidate every backup an operator already holds, and they
    would only find out on the day they needed one.

    Sync handler on purpose: ZIP decryption, JSON parsing and the per-tenant
    re-encryption are all blocking, so FastAPI runs this in its threadpool
    instead of stalling the event loop for every other request.
    """
    import pyzipper

    password = (password or "").strip()
    if len(password) < _MIN_IMPORT_PASSWORD:
        raise HTTPException(status_code=400, detail=f"备份密码至少需要 {_MIN_IMPORT_PASSWORD} 位")
    # Hard cap: 20 MiB encrypted backup is already huge for tenant JSON. Bounded
    # read so an oversized upload is rejected instead of being buffered whole.
    raw_bytes = read_upload_limited(
        file, 20 * 1024 * 1024, too_large_detail="备份文件过大（上限 20MB）"
    )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="空文件")

    _MAX_INFLATED = 5 * 1024 * 1024
    try:
        with pyzipper.AESZipFile(io.BytesIO(raw_bytes), "r") as zf:
            zf.setpassword(password.encode("utf-8"))
            names = zf.namelist()
            if len(names) > 64:
                raise HTTPException(status_code=400, detail="备份文件条目过多")
            member = "tenants.json" if "tenants.json" in names else (names[0] if names else "")
            if not member:
                raise HTTPException(status_code=400, detail="备份文件为空")
            # Cheap early-out on the declared size — but it is only a hint: file_size
            # comes from the central directory and is attacker-controlled (pyzipper
            # never cross-checks it against the actual stream), so it cannot be the
            # bound. zf.read() would inflate in 1 GiB chunks and only THEN truncate
            # to file_size, so a ~200KB upload declaring 1KB could still materialize
            # hundreds of MB. Bound the decompression itself instead.
            info = zf.getinfo(member)
            if int(getattr(info, "file_size", 0) or 0) > _MAX_INFLATED:
                raise HTTPException(status_code=400, detail="备份内容过大（解压后上限 5MB）")
            with zf.open(member) as fh:
                raw = fh.read(_MAX_INFLATED + 1)
            if len(raw) > _MAX_INFLATED:
                raise HTTPException(status_code=400, detail="备份内容过大（解压后上限 5MB）")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="无法解密备份文件，请检查密码是否正确") from exc

    try:
        data = decode_backup_payload(raw, password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items = data.get("tenants", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="备份内容格式无效")
    if len(items) > 200:
        raise HTTPException(status_code=400, detail="单次备份租户过多（上限 200）")

    def _clean_tier(raw: Any) -> str:
        tier = str(raw or "").strip().lower()
        return tier if tier in {"free", "paid"} else ""

    def _expiry_days(raw: Any) -> int:
        """Archive-supplied value clamped to the Integer column's safe range."""
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 120
        return max(0, min(value, 36500))

    imported_ids: list[str] = []
    skipped_reasons: list[str] = []
    skipped = 0

    def _skip(label: str, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(skipped_reasons) < 50:
            skipped_reasons.append(f"{label}：{reason}")

    # archive id -> newly issued row id, so 副区 rows can be re-linked to their
    # primary after every row exists (ids are reissued, never restored as-is).
    id_map: dict[str, str] = {}
    parent_of: dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            _skip(f"第 {index + 1} 条", "不是一个租户对象")
            continue
        archive_id = str(item.get("id") or "")
        archive_parent = str(item.get("parent_tenant_id") or "")
        # Never honor attacker-chosen owner/id from the archive.
        item.pop("owner_id", None)
        label = str(item.get("name") or item.get("profile") or f"第 {index + 1} 条")[:64]
        # Validate via TenantConfig
        try:
            cfg = TenantConfig(
                id=str(uuid4()),
                name=str(item.get("name") or item.get("profile") or "导入租户")[:128],
                user_ocid=str(item.get("user_ocid") or item.get("user") or ""),
                tenancy_ocid=str(item.get("tenancy_ocid") or item.get("tenancy") or ""),
                fingerprint=str(item.get("fingerprint") or ""),
                region=str(item.get("region") or "ap-tokyo-1"),
                private_key_pem=str(item.get("private_key_pem") or item.get("key_content") or ""),
                compartment_ocid=str(item.get("compartment_ocid") or item.get("compartment_id") or ""),
                description=str(item.get("description") or "")[:512],
                enabled=bool(item.get("enabled", True)),
                color=str(item.get("color") or "#3B82F6")[:32],
                password_changed_at=str(item.get("password_changed_at") or "")[:64],
                password_expiry_days=_expiry_days(item.get("password_expiry_days") or 120),
                # Normalize at ingest: an arbitrary string from the archive used to
                # flow into the quota guard, where anything unrecognized turned the
                # Always-Free caps off.
                account_tier=_clean_tier(item.get("account_tier")),
            )
        except Exception as exc:  # noqa: BLE001
            _skip(label, f"字段无法读取（{exc.__class__.__name__}）")
            continue

        errors = cfg.validate()
        if errors:
            _skip(label, "；".join(errors)[:300])
            continue
        # Width check before the INSERT, not after: see _FIELD_LIMITS.
        too_long = [
            f"{field} 超过 {limit} 字符"
            for field, limit in _FIELD_LIMITS.items()
            if len(str(getattr(cfg, field, "") or "")) > limit
        ]
        if too_long:
            _skip(label, "；".join(too_long))
            continue

        row = Tenant(
            owner_id=user.id,
            name=cfg.name,
            user_ocid=cfg.user_ocid,
            tenancy_ocid=cfg.tenancy_ocid,
            fingerprint=cfg.fingerprint,
            region=cfg.region,
            compartment_ocid=cfg.compartment_ocid or "",
            description=cfg.description or "",
            enabled=cfg.enabled,
            color=cfg.color or "#3B82F6",
            private_key_encrypted=encrypt_text(cfg.private_key_pem),
            password_changed_at=cfg.password_changed_at or "",
            password_expiry_days=int(cfg.password_expiry_days or 0),
            account_tier=cfg.account_tier or "",
            # Default ON when absent (older archives) so a restore never
            # silently loses the free-tier protection.
            free_only_mode=bool(item.get("free_only_mode", True)),
        )
        # SAVEPOINT per row. A bare db.flush() here shares one transaction with
        # every row before it, so a single rejected INSERT rolled back the whole
        # restore and returned a blank 500 — while every other kind of bad row in
        # this loop is merely skipped. Now the rejection is confined to its own
        # row and reported like the rest.
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except SQLAlchemyError as exc:
            reason = str(getattr(exc, "orig", None) or exc).strip().splitlines()
            _skip(label, f"数据库拒绝写入：{reason[0][:200] if reason else exc.__class__.__name__}")
            continue

        imported_ids.append(row.id)
        if archive_id:
            id_map[archive_id] = row.id
        if archive_parent:
            parent_of[row.id] = archive_parent

    # Second pass: a 副区 whose primary is missing from the archive stays unlinked
    # rather than pointing at a row that does not exist.
    for child_id, archive_parent in parent_of.items():
        new_parent = id_map.get(archive_parent, "")
        if new_parent and new_parent != child_id:
            child = db.get(Tenant, child_id)
            if child is not None:
                child.parent_tenant_id = new_parent

    db.commit()
    write_audit(
        db,
        owner_id=user.id,
        action="backup.import",
        target=file.filename or "upload.zip",
        detail={"imported": len(imported_ids), "skipped": skipped},
    )
    message = f"已导入 {len(imported_ids)} 个租户"
    if skipped:
        # 不把跳过藏起来：一次「已导入 200 个租户」而其中一个都不可用，
        # 操作者只会在下一次真的要用它们时才发现。
        message += f"，跳过 {skipped} 个"
    return RestoreResult(
        imported=len(imported_ids),
        tenant_ids=imported_ids,
        message=message,
        skipped=skipped,
        skipped_reasons=skipped_reasons,
    )
