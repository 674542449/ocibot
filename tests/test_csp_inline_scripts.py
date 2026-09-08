"""index.html 里的内联脚本必须真的能执行。

## 它坏过，而且是**静默**坏的

CSP 里写的是 `script-src 'self'`，而 `'self'` **不覆盖内联脚本**。于是
`web/frontend/index.html` 里那段内联脚本从来没执行过 —— 浏览器控制台里一直有：

    Executing inline script violates the following Content Security Policy
    directive 'script-src 'self''. Either the 'unsafe-inline' keyword,
    a hash ('sha256-...'), or a nonce is required to enable inline execution.

后果两个，都不报错、都不会有测试变红：

* 防主题闪烁那段（读 localStorage 提前设 `data-theme`）没生效，浅色主题的人
  每次打开先闪一下深色；
* 会话探测（`/api/auth/me`）没法提前发，打开面板的请求链白白多一个往返 ——
  而这正是 0.4.113 想省掉的那个。

修法是**按内容哈希放行**，不是 `'unsafe-inline'`：后者等于把整类注入的口子重新
打开，而这是个多租户控制台（`web/AUDIT.md` 记着十轮审计）。

## 这个文件钉住三件事

1. CSP 里确实带着 dist/index.html 每段内联脚本的哈希；
2. `'unsafe-inline'` 没有混进 `script-src`；
3. 哈希是按 **LF 归一化**之后算的 —— 这台机器的工作区是 CRLF、Docker 里构建出来
   的是 LF，而浏览器在算哈希之前会把 CRLF 折成 LF。不归一化的话开发机和线上
   会得到两个不同的哈希，失败方式还是「内联脚本被静默拦掉」。
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

DIST_INDEX = pathlib.Path("web/frontend/dist/index.html")


def _client() -> TestClient:
    from web.backend.main import create_app

    return TestClient(create_app())


def _inline_bodies() -> list[str]:
    html = DIST_INDEX.read_text(encoding="utf-8")
    return [
        m.group("body")
        for m in re.finditer(
            r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, re.S | re.I
        )
        if "src=" not in m.group("attrs").lower()
    ]


def _csp() -> str:
    resp = _client().get("/")
    return resp.headers.get("content-security-policy", "")


def _script_src() -> str:
    for part in _csp().split(";"):
        if part.strip().startswith("script-src"):
            return part.strip()
    return ""


def test_every_inline_script_is_allowed_by_its_hash():
    """少一个哈希 = 那段脚本被静默拦掉，没有任何地方会报错。"""
    if not DIST_INDEX.exists():
        pytest.skip("没有构建产物（纯 API 部署）")
    bodies = _inline_bodies()
    assert bodies, "dist/index.html 里没有内联脚本了？那这条测试该删或改"

    script_src = _script_src()
    assert script_src, "CSP 里没有 script-src"
    for body in bodies:
        normalised = body.replace("\r\n", "\n").replace("\r", "\n")
        digest = base64.b64encode(hashlib.sha256(normalised.encode("utf-8")).digest())
        token = "'sha256-" + digest.decode("ascii") + "'"
        assert token in script_src, (
            "内联脚本没有对应的 CSP 哈希，它在浏览器里会被拦掉：\n"
            + body.strip()[:200]
        )


def test_the_hash_is_computed_on_lf_normalised_bytes():
    """CRLF 的工作区和 LF 的 Docker 构建必须算出同一个哈希。

    浏览器在脚本体成形之前就把 CRLF/CR 折成了 LF（HTML 输入流预处理），
    所以按原始字节算的那个哈希在 Windows 上永远对不上 —— 而对不上的表现
    就是回到今天这个「内联脚本不执行」的状态，不报错。
    """
    if not DIST_INDEX.exists():
        pytest.skip("没有构建产物")
    from web.backend.main import _inline_script_hashes

    computed = _inline_script_hashes()
    for body in _inline_bodies():
        crlf = body.replace("\n", "\r\n")
        raw_token = "'sha256-" + base64.b64encode(
            hashlib.sha256(crlf.encode("utf-8")).digest()
        ).decode("ascii") + "'"
        # 按 CRLF 原始字节算出来的那个**不该**出现 —— 出现就说明少了归一化。
        assert raw_token not in computed or crlf == body


def test_unsafe_inline_never_appears_in_script_src():
    """加哈希是为了**不用**开这个。开了等于把整类注入的口子还回去。"""
    script_src = _script_src()
    assert "unsafe-inline" not in script_src, script_src
    # 'self' 必须留着：外部那个带哈希文件名的 bundle 靠它加载。
    # （加 hash 不会取消 'self' 对外部脚本的授权，只有 'strict-dynamic' 会。）
    assert "'self'" in script_src, script_src


def test_a_missing_dist_does_not_break_startup():
    """纯 API 部署（没构建前端）不能因为读不到 index.html 就起不来。"""
    from web.backend.main import _inline_script_hashes

    import web.backend.main as m

    original = m._DIST_DIR
    try:
        m._DIST_DIR = pathlib.Path("does/not/exist")
        assert _inline_script_hashes() == []
    finally:
        m._DIST_DIR = original
