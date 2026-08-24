"""控制台密码到期倒计时：天数按日历日算，日期按本地时区印。

这里钉住的两个缺陷（都出在 `TenantSession._effective_password_expiry`）：

1. 天数少一天。原来写的是 `(expires_at - datetime.now(utc)).days`，
   `timedelta.days` 向下取整，而 `last_set` 总是过去若干小时，所以几乎每一个真实
   密码都少算一天：120 天策略下今天刚改的密码报 119 天；明天到期的密码报成
   「2026-08-25 到期（还有 0 天）」—— 日期说明天、天数说 0，同一句话里自相矛盾。
   正确语义是日历日之差（今天到期=0、明天=1、昨天=-1），
   `app/config_store.py::Tenant.password_days_left()` 早就是这么算的，两处不该差一天。

2. 日期取的是 UTC 日历日。SPA 其余日期都是本地时区，一个 2026-09-01T20:00Z 到期的
   密码对 UTC+8 的操作员来说本地已是 09-02，面板却印 09-01。

顺带钉住：「已过期」的判定改成真正的时刻比较。日历日之差对「半小时前刚过期」给出
0（同一天），若沿用旧的 `left < 0` 就会把一个已经登不进控制台的密码报成
「今天到期，还有 0 天」。

这些字段走 `GET /tenants/{id}/oci-password-policy` 的 `effective` 一路返回给前端，
目前 TenantsView 只渲染 `all_policies[].days`，所以直接调函数验证。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.oci_client import TenantSession

POLICY_DAYS = 120
POLICIES = [
    {"id": "pol-1", "name": "DefaultPasswordPolicy", "password_expires_after": POLICY_DAYS}
]


def _effective(last_set: datetime, **user_extra):
    user = {"last_set": last_set.isoformat(), **user_extra}
    return TenantSession._effective_password_expiry(POLICIES, user)


def _pin_timezone(monkeypatch, offset_hours: int) -> None:
    """把 `_to_local()` 固定成给定偏移。

    本机时区来自操作系统，`time.tzset()` 在 Windows 上不存在，所以只能从这个接缝
    进去，否则「UTC 日历日 ≠ 本地日历日」那条分支在 UTC 机器上根本走不到。

    **每一个**断日期/天数的测试都必须调用它。不调用就等于把断言挂在跑测试那台机器
    的墙上时钟上：一条「刚过期半小时应该报 0 天」的断言，在本地时间 00:05 跑会得到
    -1，于是这套测试每天夜里那半小时必红一次。
    """
    monkeypatch.setattr(
        TenantSession,
        "_to_local",
        staticmethod(lambda v: v.astimezone(timezone(timedelta(hours=offset_hours)))),
    )


def _pin_noon(monkeypatch) -> timezone:
    """把本地时区钉成「此刻正好是当地中午」的那个偏移，并返回它。

    单钉一个固定偏移还不够。这些断言算的是**日历日之差**，而输入是
    `now ± 30 分钟` / `now ± 1 天` —— 只要跑测试的那一刻靠近该时区的午夜，
    30 分钟的偏移就会跨过日界，「刚过期半小时」算出来就是 -1 而不是 0。
    这套断言因此会在每天午夜前后各半小时必红一次(CI 上就是每晚两次莫名其妙的
    红灯)。把当地时间钉到中午，任何 ±12 小时以内的偏移都不可能跨日界。
    """
    now = datetime.now(timezone.utc)
    tz = timezone(timedelta(hours=12 - now.hour))
    monkeypatch.setattr(
        TenantSession, "_to_local", staticmethod(lambda v: v.astimezone(tz))
    )
    return tz


def _date_in(summary: str) -> str:
    """summary 里的那一个 YYYY-MM-DD —— 「已过期（…）」和「… 到期」两种句式都有。"""
    match = re.search(r"\d{4}-\d{2}-\d{2}", summary)
    assert match, summary
    return match.group(0)


# ---------------------------------------------------------------- 天数

def test_password_changed_today_reports_the_full_policy_length():
    """今天刚改密 → 还有 120 天，不是 119。"""
    eff = _effective(datetime.now(timezone.utc))
    assert eff["days"] == POLICY_DAYS
    assert eff["days_left"] == POLICY_DAYS


def test_expiring_tomorrow_says_one_day_not_zero(monkeypatch):
    """明天到期 → 天数必须是 1；旧代码给 0，和它自己印出来的日期打架。"""
    tz = _pin_noon(monkeypatch)
    now = datetime.now(timezone.utc)
    eff = _effective(now - timedelta(days=POLICY_DAYS - 1))
    assert eff["days_left"] == 1
    tomorrow = (now + timedelta(days=1)).astimezone(tz).date()
    assert eff["summary"] == f"{tomorrow:%Y-%m-%d} 到期（还有 1 天）"


def test_days_left_matches_the_date_it_prints(monkeypatch):
    """天数和日期必须出自同一次计算：对 0..400 天里的每一天都成立。

    旧代码这里必炸：`(expires_at - now).days` 向下取整，印出来的日期却是同一个
    `expires_at` 的日历日，两者永远差一天。
    """
    _pin_timezone(monkeypatch, 0)
    now = datetime.now(timezone.utc)
    for elapsed in range(0, 400):
        eff = _effective(now - timedelta(days=elapsed))
        printed = datetime.strptime(_date_in(eff["summary"]), "%Y-%m-%d").date()
        assert (printed - now.date()).days == eff["days_left"], elapsed


def test_thirty_minutes_before_expiry_still_says_zero_days(monkeypatch):
    """半小时后到期 → 「还有 0 天」，这是原本就对的行为，别改坏。"""
    _pin_noon(monkeypatch)
    now = datetime.now(timezone.utc)
    eff = _effective(now - timedelta(days=POLICY_DAYS) + timedelta(minutes=30))
    assert eff["days_left"] == 0
    assert "还有 0 天" in eff["summary"]


def test_days_left_and_the_printed_date_agree_at_a_nonzero_offset(monkeypatch):
    """真正要钉住的性质：天数和它自己印出来的日期,永远出自同一个时区。

    这条测试原来写成「和 config_store.password_days_left() 逐值相等」,但它把时区
    钉成 offset 0 —— 在那个偏移下本地日历日和 UTC 日历日恒等,两边**必然**相等,
    所以它证明不了任何事。而两者其实是刻意不同的契约(见 config_store 里那段
    docstring):同一个到期时刻在 UTC+8 下是 9 对 8,因为一边的输入是操作员填的
    日期、另一边是 Oracle 给的时刻。断言它们相等,只会在有人正确地修改任一边时
    误报成回归。

    自洽性才是原始 bug 的本体(「明天到期 / 还有 0 天」印在同一句话里),
    而且它在**任何**偏移下都必须成立,所以这里专挑非零偏移跑。
    """
    for offset in (-11, -5, 0, 5, 8, 13):
        _pin_timezone(monkeypatch, offset)
        tz = timezone(timedelta(hours=offset))
        now = datetime.now(timezone.utc)
        for elapsed in (0, 1, 59, 119, 121, 200):
            eff = _effective(now - timedelta(days=elapsed))
            printed = datetime.strptime(_date_in(eff["summary"]), "%Y-%m-%d").date()
            today_local = now.astimezone(tz).date()
            assert (printed - today_local).days == eff["days_left"], (offset, elapsed)


def test_config_store_counts_from_the_operators_today_not_utcs(monkeypatch):
    """config_store 那边的「今天」也必须是本地日历日。

    它算的是「还有几天」,给人看的。UTC-5 的操作员在当地 20:00 时 UTC 已经跨天,
    按 UTC 数会平白少一天。它的**到期日**不做时区换算(输入本来就是操作员填的
    一个日期,不该因为人在纽约就变成前一天),只有「今天」换算。
    """
    from app.config_store import TenantConfig

    cfg = TenantConfig(
        id="t",
        name="t",
        user_ocid="ocid1.user.oc1..x",
        tenancy_ocid="ocid1.tenancy.oc1..x",
        fingerprint="aa:bb",
        region="ap-tokyo-1",
        private_key_pem="",
        password_expiry_days=POLICY_DAYS,
        password_changed_at=datetime.now(timezone.utc).date().isoformat(),
    )
    expiry = cfg.password_expiry_date()
    assert expiry is not None
    expected = (expiry.date() - datetime.now().astimezone().date()).days
    assert cfg.password_days_left() == expected


# ---------------------------------------------------------------- 已过期

def test_just_expired_is_reported_expired_not_zero_days_remaining(monkeypatch):
    """半小时前刚过期 → 「已过期」，不能因为还是同一天就说「还有 0 天」。"""
    _pin_noon(monkeypatch)
    now = datetime.now(timezone.utc)
    eff = _effective(now - timedelta(days=POLICY_DAYS) - timedelta(minutes=30))
    assert eff["summary"].startswith("已过期")
    assert eff["days_left"] == 0  # 同一日历日，不是 -1


def test_expired_yesterday_counts_minus_one():
    now = datetime.now(timezone.utc)
    eff = _effective(now - timedelta(days=POLICY_DAYS + 1))
    assert eff["days_left"] == -1
    assert eff["summary"].startswith("已过期")


def test_oracle_expired_flag_wins_even_when_the_date_is_in_the_future():
    """Oracle 说已过期就是已过期，日期算出来还在将来也不翻案。"""
    eff = _effective(datetime.now(timezone.utc), expired=True)
    assert eff["summary"].startswith("已过期")


# ---------------------------------------------------------------- 时区

@pytest.mark.parametrize(
    "offset_hours, expected_date",
    [
        (8, "2026-09-02"),   # UTC+8：20:00Z 已经是次日 04:00
        (0, "2026-09-01"),
        (-5, "2026-09-01"),  # UTC-5：仍是当天 15:00
    ],
)
def test_expiry_date_is_rendered_in_local_time(monkeypatch, offset_hours, expected_date):
    """到期日期按本地时区印；旧代码固定取 UTC 日历日，UTC+8 下少一天。"""
    _pin_timezone(monkeypatch, offset_hours)
    expires_at = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    eff = _effective(expires_at - timedelta(days=POLICY_DAYS))
    assert eff["summary"].startswith(expected_date)
    # expires_at 仍是同一个绝对时刻，只是渲染换了时区
    assert datetime.fromisoformat(eff["expires_at"]) == expires_at


def test_days_left_also_follows_local_calendar_day(monkeypatch):
    """同一个到期时刻，在不同本地时区里的「还有几天」不同 —— 天数也是本地日历日。"""
    now = datetime.now(timezone.utc)
    last_set = now - timedelta(days=POLICY_DAYS) + timedelta(hours=1)  # 一小时后到期

    # 本地正午：到期时刻（+1h）还在同一个本地日历日
    _pin_timezone(monkeypatch, 12 - now.hour)
    same_day = _effective(last_set)
    # 本地 23 点：同一个到期时刻已经落到次日
    _pin_timezone(monkeypatch, 23 - now.hour)
    next_day = _effective(last_set)

    assert same_day["days_left"] == 0
    assert next_day["days_left"] == 1
    # 天数变了，印出来的日期也必须跟着变，否则又是两者打架
    assert _date_in(next_day["summary"]) > _date_in(same_day["summary"])


# ---------------------------------------------------------------- 未回归的旧行为

def test_no_policy_means_never_expires():
    eff = TenantSession._effective_password_expiry(
        [{"id": "p", "name": "DefaultPasswordPolicy", "password_expires_after": 0}],
        {"last_set": datetime.now(timezone.utc).isoformat()},
    )
    assert eff["expires"] is False
    assert eff["days_left"] is None
    assert "永不过期" in eff["summary"]


def test_cant_expire_user_short_circuits():
    eff = _effective(datetime.now(timezone.utc), cant_expire=True)
    assert eff["expires"] is False
    assert "永不过期" in eff["summary"]


def test_unparseable_last_set_falls_back_to_the_policy_number():
    eff = TenantSession._effective_password_expiry(POLICIES, {"last_set": "not-a-date"})
    assert eff["days_left"] is None
    assert eff["summary"].startswith(f"{POLICY_DAYS} 天后过期")


def test_naive_last_set_is_treated_as_utc():
    """Oracle 偶尔回不带时区的时间戳；按 UTC 解释，不能崩在 aware/naive 相减上。"""
    naive = (datetime.now(timezone.utc) - timedelta(days=POLICY_DAYS - 10)).replace(tzinfo=None)
    eff = TenantSession._effective_password_expiry(POLICIES, {"last_set": naive.isoformat()})
    assert eff["days_left"] == 10
