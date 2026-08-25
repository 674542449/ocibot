"""Real console-password expiry reporting.

The panel can turn Oracle's forced password change off, but that call returning
success only means the PATCH was accepted. The operator's actual question is
"when does my console password expire now", which needs the domain policy AND the
user's own password state — a policy of 120 days says nothing about a date without
knowing when the password was last set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.oci_client import TenantSession

effective = TenantSession._effective_password_expiry


def _policy(pid: str, days, name: str = "Default"):
    return {"id": pid, "name": name, "password_expires_after": days}


def _user(**kw):
    base = {
        "found": True,
        "user_name": "someone@example.com",
        "last_set": "",
        "expired": False,
        "cant_expire": False,
        "applicable_policy_id": "",
        "applicable_policy_name": "",
    }
    base.update(kw)
    return base


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_policy_without_expiry_reports_never():
    """This is the state 关闭强制改密 is trying to reach — it must read plainly."""
    out = effective([_policy("p1", None)], _user())
    assert out["expires"] is False
    assert "永不过期" in out["summary"]
    assert out["expires_at"] == ""


def test_zero_days_is_also_never():
    out = effective([_policy("p1", 0)], _user())
    assert out["expires"] is False
    assert "永不过期" in out["summary"]


def test_user_marked_cant_expire_wins_over_the_policy():
    out = effective([_policy("p1", 120)], _user(cant_expire=True))
    assert out["expires"] is False
    assert "永不过期" in out["summary"]


def test_real_date_is_computed_from_the_last_password_change(monkeypatch):
    """把本地时区钉成「此刻正好是当地中午」，否则这条断言每天都会红一次。

    0.4.87 起 summary 里的日期按**本地**日历日渲染，而这里的 expected 是拿
    UTC 算的。两者在 UTC 时间跨过本地日界之后就差一天 —— 对 UTC+8 来说就是
    每天 16:00 UTC 之后必红。tests/test_password_expiry_days.py 当时钉住了
    这个时区，本文件漏了。

    钉到中午而不是钉成 UTC：这里的算术是 now ± 100 天，只要当地时间离午夜够远，
    任何 ±12 小时以内的偏移都不会跨日界。
    """
    now = datetime.now(timezone.utc)
    tz = timezone(timedelta(hours=12 - now.hour))
    monkeypatch.setattr(
        TenantSession, "_to_local", staticmethod(lambda v: v.astimezone(tz))
    )

    out = effective([_policy("p1", 120)], _user(last_set=_iso(20)))
    assert out["expires"] is True
    assert out["days"] == 120
    # 120 day policy, changed 20 days ago -> ~100 left.
    assert out["days_left"] in (99, 100)
    expected = (now + timedelta(days=100)).astimezone(tz).strftime("%Y-%m-%d")
    assert expected in out["summary"]
    assert out["expires_at"]


def test_expired_password_says_so():
    out = effective([_policy("p1", 30)], _user(last_set=_iso(45)))
    assert out["expires"] is True
    assert "已过期" in out["summary"]
    assert out["days_left"] < 0


def test_expired_flag_from_oracle_is_honoured():
    out = effective([_policy("p1", 30)], _user(last_set=_iso(1), expired=True))
    assert "已过期" in out["summary"]


def test_missing_last_set_reports_the_policy_without_inventing_a_date():
    """Better to say "120 天后过期，具体日期算不出" than to guess one."""
    out = effective([_policy("p1", 120)], _user(last_set=""))
    assert out["expires"] is True
    assert out["days"] == 120
    assert out["expires_at"] == ""
    assert "120 天" in out["summary"]


def test_unparseable_last_set_does_not_raise():
    out = effective([_policy("p1", 120)], _user(last_set="not-a-date"))
    assert out["expires"] is True
    assert out["expires_at"] == ""
    assert "not-a-date" in out["summary"]


def test_the_users_own_policy_is_used_when_known():
    policies = [_policy("p1", 30, "Strict"), _policy("p2", 200, "Loose")]
    out = effective(policies, _user(applicable_policy_id="p2", last_set=_iso(0)))
    assert out["days"] == 200
    assert out["policy_name"] == "Loose"


def test_unknown_applicable_policy_falls_back_to_the_strictest():
    """Erring towards "it still expires" — telling someone their password never
    expires when it does is the failure that costs them the account."""
    policies = [_policy("p1", 200, "Loose"), _policy("p2", 30, "Strict")]
    out = effective(policies, _user(applicable_policy_id="", last_set=_iso(0)))
    assert out["days"] == 30
    assert out["policy_name"] == "Strict"


def test_mixed_policies_where_only_some_expire():
    """A never-expiring policy must not mask one that does expire."""
    policies = [_policy("p1", None, "NoExpiry"), _policy("p2", 90, "Expires")]
    out = effective(policies, _user(last_set=_iso(0)))
    assert out["expires"] is True
    assert out["days"] == 90


def test_no_policies_at_all_is_reported_as_never_not_as_a_crash():
    out = effective([], _user())
    assert out["expires"] is False
    assert out["summary"]


@pytest.mark.parametrize("bad", ["abc", None, "", {}])
def test_garbage_policy_value_is_treated_as_no_expiry(bad):
    out = effective([_policy("p1", bad)], _user(last_set=_iso(1)))
    assert out["expires"] is False


# ------------------------------------------- which policy actually governs login


def test_default_password_policy_wins_over_the_system_template():
    """Reported by the operator: defaultPasswordPolicy is the value Oracle's own
    console shows, and it is what console logins are subject to.
    StandardPasswordPolicy is a protected system template — picking it because it
    happened to be stricter reported a number the tenancy is not actually under."""
    policies = [
        _policy("StandardPasswordPolicy", 30, "StandardPasswordPolicy"),
        _policy("defaultPasswordPolicy", 120, "defaultPasswordPolicy"),
    ]
    out = effective(policies, _user(last_set=_iso(0)))
    assert out["days"] == 120
    assert out["policy_name"] == "defaultPasswordPolicy"


def test_default_policy_with_no_expiry_reports_never_even_if_template_expires():
    """After 关闭强制改密 succeeds, defaultPasswordPolicy has no expiry while the
    untouched template still says 30 days. The answer must be 永不过期."""
    policies = [
        _policy("StandardPasswordPolicy", 30, "StandardPasswordPolicy"),
        _policy("defaultPasswordPolicy", None, "defaultPasswordPolicy"),
    ]
    out = effective(policies, _user(last_set=_iso(0)))
    assert out["expires"] is False
    assert "永不过期" in out["summary"]


def test_the_users_own_policy_still_outranks_the_default_one():
    policies = [
        _policy("defaultPasswordPolicy", 120, "defaultPasswordPolicy"),
        _policy("custom", 45, "Custom"),
    ]
    out = effective(policies, _user(applicable_policy_id="custom", last_set=_iso(0)))
    assert out["days"] == 45
    assert out["policy_name"] == "Custom"


def test_raw_policy_values_are_returned_for_display():
    """The operator asked to simply see the number, not only a derived verdict."""
    policies = [
        _policy("defaultPasswordPolicy", 120, "defaultPasswordPolicy"),
        _policy("StandardPasswordPolicy", 30, "StandardPasswordPolicy"),
    ]
    out = effective(policies, _user())
    listed = {p["name"]: p for p in out["all_policies"]}
    assert listed["defaultPasswordPolicy"]["days"] == 120
    assert listed["defaultPasswordPolicy"]["is_default"] is True
    assert listed["StandardPasswordPolicy"]["is_template"] is True


def test_template_is_not_used_as_the_strictest_fallback():
    """No default policy present and no applicable one: the template must still be
    skipped rather than becoming the answer."""
    policies = [
        _policy("StandardPasswordPolicy", 30, "StandardPasswordPolicy"),
        _policy("other", 90, "Other"),
    ]
    out = effective(policies, _user(last_set=_iso(0)))
    assert out["days"] == 90
    assert out["policy_name"] == "Other"
