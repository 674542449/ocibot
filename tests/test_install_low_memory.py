"""小内存机器（2 核 1 GB）安装时卡死（0.4.155）。

在服务器上现场构建镜像会把 1 GB 内存耗尽：api 和 worker 两个镜像并行构建 = 同时跑两份
前端打包、两份 Oracle SDK 安装，没有交换空间时整机卡死。守住三件事：
  * install.sh 在内存 + 交换空间不足 3 GB 时自动加 2 GB 交换文件（各种跳过条件不误加）；
  * install.sh 和面板内一键更新都是一个一个构建，不并行；
  * worker 的构建参数和 api 一字不差 —— 否则第二个镜像不命中缓存，又要整份重建。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _function(name: str) -> str:
    text = INSTALL.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"install.sh 里找不到 {name}()"
    return m.group(0)


def _run_memory_check(tmp_path, *, mem_mb, swap_mb=0, free_gb=20, root=True, env="", existing=False):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: {mem_mb * 1024} kB\nSwapTotal: {swap_mb * 1024} kB\n")
    swapfile = tmp_path / "swapfile"
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    calls = tmp_path / "calls"
    calls.write_text("")
    if existing:
        swapfile.write_text("old")
    body = (
        _function("ensure_build_memory")
        .replace("/proc/meminfo", meminfo.as_posix())
        .replace("/etc/fstab", fstab.as_posix())
        .replace("/swapfile", swapfile.as_posix())
    )
    script = f"""
set -euo pipefail
CALLS={calls.as_posix()!r}
log()  {{ echo "LOG $*"; }}
warn() {{ echo "WARN $*"; }}
id() {{ echo {0 if root else 1000}; }}
df() {{ printf 'Filesystem 1K-blocks Used Available Use%% Mounted\\n/dev/x 1 1 {free_gb * 1024 * 1024} 1%% /\\n'; }}
fallocate() {{ : > "$3"; echo fallocate >> "$CALLS"; }}
mkswap() {{ echo mkswap >> "$CALLS"; }}
swapon() {{ echo "swapon $*" >> "$CALLS"; }}
swapoff() {{ echo "swapoff $*" >> "$CALLS"; }}
{env}
{body}
ensure_build_memory
"""
    proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout, calls.read_text().split(), fstab.read_text(), swapfile


def test_a_1gb_server_gets_a_persistent_swapfile(tmp_path):
    out, calls, fstab, swapfile = _run_memory_check(tmp_path, mem_mb=956)
    assert swapfile.exists()
    assert "mkswap" in calls and "swapon" in calls
    assert f"{swapfile.as_posix()} none swap sw 0 0" in fstab, "要写进 fstab，重启后仍有效"
    assert "已启用 2 GB 交换空间" in out


@pytest.mark.parametrize(
    "kw",
    [
        {"mem_mb": 4096},  # 内存够
        {"mem_mb": 1024, "swap_mb": 2048},  # 已经有足够的交换空间
    ],
)
def test_enough_memory_is_left_alone(tmp_path, kw):
    out, calls, fstab, swapfile = _run_memory_check(tmp_path, **kw)
    assert calls == [] and fstab == "" and not swapfile.exists()
    assert out == ""


def test_it_can_be_switched_off(tmp_path):
    out, calls, _, swapfile = _run_memory_check(tmp_path, mem_mb=956, env="OCIBOT_NO_SWAP=1")
    assert calls == [] and not swapfile.exists()
    assert "OCIBOT_NO_SWAP" in out


def test_without_root_it_only_warns(tmp_path):
    out, calls, _, swapfile = _run_memory_check(tmp_path, mem_mb=956, root=False)
    assert calls == [] and not swapfile.exists()
    assert "root" in out


def test_a_nearly_full_disk_is_not_filled_with_swap(tmp_path):
    out, calls, _, swapfile = _run_memory_check(tmp_path, mem_mb=956, free_gb=2)
    assert calls == [] and not swapfile.exists()
    assert "剩余不足" in out


def test_an_existing_swapfile_is_reenabled_not_recreated(tmp_path):
    out, calls, fstab, swapfile = _run_memory_check(tmp_path, mem_mb=956, existing=True)
    assert "fallocate" not in calls and "mkswap" not in calls
    assert any(c == "swapon" for c in calls)
    assert swapfile.read_text() == "old"


def test_images_are_built_one_at_a_time(tmp_path):
    calls = tmp_path / "calls"
    script = f"""
set -euo pipefail
log() {{ :; }}
compose() {{ echo "$*" >> {calls.as_posix()!r}; }}
{_function("build_images")}
build_images --pull
"""
    subprocess.run([BASH, "-c", script], check=True, capture_output=True, text=True)
    assert calls.read_text().split("\n")[:2] == ["build --pull api", "build --pull worker"]


def test_install_and_update_use_the_low_memory_path():
    text = INSTALL.read_text(encoding="utf-8")
    for fn in ("do_install", "do_update"):
        body = _function(fn)
        assert "ensure_build_memory" in body, fn
        assert "build_images" in body, fn
        assert not re.search(r"build( --pull)? api worker", body), f"{fn} 又在并行构建"
        assert "up -d --build\n" not in body, f"{fn}: up --build 会并行构建"


def test_the_in_panel_update_builds_one_at_a_time_too():
    src = (ROOT / "web" / "backend" / "self_update.py").read_text(encoding="utf-8")
    assert "for svc in api worker; do" in src
    assert not re.search(r"build( --pull)? api worker", src)


def test_worker_and_api_build_with_identical_arguments():
    """参数不一致的话 worker 不命中 api 的缓存，又要整份重建前端和依赖。"""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    api, worker = compose["services"]["api"]["build"], compose["services"]["worker"]["build"]
    assert api == worker
