"""Tenant management routes."""

from __future__ import annotations

import logging

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config_store import _REGION_RE, TenantConfig
from app.formatting import region_area
from web.backend.audit import write_audit
from web.backend.auth import get_current_user
from web.backend.crypto_util import encrypt_text
from web.backend.db import get_db
from web.backend.launch_service import clear_launch_meta_cache
from web.backend.models import Tenant, User
from web.backend.oci_bridge import drop_session, get_owned_tenant, get_session_for_row
from web.backend.schemas import (
    OciPasswordPolicyOut,
    RegionSubscribeRequest,
    RegionSubscribeResult,
    TenantCreate,
    TenantOut,
    TenantParseResult,
    TenantPasteImport,
    TenantRegionItem,
    TenantRegionsOut,
    TenantTestResult,
    TenantUpdate,
)
from web.backend.tenant_import import parse_pasted_oci_bundle

router = APIRouter(prefix="/tenants", tags=["tenants"])

log = logging.getLogger("ocibot.tenants")


def _commit_new_tenant(db: Session, row: Tenant) -> None:
    """Commit a tenant INSERT, turning a database rejection into a message.

    Without this a rejected INSERT is an empty `500 Internal Server Error` in the
    browser, and the only copy of the reason is a traceback in the container log.
    That happened for real: 0.4.36 unmapped three NOT NULL columns that every
    upgraded database still has, so adding a tenant failed a not-null constraint
    while every read and update kept working. Diagnosing it needed shell access to
    a server the operator was not looking at.
    """
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("tenant insert rejected by the database")
        reason = str(getattr(exc, "orig", None) or exc).strip().splitlines()
        raise HTTPException(
            status_code=500,
            detail=(
                "保存租户失败，数据库拒绝写入："
                + (reason[0][:300] if reason else exc.__class__.__name__)
                + "。这通常说明数据库结构与当前版本不一致；请把这句话和 "
                "/api/health 显示的版本号一起反馈。"
            ),
        ) from exc


def _to_out(row: Tenant) -> TenantOut:
    return TenantOut(
        id=row.id,
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        compartment_ocid=row.compartment_ocid or "",
        description=row.description or "",
        enabled=bool(row.enabled),
        color=row.color or "#3B82F6",
        has_private_key=bool(row.private_key_encrypted),
        account_tier=row.account_tier or "",
        free_only_mode=bool(getattr(row, "free_only_mode", True)),
        parent_tenant_id=getattr(row, "parent_tenant_id", "") or "",
        region_label=region_area(row.region or ""),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_fields(
    *,
    name: str,
    user_ocid: str,
    tenancy_ocid: str,
    fingerprint: str,
    region: str,
    private_key_pem: str,
    compartment_ocid: str = "",
) -> None:
    cfg = TenantConfig(
        id="validate",
        name=name,
        user_ocid=user_ocid,
        tenancy_ocid=tenancy_ocid,
        fingerprint=fingerprint,
        region=region,
        private_key_pem=private_key_pem,
        compartment_ocid=compartment_ocid or "",
    )
    errors = cfg.validate()
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))


@router.get("", response_model=list[TenantOut])
def list_tenants(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[TenantOut]:
    # Insertion order, not alphabetical: the operator knows their tenants by the
    # order they added them, and sorting by name made a rename jump a row across
    # the table. `id` only breaks ties so the order is deterministic when two rows
    # share a timestamp (a paste-import that creates 主区 + 副区 together).
    rows = db.scalars(
        select(Tenant)
        .where(Tenant.owner_id == user.id)
        .order_by(Tenant.created_at, Tenant.id)
    ).all()
    return [_to_out(r) for r in rows]


@router.post("/parse", response_model=TenantParseResult)
def parse_tenant_paste(
    body: TenantPasteImport,
    user: Annotated[User, Depends(get_current_user)],
) -> TenantParseResult:
    """Parse pasted OCI config text without saving. Does not return private key material."""
    _ = user
    result = parse_pasted_oci_bundle(
        body.api_text,
        private_key_pem=body.private_key_pem,
        name_override=body.name,
        description=body.description,
        compartment_override=body.compartment_ocid,
    )
    return TenantParseResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        name=str(result.get("name") or ""),
        user_ocid=str(result.get("user_ocid") or ""),
        tenancy_ocid=str(result.get("tenancy_ocid") or ""),
        fingerprint=str(result.get("fingerprint") or ""),
        region=str(result.get("region") or ""),
        compartment_ocid=str(result.get("compartment_ocid") or ""),
        has_private_key=bool(result.get("has_private_key")),
        key_file_hint=str(result.get("key_file_hint") or ""),
        warnings=list(result.get("warnings") or []),
    )


@router.post("/import", response_model=TenantOut, status_code=201)
def import_tenant_from_paste(
    body: TenantPasteImport,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    """Paste OCI config (+ PEM) and create a tenant in one step."""
    result = parse_pasted_oci_bundle(
        body.api_text,
        private_key_pem=body.private_key_pem,
        name_override=body.name,
        description=body.description,
        compartment_override=body.compartment_ocid,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "解析失败")

    _validate_fields(
        name=str(result["name"]),
        user_ocid=str(result["user_ocid"]),
        tenancy_ocid=str(result["tenancy_ocid"]),
        fingerprint=str(result["fingerprint"]),
        region=str(result["region"]),
        private_key_pem=str(result["private_key_pem"]),
        compartment_ocid=str(result.get("compartment_ocid") or ""),
    )

    row = Tenant(
        owner_id=user.id,
        name=str(result["name"]).strip(),
        user_ocid=str(result["user_ocid"]).strip(),
        tenancy_ocid=str(result["tenancy_ocid"]).strip(),
        fingerprint=str(result["fingerprint"]).strip(),
        region=str(result["region"]).strip(),
        compartment_ocid=str(result.get("compartment_ocid") or "").strip(),
        description=str(result.get("description") or body.description or "").strip(),
        enabled=True,
        color="#3B82F6",
        private_key_encrypted=encrypt_text(str(result["private_key_pem"]).strip()),
    )
    _commit_new_tenant(db, row)

    if body.test_connection:
        try:
            session = get_session_for_row(row)
            test = session.test_connection()
            if not test.ok:
                pass
        except Exception:
            pass

    return _to_out(row)


@router.post("", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    _validate_fields(
        name=body.name,
        user_ocid=body.user_ocid,
        tenancy_ocid=body.tenancy_ocid,
        fingerprint=body.fingerprint,
        region=body.region,
        private_key_pem=body.private_key_pem,
        compartment_ocid=body.compartment_ocid,
    )
    row = Tenant(
        owner_id=user.id,
        name=body.name.strip(),
        user_ocid=body.user_ocid.strip(),
        tenancy_ocid=body.tenancy_ocid.strip(),
        fingerprint=body.fingerprint.strip(),
        region=body.region.strip(),
        compartment_ocid=(body.compartment_ocid or "").strip(),
        description=(body.description or "").strip(),
        enabled=body.enabled,
        color=body.color or "#3B82F6",
        private_key_encrypted=encrypt_text(body.private_key_pem.strip()),
        free_only_mode=bool(body.free_only_mode),
    )
    _commit_new_tenant(db, row)
    return _to_out(row)


@router.get("/{tenant_id}", response_model=TenantOut)
def get_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_out(row)


@router.patch("/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    data = body.model_dump(exclude_unset=True)
    new_pem = data.pop("private_key_pem", None)

    for key, value in data.items():
        if value is not None:
            setattr(row, key, value if not isinstance(value, str) else value.strip())

    from web.backend.crypto_util import decrypt_text

    if isinstance(new_pem, str) and new_pem.strip():
        pem = new_pem.strip()
    else:
        # decrypt_text raises ValueError when OCIBOT_MASTER_KEY no longer matches
        # the stored ciphertext, and main.py registers no handler for it — so
        # editing anything at all about a tenant (even just its colour) answered a
        # blank 500 after a master-key rotation, with nothing saying why. The user
        # can get out of it from this very form by pasting the key again, so say so.
        try:
            pem = decrypt_text(row.private_key_encrypted)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{exc}。请在本次编辑中重新粘贴该租户的私钥，"
                    "或先把 OCIBOT_MASTER_KEY 恢复成保存这条记录时使用的值。"
                ),
            ) from exc
    _validate_fields(
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        private_key_pem=pem,
        compartment_ocid=row.compartment_ocid,
    )
    if isinstance(new_pem, str) and new_pem.strip():
        row.private_key_encrypted = encrypt_text(new_pem.strip())

    # 副区 rows hold a COPY of this row's credentials — they are the same Oracle
    # API key by construction. Without this, rotating the primary's key (or fixing
    # a fingerprint/user OCID) silently left every secondary region authenticating
    # with the old one until it was noticed as a 401.
    children = list(
        db.scalars(
            select(Tenant).where(
                Tenant.owner_id == user.id,
                Tenant.parent_tenant_id == row.id,
            )
        ).all()
    )
    for child in children:
        child.user_ocid = row.user_ocid
        child.tenancy_ocid = row.tenancy_ocid
        child.fingerprint = row.fingerprint
        child.private_key_encrypted = row.private_key_encrypted
        child.updated_at = datetime.now(timezone.utc)

    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    # Cache eviction strictly AFTER the commit. Dropping first leaves a window in
    # which a concurrent request rebuilds the session from the not-yet-committed
    # row and re-caches the OLD credentials, which would then outlive the update.
    for target in [row, *children]:
        drop_session(target.id)
        # Region / compartment may have changed, so the launch metadata cached for
        # this tenant no longer describes it.
        clear_launch_meta_cache(target.id)
    return _to_out(row)


def _root_tenant(db: Session, row: Tenant) -> Tenant:
    """The primary row of this tenancy — 副区 rows hang off it, never off each other."""
    parent_id = getattr(row, "parent_tenant_id", "") or ""
    if not parent_id:
        return row
    parent = db.get(Tenant, parent_id)
    return parent if parent is not None and parent.owner_id == row.owner_id else row


def _tenancy_rows(db: Session, row: Tenant) -> list[Tenant]:
    """Every panel row this owner has for the same Oracle tenancy."""
    return list(
        db.scalars(
            select(Tenant).where(
                Tenant.owner_id == row.owner_id,
                Tenant.tenancy_ocid == row.tenancy_ocid,
            )
        ).all()
    )


@router.get("/{tenant_id}/regions", response_model=TenantRegionsOut)
def list_tenant_regions(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantRegionsOut:
    """Subscribed regions (副区) plus the regions still available to subscribe.

    Hits Oracle, so it is only called when the user opens the 副区 panel — the
    tenant list itself stays offline like the rest of the app.
    """
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        session = get_session_for_row(row)
        subscribed_result = session.list_subscribed_regions()
    except Exception as exc:  # noqa: BLE001
        return TenantRegionsOut(ok=False, message=f"读取已开通区域失败：{exc}")
    if not subscribed_result.ok:
        return TenantRegionsOut(ok=False, message=subscribed_result.message or "读取已开通区域失败")

    sub_data = subscribed_result.data if isinstance(subscribed_result.data, dict) else {}
    home_region = str(sub_data.get("home_region") or row.region or "")
    # region name -> panel row that already manages it
    rows_by_region = {
        (r.region or "").strip().lower(): r for r in _tenancy_rows(db, row)
    }

    subscribed: list[TenantRegionItem] = []
    subscribed_names: set[str] = set()
    for item in sub_data.get("regions") or []:
        name = str(item.get("region_name") or "")
        subscribed_names.add(name)
        existing = rows_by_region.get(name)
        subscribed.append(
            TenantRegionItem(
                region_name=name,
                region_key=str(item.get("region_key") or ""),
                region_label=region_area(name),
                is_home_region=bool(item.get("is_home_region")),
                status=str(item.get("status") or ""),
                subscribed=True,
                tenant_id=existing.id if existing is not None else "",
            )
        )

    available: list[TenantRegionItem] = []
    message = ""
    try:
        catalog = session.list_all_regions()
    except Exception as exc:  # noqa: BLE001
        catalog = None
        message = f"区域清单读取失败：{exc}"
    if catalog is not None and catalog.ok:
        for item in (catalog.data if isinstance(catalog.data, dict) else {}).get("regions") or []:
            name = str(item.get("region_name") or "")
            if not name or name in subscribed_names:
                continue
            available.append(
                TenantRegionItem(
                    region_name=name,
                    region_key=str(item.get("region_key") or ""),
                    region_label=region_area(name),
                    subscribed=False,
                )
            )
    elif catalog is not None:
        message = catalog.message or "区域清单读取失败"

    return TenantRegionsOut(
        ok=True,
        message=message,
        home_region=home_region,
        subscribed=subscribed,
        available=available,
    )


@router.post("/{tenant_id}/regions/subscribe", response_model=RegionSubscribeResult)
def subscribe_tenant_region(
    tenant_id: str,
    body: RegionSubscribeRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RegionSubscribeResult:
    """开通副区: subscribe the tenancy to another region and add a panel row for it.

    Idempotent by design — an already-subscribed region skips the Oracle mutation
    and only adds the missing panel row, which is the normal path for someone who
    subscribed in the Oracle console first.

    The new row is created with ``free_only_mode`` off: Always Free exists only in
    the home region, so every instance in a 副区 is billable and leaving the
    free-cap guard armed would refuse every launch there. ``confirm`` is the user's
    acknowledgement of both that cost and the fact that Oracle cannot un-subscribe
    a region.
    """
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not body.confirm:
        raise HTTPException(status_code=400, detail="请先确认：副区一经开通无法取消，且副区资源不属于免费额度")

    wanted = body.region.strip().lower()
    # A 副区 row created by this endpoint only exists because the subscription
    # succeeded, so a repeat click (or a click while Oracle is still reporting the
    # brand-new subscription as pending) must not fire a second create_region_subscription.
    linked = next(
        (
            r
            for r in _tenancy_rows(db, row)
            if (r.region or "").strip().lower() == wanted and (r.parent_tenant_id or "")
        ),
        None,
    )
    if linked is not None:
        return RegionSubscribeResult(
            ok=True,
            message=f"该副区已开通并已在面板中：{linked.name}",
            region_name=wanted,
            already_subscribed=True,
            tenant=_to_out(linked),
        )

    try:
        session = get_session_for_row(row)
        result = session.subscribe_region(wanted)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"开通副区失败：{exc}") from exc
    if not result.ok:
        write_audit(
            db,
            owner_id=user.id,
            action="tenant.region.subscribe",
            target=f"{row.name}:{wanted}",
            detail={"ok": False, "message": result.message},
        )
        return RegionSubscribeResult(ok=False, message=result.message or "开通副区失败")

    data = result.data if isinstance(result.data, dict) else {}
    region_name = str(data.get("region_name") or wanted)
    already = bool(data.get("already"))
    message = result.message or ""

    tenant_out = None
    if body.add_tenant:
        parent = _root_tenant(db, row)
        existing = next(
            (
                r
                for r in _tenancy_rows(db, row)
                if (r.region or "").strip().lower() == region_name
            ),
            None,
        )
        if existing is not None:
            tenant_out = _to_out(existing)
            message += f"；面板中已有该区域租户「{existing.name}」"
        else:
            label = region_area(region_name)
            # Tenant.name is VARCHAR(128) and the parent may already be that long.
            # SQLite silently overflows, PostgreSQL raises "value too long" and the
            # subscription would already have been made at Oracle by then — an
            # irreversible action followed by a 500.
            suffix = f" · {label}"
            child_name = (parent.name[: 128 - len(suffix)] + suffix)[:128]
            # region 现在是一条安全边界（带 "." / "@" / ":" 就能改写 OCI SDK 拼出的
            # endpoint 主机名，详见 app/config_store.py::TenantConfig.validate）。
            # 这是**唯一**一条既不过 Pydantic 的 pattern、也不过 TenantConfig.validate
            # 就能写进 Tenant.region 的路径 —— 当前 region_name 全部来自 Oracle 的
            # 区域列表、拿不到用户输入，但既然它已经是边界，就不该依赖「上游恰好干净」。
            if not _REGION_RE.fullmatch(region_name or ""):
                raise HTTPException(
                    status_code=502,
                    detail=f"Oracle 返回的区域名不合法，已中止：{region_name!r}",
                )
            child = Tenant(
                owner_id=user.id,
                name=child_name,
                user_ocid=parent.user_ocid,
                tenancy_ocid=parent.tenancy_ocid,
                fingerprint=parent.fingerprint,
                region=region_name,
                # Compartment OCIDs are tenancy-wide, so the parent's still applies.
                compartment_ocid=parent.compartment_ocid or "",
                parent_tenant_id=parent.id,
                description=f"{parent.name} 的副区（{region_name}）· 资源按量计费",
                enabled=True,
                color=parent.color or "#3B82F6",
                private_key_encrypted=parent.private_key_encrypted,
                account_tier=parent.account_tier or "",
                free_only_mode=False,
            )
            db.add(child)
            db.commit()
            db.refresh(child)
            tenant_out = _to_out(child)
            message += f"；已添加副区租户「{child.name}」"

    write_audit(
        db,
        owner_id=user.id,
        action="tenant.region.subscribe",
        target=f"{row.name}:{region_name}",
        detail={
            "ok": True,
            "already_subscribed": already,
            "tenant_id": tenant_out.id if tenant_out else "",
        },
    )
    return RegionSubscribeResult(
        ok=True,
        message=message,
        region_name=region_name,
        already_subscribed=already,
        tenant=tenant_out,
    )


@router.get("/{tenant_id}/oci-password-policy", response_model=OciPasswordPolicyOut)
def get_oci_password_policy(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> OciPasswordPolicyOut:
    """Read Oracle Identity Domain password policies (real console force-change settings)."""
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        # Richer than the raw policy list: also resolves the user's own
        # lastSuccessfulSetDate so the panel can show the REAL expiry date, which
        # is what tells an operator whether 关闭强制改密 actually took effect.
        result = session.get_console_password_status()
        return OciPasswordPolicyOut(
            ok=bool(result.ok),
            message=result.message or "",
            data=result.data if isinstance(result.data, dict) else {},
        )
    except Exception as exc:  # noqa: BLE001
        return OciPasswordPolicyOut(ok=False, message=str(exc), data={})


@router.post("/{tenant_id}/oci-password-policy/disable-expiry", response_model=OciPasswordPolicyOut)
def disable_oci_password_expiry(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> OciPasswordPolicyOut:
    """Call Oracle Identity Domains API to clear passwordExpiresAfter (never expire)."""
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        result = session.disable_console_password_expiry()
    except Exception as exc:  # noqa: BLE001
        return OciPasswordPolicyOut(ok=False, message=str(exc), data={})

    return OciPasswordPolicyOut(
        ok=bool(result.ok),
        message=result.message or "",
        data=result.data if isinstance(result.data, dict) else {},
    )


@router.delete("/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 副区 rows share this row's credentials and would be left pointing at a tenant
    # the user can no longer edit, so they go with it.
    children = list(
        db.scalars(
            select(Tenant).where(
                Tenant.owner_id == user.id,
                Tenant.parent_tenant_id == row.id,
            )
        ).all()
    )
    # A default pointing at a deleted tenant would keep every page falling through
    # to "first tenant" with nothing in the UI explaining why.
    doomed = {target.id for target in [*children, row]}
    if user.locked_tenant_id in doomed:
        user.locked_tenant_id = ""
    for target in [*children, row]:
        db.delete(target)
    db.commit()
    # After the commit, for the same reason as update_tenant: a concurrent request
    # could otherwise rebuild and re-cache a session for a row that is going away.
    for target in [*children, row]:
        drop_session(target.id)
        # The launch-meta cache keyed on this tenant would otherwise be retained until
        # its TTL expired (clear_launch_meta_cache had no callers at all).
        clear_launch_meta_cache(target.id)
    if children:
        return {"message": f"已删除（含 {len(children)} 个副区）"}
    return {"message": "已删除"}


@router.post("/{tenant_id}/test", response_model=TenantTestResult)
def test_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantTestResult:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        result = session.test_connection()
        return TenantTestResult(ok=bool(result.ok), message=result.message or "")
    except Exception as exc:  # noqa: BLE001
        return TenantTestResult(ok=False, message=str(exc))
