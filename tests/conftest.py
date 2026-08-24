"""Test-suite wide environment.

`OCIBOT_REQUIRE_SECURE_SECRETS` 默认是 1（fail closed），所以任何 import
`web.backend.db` / `web.backend.main` 的测试模块都必须先有一对合格的密钥 —— db.py
在 import 时就调用 get_settings()，缺密钥时那是一个收集期异常，不是某条断言失败。

以前这靠每个测试文件自己在顶部 `os.environ.setdefault(...)`，于是能不能单独跑
`pytest tests/test_version_bump.py` 取决于该文件有没有抄那两行；在整套跑时又碰巧
被字母序更靠前的模块设好了环境而看不出来。这里集中声明一次：测试跑在"显式给了开发
用密钥"的姿态下，而不是"依赖不安全的默认值还能启动"。

用 setdefault 是为了让外部导出的真实密钥（例如 CI 里注入的）优先。
"""

from __future__ import annotations

import os

os.environ.setdefault("OCIBOT_MASTER_KEY", "test-suite-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "test-suite-jwt-secret-0123456789abcdef")
