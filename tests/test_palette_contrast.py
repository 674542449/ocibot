"""配色的对比度（0.4.164 珊瑚红 + 明黄 + 深紫蓝）。

直接从 styles.css 的 :root 读变量再算 WCAG 对比度，改一个颜色就会知道它压没压线：
正文 / 弱化文字 / 珊瑚色文字 4.5:1，控件边框 / 焦点框 / 选中条这类图形 3:1。

珊瑚红 #FF5A5F 本身当文字用不够（白底 3.05:1），所以文字用的是 --accent-text；它在局部
底色 #FFF5F5 上连图形的 3:1 都不到（2.85），所以落在局部底色上的焦点框和选中条用的是
--accent-hover。这两条是这套配色里最容易被「顺手统一成 --accent」改坏的地方。
"""

from __future__ import annotations

import pathlib
import re

import pytest

CSS = pathlib.Path("web/frontend/src/styles.css").read_text(encoding="utf-8")
ROOT = CSS.split(":root {", 1)[1].split("}", 1)[0]
TOKENS = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", ROOT))


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _c(name: str) -> str:
    return name if name.startswith("#") else TOKENS[name]


@pytest.mark.parametrize(
    "fg, bg, need",
    [
        # 文字 4.5:1
        ("heading", "bg", 4.5),
        ("heading", "panel-2", 4.5),
        ("text", "bg", 4.5),
        ("text", "panel-2", 4.5),
        ("text", "accent-soft", 4.5),
        ("muted", "bg", 4.5),
        ("muted", "panel-2", 4.5),  # 表头
        ("accent-text", "bg", 4.5),  # 链接、选中的标签页
        ("accent-text", "panel-2", 4.5),
        ("accent-text", "accent-soft", 4.5),  # 提示条、删除保护徽章
        ("accent-text-hover", "bg", 4.5),
        ("danger", "bg", 4.5),
        ("danger", "danger-soft", 4.5),
        ("#ffffff", "danger", 4.5),
        ("ok", "bg", 4.5),
        ("ok", "ok-soft", 4.5),
        ("warn", "bg", 4.5),
        ("warn", "warn-soft", 4.5),
        ("#ffffff", "heading", 4.5),  # 悬浮提示
        # 图形 3:1
        ("accent", "bg", 3.0),  # 主按钮、输入框焦点边框、图表主线、品牌标记
        ("#ffffff", "accent", 3.0),  # 主按钮上的白字 / 标记上的白色字形
        ("accent-hover", "bg", 3.0),  # 焦点框
        ("accent-hover", "panel-2", 3.0),  # 焦点框、侧边栏选中条（落在局部底色上）
        ("border-strong", "bg", 3.0),
        ("border-strong", "panel-2", 3.0),
    ],
)
def test_contrast(fg, bg, need):
    r = _ratio(_c(fg), _c(bg))
    assert r >= need, f"{fg} {_c(fg)} 在 {bg} {_c(bg)} 上只有 {r:.2f}:1，要 {need}:1"


def test_the_palette_is_the_one_asked_for():
    assert TOKENS["accent"] == "#ff5a5f"
    assert TOKENS["highlight"] == "#fbbf24"
    assert TOKENS["heading"] == "#1e1b4b"
    assert TOKENS["bg"] == "#ffffff" and TOKENS["panel-2"] == "#fff5f5"
    assert TOKENS["text"] == "#4b5563"


def test_coral_is_never_used_as_text():
    """珊瑚红当文字在白底上只有 3.05:1。color: 一律用 --accent-text。

    图标除外（选择器里带 ico 的规则）：SVG 图标用 currentColor 画，是图形不是文字，
    门槛是 3:1 —— 导航选中项的图标落在白底上，3.05 够。"""
    src = pathlib.Path("web/frontend/src")
    offenders = []
    for p in [*src.rglob("*.vue"), *src.rglob("*.css")]:
        selector = ""
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.rstrip().endswith("{"):
                selector = line
            if re.search(r"(?<![-\w])color:\s*var\(--accent\)", line) and "ico" not in selector:
                offenders.append(f"{p}:{i}")
    assert not offenders, offenders


def test_focus_ring_and_nav_marker_use_the_deeper_coral():
    """这两样会落在局部底色 #FFF5F5 上，珊瑚红本身在那里只有 2.85:1。"""
    assert re.search(r":focus-visible \{\s*outline: 2px solid var\(--accent-hover\)", CSS)
    layout = pathlib.Path("web/frontend/src/layouts/AppLayout.vue").read_text(encoding="utf-8")
    marker = layout.split(".nav a.nav-active::before {", 1)[1].split("}", 1)[0]
    assert "background: var(--accent-hover)" in marker
