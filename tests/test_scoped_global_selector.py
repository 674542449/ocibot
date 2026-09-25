"""scoped 样式里不许写「:global(...) 后面再跟选择器」。

Vue 的 scoped CSS 编译器把 `:global(X)` 当成「整条选择器都是全局的」，后面的
部分**整个丢掉**：

    :global(html[data-theme='dark']) .parse-box.ok { color: #7dffa8 }
    → html[data-theme="dark"] { color: #7dffa8 }

结果是两头落空：想要的深色配色永远不生效（TenantsView 的 `.parse-box.ok/.bad`、
AccountView 的配额徽章、ToastHost 的三种提示），这些颜色反而全糊到 <html> 上，
哪条最后加载哪条赢。0.4.117 之前有 10 处这么写。

vue-tsc 和 vite build 都不会报错 —— 编译产物是合法 CSS，只是意思全变了；项目又
没有前端测试运行器。所以在这里卡住源码。

要的效果直接写成后代选择器即可：`html[data-theme='dark'] .parse-box.ok`。scoped
样式只给最后一段加 data-v 属性，正好就是本意。单独的 `:global(.x)`、以及
`.a :global(.b)` 都是合法用法，不拦。
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path("web/frontend/src")

_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# :global(...) 之后、下一个 `{` 或 `,` 之前还有别的选择器内容 —— 就是会被吞掉的那段。
# 括号里允许一层嵌套，够覆盖 :global(html:not(.x)) 这类写法。
_SWALLOWED = re.compile(r":global\((?:[^()]|\([^()]*\))*\)\s*[^\s{,]")


def _offenders(css: str) -> list[str]:
    return [m.group(0) for m in _SWALLOWED.finditer(_COMMENT.sub("", css))]


def test_pattern_flags_the_broken_form_and_allows_the_valid_ones():
    """守卫自己不能悄悄失效：先确认它认得出坏写法、放得过好写法。"""
    assert _offenders(":global(html[data-theme='dark']) .toast-ok { color: red }")
    assert _offenders(":global(html[data-theme='dark']).x { color: red }")
    assert _offenders("a,\n:global(html:not(.light)) .b { color: red }")
    assert not _offenders(":global(.x) { color: red }")
    assert not _offenders(":global(.x),\n:global(.y) { color: red }")
    assert not _offenders(".a :global(.b) { color: red }")
    assert not _offenders("html[data-theme='dark'] .parse-box.ok { color: red }")
    # 注释里讲这个坑是允许的（InstancesView / TenantsView 都有）。
    assert not _offenders("/* 不要写成 :global(html[data-theme='dark']) .x */ .y { }")


def test_no_vue_style_block_uses_global_as_an_ancestor():
    files = sorted(SRC.rglob("*.vue"))
    assert files, f"no .vue files under {SRC} — wrong working directory?"
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for block in _STYLE.findall(text):
            bad.extend(f"{path}: {hit.strip()}" for hit in _offenders(block))
    assert not bad, (
        "Vue 会把 `:global(...)` 后面的选择器整个丢掉，编译成一条裸的全局规则；"
        "改成普通后代选择器，如 `html[data-theme='dark'] .foo`：\n  " + "\n  ".join(bad)
    )
