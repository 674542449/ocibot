"""前端 HTTP 客户端的契约 —— 用真实 HTTP 服务器跑一遍。

这个仓库**没有前端测试运行器**（`web/frontend/package.json` 里没有 `test` 脚本，
也没装 vitest），CLAUDE.md 把这件事记成「前端回归只能靠注释和人工审查」。
对绝大多数组件那还能接受，但 `src/api/client.ts` 不行：它在**每一个请求**的路径上。

0.4.111 用一个 fetch 封装替掉了 axios（入口包 -24%，那是唯一阻塞首屏的包）。
替换必须逐字复刻四件调用方依赖的事，而每一件失灵都是**静默**的：

  1. `headers` 是小写普通对象 —— 两处直接下标取 `res.headers['x-ocibot-reread']`，
     给 Headers 实例的话取到 undefined，「这次是 Oracle 瞬时故障」的提示就没了。
  2. `err.response = {status, data}` —— BackupView 靠 `data instanceof Blob` 解析
     导出失败的正文，LaunchView 靠 `status` 区分「被拒」和「响应丢了」。
  3. blob 请求失败时错误正文仍是 Blob。
  4. 超时消息里有 "timeout" —— LaunchView 用它决定要不要提示用户
     「机器可能已经开出来了，去列表看一眼」。

所以这些断言写在 `web/frontend/test/client.check.mjs` 里，用 node 起一台真服务器
跑 14 条，由这条测试带进 Python 套件。没有 node 的机器上跳过 —— 不是每个跑
后端测试的人都装了 node，为此让套件变红没有意义。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

HARNESS = pathlib.Path("web/frontend/test/client.check.mjs")


def test_the_api_client_still_honours_its_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node —— 前端客户端契约检查跳过")
    if not (pathlib.Path("web/frontend/node_modules/esbuild").exists()):
        pytest.skip("web/frontend 没装依赖 —— 前端客户端契约检查跳过")

    proc = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    # 断言条数别悄悄变少：harness 里 process.exit(fail ? 1 : 0)，
    # 一条都不跑同样是 returncode 0。
    assert "0 失败" in (proc.stdout or ""), proc.stdout
    passed = int((proc.stdout or "").split("通过")[0].strip().split()[-1])
    assert passed >= 14, f"契约检查只跑了 {passed} 条，是不是被删了？\n{proc.stdout}"


def test_axios_is_gone_from_the_frontend():
    """axios 占入口包 24%，而用到的能力只有几十行。

    装回来的话包体又涨回去，而这条测试是唯一会说话的地方。
    """
    pkg = pathlib.Path("web/frontend/package.json").read_text(encoding="utf-8")
    assert '"axios"' not in pkg, "axios 又被装回来了"
    src = pathlib.Path("web/frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "from 'axios'" not in src
