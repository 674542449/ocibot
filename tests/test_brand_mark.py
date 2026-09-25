"""品牌标记的几何分散在四个地方，这条测试保证它们不会走散。

标记（0.4.121 起是「环与核心」：同心的圆环 + 实心圆点）同一套几何存在于：

  1. web/frontend/src/layouts/AppLayout.vue —— 侧边栏内联的那份。必须内联，
     因为 `<img src="...">` 里的 currentColor 拿不到外部 CSS，标记就没法跟着
     主题变色。
  2. web/frontend/public/logo.svg —— 给 apple-touch-icon 那类没有 CSS 上下文
     的地方用，颜色写死。
  3. web/frontend/public/favicon.svg —— 标签页图标，带底板。
  4. scripts/make_favicon.py —— favicon.ico 是二进制的，改了看不出 diff，
     所以画它的参数留在脚本里。

改其中一个而忘了另外三个，症状是「标签页图标和界面里的 logo 长得不一样」——
没有任何测试会失败，也没人会立刻注意到。所以这里把它们钉在一起。
"""

from __future__ import annotations

import math
import pathlib
import re

FRONTEND = pathlib.Path("web/frontend")
LAYOUT = FRONTEND / "src/layouts/AppLayout.vue"
LOGO = FRONTEND / "public/logo.svg"
FAVICON = FRONTEND / "public/favicon.svg"
ICO = FRONTEND / "public/favicon.ico"
GENERATOR = pathlib.Path("scripts/make_favicon.py")

# 圆环：中线半径 11、线宽 4.2，不填充 —— 标记的主体。
_RING = re.compile(
    r'<circle\s+cx="16"\s+cy="16"\s+r="11"\s+fill="none"\s+stroke="[^"]+"\s+stroke-width="4\.2"'
)
# 同心的实心核心，半径与环的线宽相同，粗细一致。
_CORE = re.compile(r'<circle\s+cx="16"\s+cy="16"\s+r="4\.2"\s+fill="[^"]+"')
# 带底板的两份把字形收到 78% 并居中：16 - 16*0.78 = 3.52。
_INSET = re.compile(r'translate\(3\.52 3\.52\)\s*scale\(0\.78\)')


def _read(p: pathlib.Path) -> str:
    assert p.is_file(), f"missing {p}"
    return p.read_text(encoding="utf-8")


def test_all_four_copies_share_the_same_geometry():
    for path in (LAYOUT, LOGO, FAVICON):
        src = _read(path)
        assert _RING.search(src), f"{path} 里的圆环和其它几处对不上"
        assert _CORE.search(src), f"{path} 里的核心圆点和其它几处对不上"
    for path in (LOGO, FAVICON):
        assert _INSET.search(_read(path)), f"{path} 的底板内缩和 make_favicon.py 对不上"


def test_the_generator_uses_that_same_geometry():
    """脚本里存的是参数不是 SVG 字符串，所以这里取出常量逐个比对。"""
    src = _read(GENERATOR)
    ns: dict = {}
    # 只取常量段：再往下有 pathlib 之类的东西，exec 起来要多喂一堆依赖。
    head = src.split("_OUT =")[0]
    exec(compile(head, "gen", "exec"), {"math": math}, ns)

    assert (ns["CX"], ns["CY"], ns["R"]) == (16.0, 16.0, 11.0)
    assert ns["SW"] == 4.2
    assert ns["CORE_R"] == 4.2
    assert ns["GLYPH_INSET"] == 0.78
    assert round(16 - 16 * ns["GLYPH_INSET"], 2) == 3.52
    # 旧标记（缺口 + 方块）的参数不能残留：残留说明有人只改了一半。
    for stale in ("GAP_CENTER", "GAP_HALF", "NODE_ANG", "NODE_SIZE"):
        assert stale not in ns, stale


def test_the_sidebar_mark_follows_the_theme():
    """内联的那份必须用 currentColor 并挂在 --accent 上。

    写死颜色的话，亮色主题下的深靛在暗色侧边栏上会糊成一团 —— 这正是换掉
    旧 logo 的原因之一：它那个 #3370ff→#6b4eff 渐变和面板的强调色根本不是
    一个颜色。
    """
    src = _read(LAYOUT)
    mark = src.split('class="brand-mark"', 1)[1].split("</svg>", 1)[0]
    assert 'stroke="currentColor"' in mark
    assert 'fill="currentColor"' in mark
    assert ".brand-mark {" in src
    style = src.split(".brand-mark {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" in style
    # 渐变是旧标记的做法，别再回去了。
    assert "linearGradient" not in mark


def test_the_brand_block_has_no_text_left():
    """用户要的就是「logo 旁边的文字去掉」。"""
    src = _read(LAYOUT)
    brand = src.split('<div class="brand">', 1)[1].split("</div>", 1)[0]
    assert "OCIBot" not in brand.replace('aria-label="OCIBot"', "")
    # 匹配**用法**而不是**提及**：解释「原来这里有个 brand-text」的注释本身就含
    # 这个词，直接查子串会被自己写的注释绊倒（本仓踩过好几次）。
    assert 'class="brand-text"' not in src
    assert chr(10) + ".brand-text" not in src


def test_the_account_line_survived_the_cleanup():
    """全应用只有一处显示「我登录的是哪个账号」，原来挂在标题下面。

    这是个多租户面板，用错账号做的操作（关机、终止实例）是不可逆的。删标题时
    顺手把它一起删掉，就是把一个安全相关的信息去掉了 —— 所以它挪到了脚部。
    """
    src = _read(LAYOUT)
    assert "auth.username" in src
    assert "account-line" in src
    foot = src.split('<div class="sidebar-foot">', 1)[1]
    assert "auth.username" in foot.split("</div>", 3)[0] + foot[:600]
    # 侧栏收窄成图标栏时 .rail-label 会被隐藏，那时只剩 title 能确认身份。
    assert "accountFull" in src


def test_the_favicon_is_wired_up_including_the_ico_fallback():
    html = _read(FRONTEND / "index.html")
    assert 'href="/favicon.svg"' in html
    assert 'href="/favicon.ico"' in html, "少了 .ico 兜底，老 Safari 和抓图标的工具会拿到空白"
    # theme-color 以前还是旧 logo 那个蓝，和面板强调色对不上。
    assert "#3370ff" not in html


def test_the_ico_is_a_valid_multi_size_icon():
    import struct

    raw = ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind) == (0, 1), "不是合法的 ICO 头"
    assert count >= 3, f"只有 {count} 个尺寸；16/32/48 是最低要求"
    seen = []
    for i in range(count):
        w, h, *_rest, size, offset = struct.unpack("<BBBBHHII", raw[6 + 16 * i : 22 + 16 * i])
        blob = raw[offset : offset + size]
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"第 {i} 项不是 PNG"
        seen.append(w or 256)
    assert 16 in seen and 32 in seen, seen


def test_the_drawer_close_button_is_hidden_on_desktop():
    """宽屏下 logo 旁边曾经一直挂着一个点了没反应的「×」。

    .sidebar-close 同时也是 .icon-btn，而 .icon-btn 的 display 写在后面、优先级
    相同，单写 `.sidebar-close { display: none }` 会被它盖掉。隐藏和手机断点里的
    显示都得用两个类，才不依赖规则的先后顺序。
    """
    src = _read(LAYOUT)
    style = src.split("<style", 1)[1]
    hide = style.split(".icon-btn.sidebar-close {", 1)
    assert len(hide) == 2, "隐藏规则必须写成 .icon-btn.sidebar-close"
    assert "display: none" in hide[1].split("}", 1)[0]
    mobile = style.split("@media (max-width: 900px)", 1)[1]
    show = mobile.split(".icon-btn.sidebar-close {", 1)
    assert len(show) == 2, "手机断点里的显示规则也要同样的优先级，否则抽屉关不掉"
    assert "display: inline-grid" in show[1].split("}", 1)[0]
