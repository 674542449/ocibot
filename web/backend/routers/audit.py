"""Audit log listing."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from web.backend.audit import iso_utc
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import AuditLog, User

router = APIRouter(prefix="/audit", tags=["audit"])

# 登录时间线的动作白名单。auth_only=true 时返回的就是且只是这些。
#
# 原来只有前六条。后加的四条同样是「这个账号的登录方式发生了什么」：注册是账号第一次
# 出现，改密码和开关 2FA 都会 bump token_version 顶掉全部已有会话。少了它们，「一串
# login_failed 之后 2FA 被关掉、密码被改」这种最该一眼看出来的序列会断成两截 ——
# 后半截只在不勾「只看登录」的全量视图里，而那个视图混着租户/实例/存储几十种动作，
# 等于藏起来了。
#
# 管理员重置密码 / 改 is_active 同样会顶掉会话，看着也该进来，但 admin.reset_password
# 和 admin.user_patch 是**故意**不加的，两个理由：
#  1. 它们的 owner_id 是执行操作的管理员（见 routers/admin.py），不是被改的那个账号。
#     加进来对被重置的用户毫无帮助 —— 他在自己的审计页里照样看不到自己被重置。要补
#     这件事得在写入端多写一行归属到 target 用户的审计，不是这张白名单能解决的。
#  2. 它们不是 auth.* 前缀。实测过：加进来之后，下面那条针对管理员的放宽会让别的管理
#     员的 admin.* 行出现在「只看登录」里，tests/test_login_audit.py 里
#     `all(action.startswith("auth."))` 这条不变量当场失败（整套跑时共用一个 sqlite
#     库，本文件的用例先跑、先写下那些行）。改成把 admin.* 也算登录事件，得连那条
#     不变量和前端 ACTION_LABELS（只认 auth.*，其余按原始 id 裸显示）一起改。
#
# 也就是说：这里少的不是两个字符串，是一个跨三处的约定。真要做，写入端先补归属。
_AUTH_ACTIONS = (
    "auth.login",
    "auth.login_failed",
    "auth.login_blocked",
    "auth.login_disabled",
    "auth.totp_failed",
    "auth.logout_all",
    "auth.register",
    "auth.change_password",
    "auth.totp_enabled",
    "auth.totp_disabled",
)


@router.get("")
def list_audit(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(100, ge=1, le=500),
    auth_only: bool = Query(False, description="only return login-related events"),
) -> list[dict[str, Any]]:
    stmt = select(AuditLog)
    # 动作白名单先加、且只看 auth_only 一个条件。放在 owner 过滤**之前**是结构上的
    # 保险：下面那条放宽 owner 的分支只有在 auth_only 为真时才走得到，因此它能放宽的
    # 范围永远被这一行框死在登录类事件里，改不出「顺手把租户/实例操作也漏出去」。
    if auth_only:
        stmt = stmt.where(AuditLog.action.in_(_AUTH_ACTIONS))
    # 「只看登录」+ 管理员：这是对 owner 隔离的一处**刻意的、范围受限的**例外。
    #
    # 为什么必须有它：auth.py 在用户名**存在**时把 auth.login_failed / auth.totp_failed
    # 归属到被匹配上的那个账号，只有瞎编的用户名才 owner_id 为空。于是原来的
    # `owner_id == 自己 OR IS NULL` 恰好把信号和噪声搞反了 —— 管理员看得见有人拿
    # 不存在的用户名乱撞，看不见有人正在撞 bob 这个真实账号；连「密码是对的、只被
    # 2FA 挡住」这条本文件里信号最强的事件也一并滤掉。实测过：bob 连续失败六次之后，
    # 管理员 GET /api/audit?auth_only=true 返回的是空数组。
    #
    # 为什么这样放宽是正当的：本面板的管理员本来就能列出全部用户、禁用账号、重置任
    # 意账号的密码（routers/admin.py），登录事件里「别人的东西」只有四样：用户名、
    # 来源 IP、成败原因，以及 detail 里的 ua（User-Agent，auth.py 的 _audit() 从请求头
    # 取、截断 200 字符，AuditView 有专门一列渲染它）。用户名管理员本来就看得到，
    # IP 和原因正是判断撞库要用的，ua 不比 IP 更敏感。真正需要挡住
    # 的是别人的租户配置、实例操作、任务与通知，那些不在 _AUTH_ACTIONS 里，也就不受
    # 这个分支影响；web/AUDIT.md 第十轮记下的 owner 隔离在其余所有路径上原样保留。
    #
    # 作用域故意留窄：不勾「只看登录」的默认视图不放宽（否则等于全表可见），普通用户
    # 不放宽（他只该看自己的），响应字段也没有增加 —— 账号名一直在 target 里。
    if auth_only and user.is_admin:
        pass  # 不加 owner 过滤：范围已被上面的白名单框死在登录事件里
    elif user.is_admin:
        # NULL owner = anonymous event (failed login for an unknown username, or a
        # rate-limit block). Only an admin has a reason to see other accounts'.
        stmt = stmt.where(or_(AuditLog.owner_id == user.id, AuditLog.owner_id.is_(None)))
    else:
        stmt = stmt.where(AuditLog.owner_id == user.id)
    rows = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "created_at": iso_utc(r.created_at),
        }
        for r in rows
    ]
