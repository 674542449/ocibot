"""配色的对比度（0.4.166 Element 配色，浅色）。

直接从 styles.css 的 :root 读变量再算 WCAG 对比度，改一个颜色就会知道它压没压线。

Element 的亮色当**小字**不够清楚：#409EFF 在白底上 2.78:1、#67C23A 2.24、#E6A23C 2.19、
#F56C6C 2.90，它自己的次要文字 #909399 也只有 3.08（正文要 4.5:1）。这套配色的规矩是：
按钮、进度条、状态点这些**图形**用 Element 原色（--accent、--*-fill），**文字**用深一档的
（--accent-text、--ok / --warn / --danger、--muted）。最容易改坏的就是「顺手把文字也写成原色」，
下面两条扫描专门防这个。
"""

from __future__ import annotations

import pathlib
import re

import pytest

FRONTEND = pathlib.Path("web/frontend/src")
CSS = (FRONTEND / "styles.css").read_text(encoding="utf-8")
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


SURFACES = ("bg", "panel", "panel-2", "input-bg", "row-hover")


@pytest.mark.parametrize(
    "fg, bg, need",
    [
        # 文字 4.5:1，在每一种浅色面上
        *[(fg, s, 4.5) for fg in ("heading", "text", "muted", "accent-text") for s in SURFACES],
        ("heading", "accent-soft", 4.5),
        ("text", "accent-soft", 4.5),
        ("accent-text", "accent-soft", 4.5),  # 导航选中项、提示条、删除保护徽章
        ("accent-text-hover", "panel", 4.5),
        ("danger", "panel", 4.5),
        ("danger", "danger-soft", 4.5),
        ("ok", "panel", 4.5),
        ("ok", "ok-soft", 4.5),  # 状态徽章、用量胶囊
        ("warn", "panel", 4.5),
        ("warn", "warn-soft", 4.5),
        ("on-accent", "danger", 4.5),  # 危险按钮悬停
        ("#ffffff", "heading", 4.5),  # 深色提示框
        # 键盘焦点框是图形，3:1
        ("accent-active", "panel", 3.0),
        ("accent-active", "bg", 3.0),
        ("accent-active", "panel-2", 3.0),
    ],
)
def test_contrast(fg, bg, need):
    r = _ratio(_c(fg), _c(bg))
    assert r >= need, f"{fg} {_c(fg)} 在 {bg} {_c(bg)} 上只有 {r:.2f}:1，要 {need}:1"


def test_the_palette_is_element():
    assert TOKENS["accent"] == "#409eff"
    assert (TOKENS["ok-fill"], TOKENS["warn-fill"], TOKENS["danger-fill"]) == ("#67c23a", "#e6a23c", "#f56c6c")
    assert TOKENS["bg"] == "#f2f3f5" and TOKENS["panel"] == "#ffffff" and TOKENS["panel-2"] == "#f5f7fa"
    assert TOKENS["border"] == "#e4e7ed" and TOKENS["border-strong"] == "#dcdfe6"
    assert TOKENS["heading"] == "#303133" and TOKENS["text"] == "#606266"


def _rules(pattern: str) -> list[str]:
    hits = []
    for p in [*FRONTEND.rglob("*.vue"), *FRONTEND.rglob("*.css")]:
        selector = ""
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.rstrip().endswith("{"):
                selector = line
            if re.search(pattern, line) and "ico" not in selector:
                hits.append(f"{p}:{i}: {line.strip()}")
    return hits


def test_element_blue_is_never_used_as_text():
    """#409EFF 当文字在白底上只有 2.78:1。color: 用 --accent-text。图标除外（选择器带 ico 的规则）：
    SVG 图标用 currentColor 画，是图形不是文字。"""
    assert not _rules(r"(?<![-\w])color:\s*var\(--accent\)"), _rules(r"(?<![-\w])color:\s*var\(--accent\)")


def test_only_the_usage_bar_is_faded_when_full():
    """「用满」只淡化进度条。.st-full 同时用在状态胶囊和徽章上（浅底深字），整体调到 72% 透明度后
    字只剩 2.8:1 左右（0.4.166 的检查里发现）。"""
    vue = (FRONTEND / "views" / "AccountView.vue").read_text(encoding="utf-8")
    style = vue[vue.index("<style") :]
    for m in re.finditer(r"([^{}]*)\{([^}]*)\}", style):
        selector, body = m.group(1).strip(), m.group(2)
        if "opacity" in body and "st-full" in selector:
            assert selector.splitlines()[-1].strip() == ".gauge-fill.st-full", selector


def test_bright_status_fills_are_never_used_as_text():
    """--ok-fill / --warn-fill / --danger-fill 是 Element 原色，当文字只有 2.2～2.9:1。"""
    hits = _rules(r"(?<![-\w])color:\s*var\(--(ok|warn|danger)-fill\)")
    assert not hits, hits
