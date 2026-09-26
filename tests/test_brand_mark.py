"""全站只有一个品牌标记，这条测试保证所有 LOGO / 网站图标长得一样。

标记「环与核心」：陶土橙圆角底板 + 白色圆环 + 同心实心圆点。0.4.122 起：

  * web/frontend/public/logo.svg —— 唯一的矢量源。侧边栏、手机顶栏、登录页都直接
    ``<img src="/logo.svg">``；以前侧边栏内联了一份跟随 --accent、没有底板的字形，
    登录页和 favicon 又是另一种颜色，三处各不一样。
  * web/frontend/public/favicon.svg —— 与 logo.svg **逐字相同**（不再有「深色标签栏
    换一档颜色」的变体）。
  * scripts/make_favicon.py —— 位图图标（favicon.ico、apple-touch-icon.png、
    icon-192/512.png）都由它按同一套几何生成；这些是二进制，改了看不出 diff。

改其中一处而忘了别处，症状是「标签页图标和界面里的 logo 长得不一样」——
没有任何别的测试会失败，也没人会立刻注意到。所以这里把它们钉在一起。
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import struct

FRONTEND = pathlib.Path("web/frontend")
PUBLIC = FRONTEND / "public"
LAYOUT = FRONTEND / "src/layouts/AppLayout.vue"
LOGIN = FRONTEND / "src/views/LoginView.vue"
LOGO = PUBLIC / "logo.svg"
FAVICON = PUBLIC / "favicon.svg"
ICO = PUBLIC / "favicon.ico"
MANIFEST = PUBLIC / "manifest.webmanifest"
GENERATOR = pathlib.Path("scripts/make_favicon.py")

# 圆环：中线半径 11、线宽 4.2，不填充 —— 标记的主体。
_RING = re.compile(
    r'<circle\s+cx="16"\s+cy="16"\s+r="11"\s+fill="none"\s+stroke="#fff"\s+stroke-width="4\.2"'
)
# 同心的实心核心，半径与环的线宽相同，粗细一致。
_CORE = re.compile(r'<circle\s+cx="16"\s+cy="16"\s+r="4\.2"\s+fill="#fff"')
# 字形收到 78% 并居中：16 - 16*0.78 = 3.52。
_INSET = re.compile(r"translate\(3\.52 3\.52\)\s*scale\(0\.78\)")
# 陶土橙底板，与生成脚本里的 BRAND 同色。
_TILE = re.compile(r'<rect\s+width="32"\s+height="32"\s+rx="7"\s+fill="#c6613f"')


def _read(p: pathlib.Path) -> str:
    assert p.is_file(), f"missing {p}"
    return p.read_text(encoding="utf-8")


def _png_size(p: pathlib.Path) -> tuple[int, int]:
    raw = p.read_bytes()
    assert raw[:8] == bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]), f"{p} 不是 PNG"
    return struct.unpack(">II", raw[16:24])


def test_logo_svg_is_the_single_vector_source():
    src = _read(LOGO)
    for pattern, what in ((_TILE, "底板"), (_RING, "圆环"), (_CORE, "核心圆点"), (_INSET, "内缩")):
        assert pattern.search(src), f"logo.svg 的{what}和生成脚本对不上"


def test_the_favicon_svg_is_byte_identical_to_the_logo():
    """不再有「深色标签栏换一档颜色」之类的变体 —— 标签页图标就是 logo。"""
    assert FAVICON.read_bytes() == LOGO.read_bytes()
    assert "prefers-color-scheme" not in _read(FAVICON)


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
    assert ns["TILE_R"] == 7.0
    assert ns["GLYPH_INSET"] == 0.78
    assert round(16 - 16 * ns["GLYPH_INSET"], 2) == 3.52
    assert "#%02x%02x%02x" % ns["BRAND"] == "#c6613f"
    # 旧标记（缺口 + 方块）的参数不能残留：残留说明有人只改了一半。
    for stale in ("GAP_CENTER", "GAP_HALF", "NODE_ANG", "NODE_SIZE"):
        assert stale not in ns, stale


def test_every_place_in_the_ui_shows_that_same_file():
    """侧边栏、手机顶栏、登录页都引用 /logo.svg，不再各画各的。"""
    layout = _read(LAYOUT)
    brand = layout.split('<div class="brand">', 1)[1].split("</div>", 1)[0]
    assert 'class="brand-mark" src="/logo.svg"' in brand
    topbar = layout.split('<header class="mobile-topbar">', 1)[1].split("</header>", 1)[0]
    assert 'src="/logo.svg"' in topbar, "手机顶栏没有 logo"
    assert 'src="/logo.svg"' in _read(LOGIN)
    # 内联的第二份标记不能回来 —— 那正是三处长得不一样的原因。
    assert "<circle" not in layout
    assert "currentColor" not in brand


def test_the_brand_block_has_no_text_left():
    """用户要的就是「logo 旁边的文字去掉」。"""
    src = _read(LAYOUT)
    brand = src.split('<div class="brand">', 1)[1].split("</div>", 1)[0]
    assert "OCIBot" not in brand.replace('alt="OCIBot"', "")
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


def test_home_screen_icons_are_pngs_of_the_right_size():
    """iOS 不认 SVG 的 apple-touch-icon —— 以前指向 logo.svg，等于没有主屏图标。"""
    html = _read(FRONTEND / "index.html")
    assert 'rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"' in html
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert _png_size(PUBLIC / "apple-touch-icon.png") == (180, 180)
    # iOS 自己套圆角蒙版：四角必须不透明，否则会被垫成黑色。
    raw = (PUBLIC / "apple-touch-icon.png").read_bytes()
    import zlib

    idat = b"".join(
        raw[i + 8 : i + 8 + struct.unpack(">I", raw[i : i + 4])[0]]
        for i in [m.start() - 4 for m in re.finditer(b"IDAT", raw)]
    )
    first_px_alpha = zlib.decompress(idat)[1 + 3]  # 过滤字节之后第一个像素的 A
    assert first_px_alpha == 255, "apple-touch-icon 的角是透明的"


def test_the_manifest_points_at_icons_that_exist():
    data = json.loads(_read(MANIFEST))
    assert data["name"] == "OCIBot"
    sizes = {}
    for icon in data["icons"]:
        path = PUBLIC / icon["src"].lstrip("/")
        assert path.is_file(), icon["src"]
        if icon["type"] == "image/png":
            w, h = _png_size(path)
            assert icon["sizes"] == f"{w}x{h}", icon
            sizes[w] = icon["src"]
    assert 192 in sizes and 512 in sizes, "Android 安装图标至少要 192 和 512"


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


def test_the_backend_serves_the_icons_with_the_right_types():
    """.webmanifest 在不少发行版的 mimetypes 里没有登记，交给通配路由会变成
    text/plain；PNG 图标走通配路由，要确认真的能取到而不是落回 SPA 首页。"""
    import pytest

    if not (FRONTEND / "dist" / "index.html").is_file():
        pytest.skip("没有构建产物（纯 API 部署）")
    from fastapi.testclient import TestClient

    from web.backend.main import create_app

    with TestClient(create_app()) as c:
        r = c.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        assert r.json()["name"] == "OCIBot"
        for name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
            r = c.get("/" + name)
            assert r.status_code == 200, name
            assert r.headers["content-type"] == "image/png", name
