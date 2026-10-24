"""配色的对比度（0.4.165 电光莱姆绿 + 极夜黑，深色）。

直接从 styles.css 的 :root 读变量再算 WCAG 对比度，改一个颜色就会知道它压没压线：
标题 / 正文 / 弱化文字 / 莱姆绿文字 / 状态色 4.5:1，控件边框 / 图表线这类图形 3:1。

深色底上要当心的是反过来的那一面：**亮色填充上面的字**。莱姆绿上白字只有 1.5:1，成功绿、
琥珀、红这些胶囊上的白字也都不到 3:1 —— 按钮和状态胶囊上的字一律用 --on-accent（极夜黑）。
0.4.164 之前是浅色主题，那些地方写的都是 `color: #fff`，换深色时最容易漏改的就是它们。
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
        # 文字 4.5:1，在每一种深色面上
        *[(fg, s, 4.5) for fg in ("heading", "text", "muted", "accent-text") for s in SURFACES],
        ("text", "accent-soft", 4.5),  # 导航选中项
        ("heading", "accent-soft", 4.5),
        ("accent-text", "accent-soft", 4.5),  # 提示条、删除保护徽章
        ("danger", "panel", 4.5),
        ("danger", "danger-soft", 4.5),
        ("ok", "panel", 4.5),
        ("ok", "ok-soft", 4.5),
        ("warn", "panel", 4.5),
        ("warn", "warn-soft", 4.5),
        # 亮色填充上的字
        ("on-accent", "accent", 4.5),  # 主按钮
        ("on-accent", "accent-hover", 4.5),
        ("on-accent", "danger", 4.5),  # 危险按钮悬停、超额胶囊
        ("on-accent", "ok", 4.5),  # 用量胶囊
        ("on-accent", "warn", 4.5),
        ("on-accent", "#fb923c", 4.5),  # 临界胶囊（AccountView .st-critical）
        # 图形 3:1
        *[("border-strong", s, 3.0) for s in SURFACES],
        ("accent", "panel-2", 3.0),  # 焦点框、选中条、进度条
        ("secondary", "panel", 3.0),  # 次级图表
    ],
)
def test_contrast(fg, bg, need):
    r = _ratio(_c(fg), _c(bg))
    assert r >= need, f"{fg} {_c(fg)} 在 {bg} {_c(bg)} 上只有 {r:.2f}:1，要 {need}:1"


def test_the_palette_is_the_one_asked_for():
    assert TOKENS["accent"] == "#a3e635"
    assert TOKENS["bg"] == "#090a0f"
    assert TOKENS["panel"] == "#12151e" and TOKENS["border"] == "#1f2430"
    assert TOKENS["secondary"] == "#10b981" and TOKENS["ok"] == "#10b981"
    assert TOKENS["heading"] == "#f8fafc" and TOKENS["text"] == "#94a3b8"


def test_no_white_text_on_bright_fills():
    """浅色主题时写下的 `color: #fff` 在深色主题里都落在亮色填充上 —— 白字在莱姆绿上 1.5:1，
    在成功绿上 2.5:1，基本看不见。亮色填充上的字用 --on-accent。"""
    offenders = []
    for p in [*FRONTEND.rglob("*.vue"), *FRONTEND.rglob("*.css")]:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![-\w])color:\s*(#fff\b|#ffffff\b|white\b)", line):
                offenders.append(f"{p}:{i}: {line.strip()}")
    assert not offenders, offenders
