"""1 核 1 GB 的服务器也能安装：用 GitHub 预先构建好的镜像，不在本机构建（0.4.160）。

在服务器上现场构建要打包前端、装 Oracle SDK，1 GB 内存吃不消（0.4.155 加了交换空间也只是
「慢慢熬过去」）。现在推送到 main 后 GitHub Actions 把镜像构建好发到 ghcr.io，install.sh
先下载和本机代码同一个提交的那份，下载不到才退回本机构建。守住这几件事：
  * 只用和本机代码同一个提交的镜像，并核对烤进镜像里的提交号；
  * 下载不到的各种情况（没公开、没开 Actions、刚推送还在构建、钉了基础镜像、OCIBOT_PREBUILT=0）
    都退回本机构建，而不是装一个对不上的版本或者直接失败；
  * 打上的名字和 docker-compose.yml 里的 image: 一致，compose 才不会再去构建；
  * 工作流两种架构都发，Action 钉在提交上。
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.test_install_low_memory import BASH, INSTALL, ROOT, _function

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE = "ghcr.io/674542449/ocibot"


def _bash(script: str, **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OCIBOT_")}
    out = subprocess.run(
        [BASH, "-c", script], capture_output=True, encoding="utf-8", env={**clean, **env}, check=True
    )
    return out.stdout


# ---------------------------------------------------------------- 镜像仓库地址


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/674542449/ocibot.git", IMAGE),
        ("https://github.com/674542449/ocibot", IMAGE),
        ("https://github.com/Some-User/OciBot.git/", "ghcr.io/some-user/ocibot"),
        ("git@github.com:a/b.git", "ghcr.io/a/b"),
        ("ssh://git@github.com/a/b.git", "ghcr.io/a/b"),
        ("https://x-access-token:abc@github.com/a/b.git", "ghcr.io/a/b"),
        ("https://gitlab.com/a/b.git", "NONE"),
        ("https://github.com/a/b/c", "NONE"),
        ("", "NONE"),
    ],
)
def test_the_image_repository_follows_the_git_remote(url, expected):
    """fork 用自己仓库的镜像；ghcr 只认小写。"""
    script = f"""
set -euo pipefail
REPO_DIR=/repo
git() {{ printf '%s\\n' "$URL"; }}
get_env_kv() {{ :; }}
{_function("prebuilt_image_ref")}
prebuilt_image_ref || echo NONE
"""
    assert _bash(script, URL=url).strip() == expected


def test_the_image_repository_can_be_overridden():
    script = f"""
set -euo pipefail
REPO_DIR=/repo
git() {{ echo https://github.com/674542449/ocibot.git; }}
get_env_kv() {{ :; }}
{_function("prebuilt_image_ref")}
prebuilt_image_ref
"""
    assert _bash(script, OCIBOT_IMAGE="registry.example.com/me/panel").strip() == "registry.example.com/me/panel"


# ---------------------------------------------------------------- 下载 / 核对 / 退回


def _run(
    tmp_path, *, have_main=True, exists=True, sha_after=0, cli_reach=True, pull_failures=0,
    baked=SHA, env_kv=None, **env,
):
    """``exists``：仓库里有没有这个提交的镜像；``sha_after``：第几次问起才有（模拟还在构建）；
    ``cli_reach``：docker 命令行连不连得上镜像仓库（manifest inspect）；``pull_failures``：
    前几次下载失败（模拟断在半路）。下载只看镜像在不在，不看命令行连不连得上 —— 走的是 docker 服务。"""
    calls, polls, pulls = tmp_path / "calls", tmp_path / "polls", tmp_path / "pulls"
    calls.write_text("")
    polls.write_text("0")
    pulls.write_text("0")
    kv = "".join(f"    {k}) echo '{v}' ;;\n" for k, v in (env_kv or {}).items())
    script = f"""
set -euo pipefail
log() {{ echo "LOG $*"; }}
warn() {{ echo "WARN $*"; }}
APP_IMAGES="ocibot-api:latest ocibot-worker:latest"
OCIBOT_GIT_SHA={SHA}
REPO_DIR=/repo
git() {{ echo https://github.com/674542449/ocibot.git; }}
get_env_kv() {{
  case "$1" in
{kv}    *) : ;;
  esac
}}
sleep() {{ :; }}
docker() {{
  echo "$*" >> {calls.as_posix()!r}
  case "$1 $2" in
    "manifest inspect")
      [ {int(cli_reach)} = 1 ] || return 1
      case "$3" in
        *:main) [ {int(have_main)} = 1 ] ;;
        *) n=$(cat {polls.as_posix()!r}); echo $((n + 1)) > {polls.as_posix()!r}
           [ {int(exists)} = 1 ] && [ "$n" -ge {sha_after} ] ;;
      esac ;;
    "pull "*)
      [ {int(exists)} = 1 ] || return 1
      p=$(cat {pulls.as_posix()!r}); echo $((p + 1)) > {pulls.as_posix()!r}
      [ "$p" -ge {pull_failures} ] ;;
    "image inspect") printf 'PATH=/usr/bin\\nOCIBOT_GIT_SHA=%s\\n' '{baked}' ;;
    *) : ;;
  esac
}}
{_function("prebuilt_image_ref")}
{_function("wait_for_prebuilt")}
{_function("use_prebuilt_images")}
if use_prebuilt_images; then echo RESULT=used; else echo RESULT=fallback; fi
"""
    out = _bash(script, **env)
    return out, calls.read_text().splitlines(), int(polls.read_text())


def test_the_image_for_this_commit_is_used(tmp_path):
    out, calls, _ = _run(tmp_path)
    assert "RESULT=used" in out
    assert f"pull {IMAGE}:{SHA}" in calls
    assert f"tag {IMAGE}:{SHA} ocibot-api:latest" in calls
    assert f"tag {IMAGE}:{SHA} ocibot-worker:latest" in calls
    assert f"image rm {IMAGE}:{SHA}" in calls, "下载用的那个标签去掉，免得 docker images 里越积越多"


def test_an_image_built_from_other_code_is_not_used(tmp_path):
    """标签可以被重新指向，烤进镜像里的提交号才说明它是从哪份代码构建的。"""
    out, calls, _ = _run(tmp_path, baked="f" * 40)
    assert "RESULT=fallback" in out and "对不上" in out
    assert not [c for c in calls if c.startswith("tag ")]


def _pulls(calls):
    return [c for c in calls if c.startswith("pull ")]


def test_a_repository_without_images_falls_back_at_once(tmp_path):
    """没公开 / 没开 Actions / fork：:main 都没有，就别干等十分钟。直接下载试一次（失败得很快）。"""
    out, calls, polls = _run(tmp_path, have_main=False, exists=False)
    assert "RESULT=fallback" in out and "没有找到预构建镜像" in out
    assert polls == 1
    assert len(_pulls(calls)) == 1, "不存在的镜像不用重试"


def test_a_cli_that_cannot_reach_the_registry_still_downloads(tmp_path):
    """manifest inspect 走 docker 命令行自己的网络设置；只给 docker 服务配了代理的机器上它会失败，
    下载（走 docker 服务）却能成功。以前这种机器直接退回本机构建。"""
    out, calls, _ = _run(tmp_path, cli_reach=False)
    assert "RESULT=used" in out
    assert len(_pulls(calls)) == 1


def test_a_fresh_push_is_waited_for(tmp_path):
    """推送后 GitHub 要构建几分钟，紧接着运行 update 时这个提交的镜像还没有。"""
    out, calls, polls = _run(tmp_path, sha_after=2)
    assert "RESULT=used" in out
    assert polls == 3
    assert "还在构建" in out


def test_the_wait_is_bounded(tmp_path):
    out, calls, polls = _run(tmp_path, sha_after=10**6, OCIBOT_PREBUILT_WAIT="90")
    assert "RESULT=fallback" in out
    assert polls == 1 + 3
    assert not _pulls(calls)


def test_an_interrupted_download_is_retried_once(tmp_path):
    """退回本机构建正是 1 GB 的机器扛不住的那条路；断在半路再试一次，已下完的层不用重下。"""
    out, calls, _ = _run(tmp_path, pull_failures=1)
    assert "RESULT=used" in out
    assert len(_pulls(calls)) == 2


def test_a_failed_download_falls_back(tmp_path):
    out, calls, _ = _run(tmp_path, pull_failures=99)
    assert "RESULT=fallback" in out
    assert len(_pulls(calls)) == 2
    assert not [c for c in calls if c.startswith("tag ")]


def test_a_registry_the_cli_cannot_reach_does_not_abort_the_install(tmp_path):
    """wait_for_prebuilt 返回 2 后要接着去「直接下载一次」。不放在 if 里调用时，`wait_for_prebuilt
    …; case $?` 这种写法会被 set -e 当场停掉，下载那一步根本走不到。"""
    script = f"""
set -euo pipefail
log() {{ :; }}
warn() {{ :; }}
APP_IMAGES="ocibot-api:latest ocibot-worker:latest"
OCIBOT_GIT_SHA={SHA}
REPO_DIR=/repo
git() {{ echo https://github.com/674542449/ocibot.git; }}
get_env_kv() {{ :; }}
docker() {{
  case "$1 $2" in
    "manifest inspect") return 1 ;;
    "image inspect") printf 'OCIBOT_GIT_SHA=%s\\n' {SHA} ;;
    *) : ;;
  esac
}}
{_function("prebuilt_image_ref")}
{_function("wait_for_prebuilt")}
{_function("use_prebuilt_images")}
use_prebuilt_images
echo still-running
"""
    assert _bash(script).split() == ["still-running"]


@pytest.mark.parametrize("where", ["env", "web/.env"])
def test_prebuilt_images_can_be_turned_off(tmp_path, where):
    kw = {"OCIBOT_PREBUILT": "0"} if where == "env" else {"env_kv": {"OCIBOT_PREBUILT": "0"}}
    out, calls, _ = _run(tmp_path, **kw)
    assert "RESULT=fallback" in out
    assert calls == []


def test_pinned_base_images_are_honoured(tmp_path):
    """钉了基础镜像的人要的是按钉的版本构建；预构建的镜像用的是构建当时的最新版。"""
    pinned = {"OCIBOT_PYTHON_IMAGE": "python:3.12-slim@sha256:" + "a" * 64}
    out, calls, _ = _run(tmp_path, env_kv=pinned)
    assert "RESULT=fallback" in out and calls == []
    out, _, _ = _run(tmp_path, env_kv={**pinned, "OCIBOT_PREBUILT": "1"})
    assert "RESULT=used" in out


def test_install_and_update_try_the_prebuilt_image_first():
    for fn in ("do_install", "do_update"):
        body = _function(fn)
        m = re.search(r"remember_app_images\n\s*if ! use_prebuilt_images; then\n(.*?)\n\s*fi\n", body, re.S)
        assert m, fn
        fallback = m.group(1)
        assert "ensure_build_memory" in fallback and "build_images" in fallback, fn
        assert body.count("ensure_build_memory") == 1, f"{fn}: 用上预构建镜像时不用加交换空间"
        assert body.index("remove_replaced_app_images") > body.rindex("compose up -d"), fn


def test_the_names_match_docker_compose():
    """打上的名字和 compose 的 image: 对不上的话，compose 看不到镜像，又去本机构建。"""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    names = [compose["services"][svc]["image"] for svc in ("api", "worker")]
    m = re.search(r'^APP_IMAGES="([^"]+)"', INSTALL.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1).split() == names


# ---------------------------------------------------------------- 换下来的旧镜像


def test_only_replaced_images_are_removed(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("")
    script = f"""
set -euo pipefail
APP_IMAGES="ocibot-api:latest ocibot-worker:latest"
docker() {{
  echo "$*" >> {calls.as_posix()!r}
  case "$*" in
    "image inspect --format {{{{.Id}}}} "*) printf 'sha256:old\\nsha256:shared\\n' ;;
    *"{{{{len .RepoTags}}}}"*sha256:old) echo 0 ;;
    *"{{{{len .RepoTags}}}}"*sha256:shared) echo 1 ;;
    *) : ;;
  esac
}}
{_function("remember_app_images")}
{_function("remove_replaced_app_images")}
remember_app_images
remove_replaced_app_images
"""
    _bash(script)
    removed = [c for c in calls.read_text().splitlines() if c.startswith("image rm")]
    assert removed == ["image rm sha256:old"], "还有名字的（还在用的）不能删"


def test_no_images_yet_is_fine(tmp_path):
    """全新安装时一个镜像都还没有：inspect 失败不能让 set -e 把安装停掉。"""
    script = f"""
set -euo pipefail
APP_IMAGES="ocibot-api:latest ocibot-worker:latest"
docker() {{ return 1; }}
{_function("remember_app_images")}
{_function("remove_replaced_app_images")}
remember_app_images
remove_replaced_app_images
echo ok
"""
    assert _bash(script).strip() == "ok"


# ---------------------------------------------------------------- 工作流


def _workflow():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8"))


def test_the_workflow_publishes_both_architectures_for_every_push_to_main():
    wf = _workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 把 on 读成 True
    assert on == {"push": {"branches": ["main"]}}
    jobs = wf["jobs"]
    assert {jobs["amd64"]["runs-on"], jobs["arm64"]["runs-on"]} == {"ubuntu-24.04", "ubuntu-24.04-arm"}
    for arch in ("amd64", "arm64"):
        build = next(s for s in jobs[arch]["steps"] if s.get("id") == "build")["with"]
        assert build["platforms"] == f"linux/{arch}"
        assert build["file"] == "web/Dockerfile"
        assert build["build-args"] == "OCIBOT_GIT_SHA=${{ github.sha }}", "install.sh 要核对这个提交号"
        assert "push-by-digest=true" in build["outputs"], "半成品不能以提交号的名义出现"
    run = jobs["merge"]["steps"][-1]["run"]
    assert '-t "$IMAGE:$SHA"' in run and '-t "$IMAGE:main"' in run
    assert "${ref}:main" in _function("wait_for_prebuilt"), "install.sh 用 :main 判断仓库发没发过镜像"


def test_the_workflow_holds_minimal_permissions_and_pinned_actions():
    wf = _workflow()
    assert wf["permissions"] == {}
    for name, job in wf["jobs"].items():
        assert set(job["permissions"]) <= {"contents", "packages"}, name
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
