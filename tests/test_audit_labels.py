"""审计日志页的动作名必须覆盖后端写入的每一种 action。

AuditView.vue 的 actionLabel 兜底是「原样显示」，于是后端新增一种审计动作而前端
没跟上时，页面不会报错，只是在动作列里裸显示 `firewall.sl_clear` 这样的内部 id ——
0.4.127 之前除登录外的 50 多种动作全是这样。这里从后端源码里收集所有 action，
逐个确认前端有中文名。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_VIEW = ROOT / "web" / "frontend" / "src" / "views" / "AuditView.vue"

# action=... 这一段（到行尾的逗号为止）里出现的所有带点的字符串字面量。
# 覆盖 `action="tenant.protect" if x else "tenant.unprotect"` 这种条件写法。
_ACTION_EXPR = re.compile(r"\baction=(f?\"[^\n]*?),?\s*$", re.M)
_DOTTED = re.compile(r"f?\"([a-z_]+(?:\.[a-z_{}]+)+)\"")
# auth.py 的登录审计走一个局部函数：_audit("auth.login_failed", "bad_password")
_AUTH_HELPER = re.compile(r"\b_audit\(\s*\"([a-z_]+\.[a-z_]+)\"")


def _backend_actions() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "web" / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for expr in _ACTION_EXPR.findall(text):
            found.update(_DOTTED.findall(expr))
        found.update(_AUTH_HELPER.findall(text))
    return found


def _frontend_labels() -> set[str]:
    text = AUDIT_VIEW.read_text(encoding="utf-8")
    block = text[text.index("const ACTION_LABELS") : text.index("const POWER_LABELS")]
    return set(re.findall(r"'([a-z_]+(?:\.[a-z_]+)+)':", block))


def test_the_collector_actually_finds_actions():
    """收集器要是悄悄什么都没找到，下面那条测试会假绿。"""
    actions = _backend_actions()
    assert len(actions) > 40, sorted(actions)
    assert {"instance.launch", "tenant.unprotect", "auth.login_failed"} <= actions


def test_every_backend_audit_action_has_a_chinese_label():
    labels = _frontend_labels()
    missing = []
    for action in sorted(_backend_actions()):
        # instance.power.{action} 是拼出来的，actionLabel 按前缀处理。
        if action.startswith("instance.power."):
            continue
        if action not in labels:
            missing.append(action)
    assert not missing, f"AuditView.vue 的 ACTION_LABELS 缺少：{missing}"
