"""账号等级判断，以及「这个型号能不能开」。

多出口 IP（仅升级账号）和付费型号（仅升级账号 + 二次确认）共用这一套，
别在各个路由里各写一份 —— 写法一不一致，同一个租户在两个页面上会得到相反的答案。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.free_quota import is_free_shape


def effective_tier(db: Session, row: Any) -> str:
    """'paid' / 'free' / ''（还没做过「等级查询」）。

    副区租户行建立时抄了一份主租户的等级，但主租户之后才点「等级查询」的话那份是
    旧的，所以以主租户为准。
    """
    from web.backend.models import Tenant

    tier = str(getattr(row, "account_tier", "") or "")
    parent_id = str(getattr(row, "parent_tenant_id", "") or "")
    if parent_id and db is not None:
        parent = db.get(Tenant, parent_id)
        if parent is not None and parent.account_tier:
            tier = parent.account_tier
    return tier


def paid_shape_block_reason(db: Session, row: Any, shape: str) -> str:
    """'' if this tenant may launch ``shape``; otherwise why not.

    免费型号谁都能开。付费型号只对已识别为升级（按量付费）的账号开放：免费账号
    Oracle 本来就不让开（或者会去扣试用金），没查过等级的先让用户查一次 —— 不在
    不知道账号情况的时候放一台计费机器出去。
    """
    if is_free_shape(shape):
        return ""
    tier = effective_tier(db, row)
    if tier == "paid":
        return ""
    if tier == "free":
        return f"当前是免费（未升级）账号，只能创建免费型号（A1.Flex / E2.1.Micro）；「{shape}」是付费型号"
    return (
        f"「{shape}」是付费型号，但还不知道这个账号是否已升级："
        "请先到「租户」页点该租户的「等级查询」，升级账号才能创建付费型号"
    )
