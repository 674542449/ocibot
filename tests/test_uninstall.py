"""一键卸载（0.4.163）：curl …/install.sh | bash -s uninstall。

只问一件事 —— 数据删不删，因为只有它删了就回不来。守住这几件事：
  * 默认（直接回车 / 没有终端）保留数据：数据库卷和安装目录（web/.env 主密钥）一起留着；
  * 只有输入「删除」或 OCIBOT_PURGE_DATA=1 才删数据卷、证书卷、安装目录和第三方镜像；
  * 只删认得出是本面板的目录，家目录 / 根目录一律不删；
  * 不要求安装目录还在、不装 Docker、不克隆仓库：找不到目录也按标签把容器清掉。
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.test_install_low_memory import BASH, INSTALL, ROOT, _function

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

FUNCS = ("get_env_kv", "have_compose", "is_panel_dir", "find_panel_dir", "remove_docker_objects", "do_uninstall")


def _panel(tmp_path, *, project="ocibot", env_file=True):
    d = tmp_path / "ocibot"
    (d / "scripts").mkdir(parents=True)
    (d / "web" / "backend").mkdir(parents=True)
    (d / "docker-compose.yml").write_text(f"name: {project}\nservices: {{}}\n", encoding="utf-8")
    (d / "scripts" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (d / "web" / "backend" / "config.py").write_text("", encoding="utf-8")
    if env_file:
        (d / "web" / ".env").write_text(
            "POSTGRES_PASSWORD=x\nOCIBOT_POSTGRES_IMAGE=postgres:16-alpine@sha256:abc\n", encoding="utf-8"
        )
    return d


def _uninstall(tmp_path, repo_dir, *, answer="", docker=True, home=None, **env):
    calls = tmp_path / "calls"
    calls.write_text("")
    home = home or (tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    c = calls.as_posix()
    script = f"""
set -euo pipefail
log() {{ echo "LOG $*"; }}
warn() {{ echo "WARN $*"; }}
ask() {{ echo ASKED >> {c!r}; printf '%s' {answer!r}; }}
link_root_env() {{ :; }}
compose() {{ echo "compose $*" >> {c!r}; }}
APP_IMAGES="ocibot-api:latest ocibot-worker:latest"
PANEL_PROJECT=ocibot
COMPOSE="docker compose"
REPO_DIR={repo_dir.as_posix()!r}
{chr(10).join(_function(name) for name in FUNCS)}
have_docker() {{ return {0 if docker else 1}; }}
docker() {{
  echo "docker $*" >> {c!r}
  case "$1" in
    compose) [ "$2" = version ] ;;
    ps) printf 'c1\\nc2\\n' ;;
    network) if [ "$2" = ls ]; then echo n1; fi ;;
    volume) if [ "$2" = ls ]; then echo v1; fi ;;
    *) : ;;
  esac
}}
do_uninstall
"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OCIBOT_")}
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, encoding="utf-8",
        env={**clean, "HOME": str(home), **env},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout, calls.read_text(encoding="utf-8").splitlines()


def test_by_default_the_data_is_kept(tmp_path):
    d = _panel(tmp_path)
    out, calls = _uninstall(tmp_path, d)
    assert "ASKED" in calls
    assert "compose --profile tls down --remove-orphans" in calls, "不带 -v：数据卷留着"
    assert "docker rm -f c1 c2" in calls
    assert "docker network rm n1" in calls
    assert "docker image rm ocibot-api:latest" in calls and "docker image rm ocibot-worker:latest" in calls
    assert not [x for x in calls if x.startswith("docker volume rm")]
    assert not [x for x in calls if "postgres" in x], "保留数据时不删第三方镜像"
    assert d.exists() and (d / "web" / ".env").exists(), "主密钥要和数据库一起留着"
    assert "数据保留着" in out


@pytest.mark.parametrize("answer", ["", "y", "yes", "删", "删除 "])
def test_only_the_exact_word_deletes_data(tmp_path, answer):
    d = _panel(tmp_path)
    _, calls = _uninstall(tmp_path, d, answer=answer)
    assert d.exists()
    assert not [x for x in calls if x.startswith("docker volume rm")]


def test_typing_the_word_deletes_everything(tmp_path):
    d = _panel(tmp_path)
    out, calls = _uninstall(tmp_path, d, answer="删除")
    assert "compose --profile tls down -v --remove-orphans" in calls
    assert "docker volume rm v1" in calls
    assert "docker volume rm ocibot_pg ocibot_caddy_data ocibot_caddy_config" in calls, "早期没有标签的卷也删"
    assert "docker image rm postgres:16-alpine@sha256:abc" in calls, "以 web/.env 里钉的为准（删目录前读）"
    assert "docker image rm caddy:2-alpine" in calls
    assert not d.exists()
    assert "已删除安装目录" in out


def test_purge_data_does_not_ask(tmp_path):
    d = _panel(tmp_path)
    _, calls = _uninstall(tmp_path, d, OCIBOT_PURGE_DATA="1")
    assert "ASKED" not in calls
    assert not d.exists()


def test_a_missing_install_dir_still_cleans_up_containers(tmp_path):
    """目录早就删了、只剩容器在跑 —— 以前 ensure_repo 会直接报错退出（或者去克隆一份）。"""
    out, calls = _uninstall(tmp_path, tmp_path / "gone", answer="删除")
    assert not [x for x in calls if x.startswith("compose ")]
    assert "docker rm -f c1 c2" in calls
    assert "docker volume rm v1" in calls
    assert "卸载完成" in out


def test_a_directory_that_is_not_the_panel_is_never_deleted(tmp_path):
    d = _panel(tmp_path, project="someone-else")
    _, calls = _uninstall(tmp_path, d, answer="删除")
    assert d.exists()
    assert not [x for x in calls if x.startswith("compose ")], "认不出的目录也不拿它的 compose 文件去 down"


def test_the_home_directory_is_never_deleted(tmp_path):
    d = _panel(tmp_path)
    out, _ = _uninstall(tmp_path, d, answer="删除", home=d)
    assert d.exists()
    assert "太危险" in out


def test_without_docker_it_does_not_install_docker(tmp_path):
    d = _panel(tmp_path)
    out, calls = _uninstall(tmp_path, d, answer="删除", docker=False)
    assert not [x for x in calls if x.startswith(("docker ", "compose "))]
    assert not d.exists()
    assert "没有找到 Docker" in out


def test_uninstall_needs_neither_docker_nor_the_repo():
    body = _function("do_uninstall")
    for forbidden in ("ensure_docker", "ensure_repo", "detect_compose", "die "):
        assert forbidden not in body, f"卸载不能 {forbidden}：没有 Docker 时会去装、没有代码时会去克隆或退出"


def test_the_project_name_matches_docker_compose():
    text = INSTALL.read_text(encoding="utf-8")
    m = re.search(r'^PANEL_PROJECT="([^"]+)"', text, re.M)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert m and re.search(rf"^name: {re.escape(m.group(1))}$", compose, re.M)


def test_the_one_line_uninstall_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/install.sh | bash -s uninstall" in readme
    assert "scripts/install.sh | bash -s uninstall" in INSTALL.read_text(encoding="utf-8")
