"""租户页「等级」列的标签 / 徽标 / 注册天数（web/frontend/src/utils/tier.ts）。

前端没有测试运行器；断言写在 `web/frontend/test/tier.check.mjs` 里，用 node 现编译
源码直接调用，由这条测试带进 Python 套件。没有 node（或没装前端依赖）的机器上跳过，
和 test_api_client_contract.py 同一个办法。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

HARNESS = pathlib.Path("web/frontend/test/tier.check.mjs")


def test_tier_labels_badges_and_account_age():
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node —— 等级标签检查跳过")
    if not pathlib.Path("web/frontend/node_modules/esbuild").exists():
        pytest.skip("web/frontend 没装依赖 —— 等级标签检查跳过")

    proc = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "0 失败" in (proc.stdout or ""), proc.stdout
    passed = int((proc.stdout or "").split("通过")[0].strip().split()[-1])
    assert passed >= 16, f"只跑了 {passed} 条，是不是被删了？\n{proc.stdout}"
