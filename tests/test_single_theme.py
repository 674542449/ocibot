"""面板只有一套配色（Claude 风格的亮色），暗色模式在 0.4.119 移除。

这条测试防的是「半个暗色模式」回来：没有切换按钮、也没人设置 data-theme，但某个
组件里又写了 `html[data-theme='dark'] …` 规则 —— 那种规则永远不会生效，只是死代码，
而它的存在会让下一个人以为暗色模式还在、去给它补样式。

另外钉住 `:global(html…)` 这种写法不再出现：Vue 的 scoped 样式会把 :global() 后面
的后代选择器整个吞掉，编译成一条裸的 `html{…}` 规则，把颜色糊到根元素上
（本项目以前有十来处这样的规则，见 0.4.117 的 CHANGELOG）。
"""

from __future__ import annotations

import pathlib
import re

FRONTEND = pathlib.Path("web/frontend")
SOURCES = [
    FRONTEND / "index.html",
    *sorted((FRONTEND / "src").rglob("*.vue")),
    *sorted((FRONTEND / "src").rglob("*.ts")),
    *sorted((FRONTEND / "src").rglob("*.css")),
]


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_no_dark_theme_rules_or_attribute_remain():
    offenders = [str(p) for p in SOURCES if "data-theme" in _read(p)]
    assert not offenders, f"暗色模式已移除，这些文件还在用 data-theme：{offenders}"


def test_no_theme_toggle_remains():
    for p in SOURCES:
        src = _read(p)
        assert "toggleTheme" not in src, p
        assert "setItem('ocibot_theme'" not in src, p


def test_no_global_html_selector_in_scoped_styles():
    pattern = re.compile(r":global\(\s*html")
    offenders = [str(p) for p in SOURCES if p.suffix == ".vue" and pattern.search(_read(p))]
    assert not offenders, offenders


def test_the_page_declares_light_only():
    css = _read(FRONTEND / "src/styles.css")
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    assert "color-scheme: light" in root
    # 强调色是陶土橙（Claude 风格），不是原来的靛蓝。
    assert "--accent: #ae4f2b" in root
    assert "prefers-color-scheme" not in css
