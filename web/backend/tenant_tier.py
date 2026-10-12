"""账号等级判断（副区以主租户为准）。

多出口 IP（仅升级账号）用它。付费型号曾经也只对升级账号开放，0.4.134 起放开 ——
新注册的号带试用积分，可以用积分开付费型号；付费型号只要求页面上的二次确认。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


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
