"""In-panel self-update (admin): check GitHub + rebuild via host Docker.

Production layout (install.sh / docker-compose.yml):
- Host repo mounted at /host/ocibot inside the API container
- Env OCIBOT_HOST_REPO = absolute path *on the Docker host* (e.g. /root/ocibot)
- Docker socket mounted at /var/run/docker.sock

Compose is executed with the official ``docker:27-cli`` image (has the compose
plugin). The static ``docker`` binary shipped in the API image does not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from web.backend.config import get_settings
from web.backend.meta import get_meta, set_meta

log = logging.getLogger("ocibot.update")

KEY_UPDATE_STATUS = "self_update_status"
DEFAULT_REPO = "674542449/ocibot"
DEFAULT_BRANCH = "main"
DOCKER_CLI_IMAGE = (os.environ.get("OCIBOT_DOCKER_CLI_IMAGE") or "docker:27-cli").strip()

_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo() -> str:
    return (os.environ.get("OCIBOT_UPDATE_REPO") or DEFAULT_REPO).strip()


def _branch() -> str:
    return (os.environ.get("OCIBOT_UPDATE_BRANCH") or os.environ.get("OCIBOT_BRANCH") or DEFAULT_BRANCH).strip()


def _host_dir() -> Path:
    """Path to the git checkout *inside this container*."""
    raw = (os.environ.get("OCIBOT_HOST_DIR") or "/host/ocibot").strip()
    return Path(raw)


def _detect_host_bind_source(mountpoint: str = "/host/ocibot") -> str:
    """Resolve the host-side path that is bind-mounted at ``mountpoint``."""
    try:
        mp = str(Path(mountpoint).resolve())
        with open("/proc/self/mountinfo", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                # mountinfo: ... <mount point> ... - <fs type> <source> ...
                parts = line.split()
                if len(parts) < 10:
                    continue
                # Find separator '-'
                try:
                    dash = parts.index("-")
                except ValueError:
                    continue
                mnt = parts[4]
                if mnt.rstrip("/") != mp.rstrip("/"):
                    continue
                source = parts[dash + 2] if dash + 2 < len(parts) else ""
                # For bind mounts source looks like /root/ocibot or /dev/sda1; root path
                # often appears in field 3 as /root/ocibot relative to host root.
                root = parts[3]  # path relative to device root
                if source.startswith("/") and not source.startswith("/dev/"):
                    return source
                if root.startswith("/") and root not in {"/", "/host/ocibot"}:
                    # Overlay/bind: field 3 is the path on the host share
                    return root
                if root not in {"/"} and not root.startswith("/host"):
                    return root
    except Exception as exc:  # noqa: BLE001
        log.warning("mountinfo parse failed: %s", exc)
    return ""


def _host_repo_on_host() -> str:
    """Absolute path of the repo on the *Docker host* (for -v and compose binds)."""
    env = (os.environ.get("OCIBOT_HOST_REPO") or "").strip()
    if env and env not in {".", "./", "/host/ocibot"} and env.startswith("/"):
        return env
    detected = _detect_host_bind_source(str(_host_dir()))
    if detected and detected.startswith("/") and detected not in {"/host/ocibot"}:
        return detected
    # Last resort: common install location (cannot stat host paths from container).
    return env or detected or "/root/ocibot"


def update_enabled() -> bool:
    v = (os.environ.get("OCIBOT_UPDATE_ENABLED") or "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _load_dotenv_into_environ(path: Path) -> dict[str, str]:
    """Load KEY=VAL lines into os.environ (does not override existing). Returns loaded."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return loaded
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = val
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = val
    return loaded


def _read_status_raw(db: Session) -> dict[str, Any]:
    raw = get_meta(db, KEY_UPDATE_STATUS) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_status(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    data["updated_at"] = _utcnow()
    set_meta(db, KEY_UPDATE_STATUS, json.dumps(data, ensure_ascii=False))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return data


def local_build_info() -> dict[str, str]:
    settings = get_settings()
    sha = (os.environ.get("OCIBOT_GIT_SHA") or "").strip()
    if not sha or sha in {"unknown", "None"}:
        try:
            root = Path(__file__).resolve().parents[2]
            out = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            ).strip()
            sha = out or "unknown"
        except Exception:
            sha = "unknown"
    return {
        "app_version": settings.app_version,
        "git_sha": sha,
        "repo": _repo(),
        "branch": _branch(),
    }


def _run_cmd(cmd: list[str], *, cwd: Optional[str] = None, timeout: int = 600) -> tuple[int, str]:
    log.info("update cmd: %s (cwd=%s)", " ".join(cmd), cwd or "")
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return int(p.returncode), out
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (("\n" + (exc.stderr or "")) if exc.stderr else "")
        return 124, out + f"\n[timeout after {timeout}s]"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _docker_works() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "容器内找不到 docker 可执行文件"
    if not Path("/var/run/docker.sock").exists():
        return False, "未挂载 /var/run/docker.sock"
    code, out = _run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    if code != 0:
        return False, f"无法连接 Docker 守护进程：{out[-300:]}"
    return True, (out or "").strip()


def capabilities() -> dict[str, Any]:
    host = _host_dir()
    sock = Path("/var/run/docker.sock")
    docker_bin = bool(shutil.which("docker"))
    git_bin = bool(shutil.which("git"))
    compose_file = host / "docker-compose.yml"
    ok, detail = _docker_works() if docker_bin and sock.exists() else (False, "")
    host_repo = _host_repo_on_host()
    can = bool(
        update_enabled()
        and host.is_dir()
        and compose_file.is_file()
        and sock.exists()
        and docker_bin
        and git_bin
        and ok
        and host_repo.startswith("/")
    )
    return {
        "enabled": update_enabled(),
        "host_dir": str(host),
        "host_dir_exists": host.is_dir(),
        "compose_file_exists": compose_file.is_file(),
        "docker_sock": sock.exists(),
        "docker_bin": docker_bin,
        "git_bin": git_bin,
        "docker_daemon": ok,
        "docker_daemon_detail": detail,
        "host_repo_on_host": host_repo,
        "compose_via": f"container:{DOCKER_CLI_IMAGE}",
        "can_apply": can,
    }


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ocibot-self-update",
    }
    token = (os.environ.get("OCIBOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_remote_head(timeout: float = 15.0) -> dict[str, Any]:
    repo = _repo()
    branch = _branch()
    # Strict allowlist: owner/name only.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise RuntimeError(f"非法仓库名：{repo}")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch or "") or ".." in branch:
        raise RuntimeError(f"非法分支名：{branch}")
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        r = client.get(url, headers=_github_headers())
        if r.status_code == 404:
            raise RuntimeError(f"仓库或分支不存在：{repo}@{branch}")
        if r.status_code == 403:
            raise RuntimeError("GitHub API 限流或无权限（可设置 OCIBOT_GITHUB_TOKEN）")
        r.raise_for_status()
        data = r.json()
    sha = str(data.get("sha") or "")
    commit = data.get("commit") or {}
    msg = str((commit.get("message") or "").split("\n", 1)[0])
    date = str(((commit.get("author") or {}).get("date")) or "")
    html = str((data.get("html_url") or f"https://github.com/{repo}/commit/{sha}"))
    return {
        "sha": sha,
        "short_sha": sha[:7] if sha else "",
        "message": msg,
        "date": date,
        "html_url": html,
        "repo": repo,
        "branch": branch,
    }


def check_for_update(db: Session) -> dict[str, Any]:
    st = _read_status_raw(db)
    if st.get("state") == "running":
        return get_status(db)
    try:
        remote = fetch_remote_head()
        st["remote"] = remote
        st["checked_at"] = _utcnow()
        st["state"] = "idle"
        st["message"] = f"远程 {remote.get('short_sha')} · {remote.get('message')}"
        st["last_error"] = ""
        _write_status(db, st)
    except Exception as exc:  # noqa: BLE001
        st["state"] = "error" if st.get("state") != "running" else st.get("state")
        st["last_error"] = str(exc)
        st["message"] = f"检查更新失败：{exc}"
        st["checked_at"] = _utcnow()
        _write_status(db, st)
        raise
    return get_status(db)


def _append_log(existing: str, chunk: str, limit: int = 20000) -> str:
    text = (existing or "") + chunk
    if len(text) > limit:
        return text[-limit:]
    return text


def _project_name(host: Path) -> str:
    try:
        text = (host / "docker-compose.yml").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return host.name or "ocibot"


def _disk_free_gb(path: str = "/") -> Optional[float]:
    try:
        st = os.statvfs(path)
        return round((st.f_bavail * st.f_frsize) / (1024**3), 2)
    except Exception:
        return None


def _compose_env_flags(host_repo: str) -> list[str]:
    """Env for helper containers. Never inject empty secrets (empty overrides defaults)."""
    flags: list[str] = [
        "-e",
        f"OCIBOT_HOST_REPO={host_repo}",
        "-e",
        f"OCIBOT_GIT_SHA={os.environ.get('OCIBOT_GIT_SHA') or 'unknown'}",
        "-e",
        f"OCIBOT_PORT={os.environ.get('OCIBOT_PORT') or '8000'}",
        "-e",
        f"OCIBOT_API_WORKERS={os.environ.get('OCIBOT_API_WORKERS') or '2'}",
        "-e",
        "OCIBOT_UPDATE_ENABLED=1",
        "-e",
        f"OCIBOT_UPDATE_BRANCH={_branch()}",
        "-e",
        f"OCIBOT_UPDATE_REPO={_repo()}",
        "-e",
        f"OCIBOT_DOCKER_CLI_IMAGE={DOCKER_CLI_IMAGE}",
    ]
    for key in (
        "POSTGRES_PASSWORD",
        "OCIBOT_CORS_ORIGINS",
        "OCIBOT_MASTER_KEY",
        "OCIBOT_JWT_SECRET",
        "OCIBOT_REQUIRE_SECURE_SECRETS",
        "OCIBOT_COOKIE_SECURE",
        "OCIBOT_COOKIE_SAMESITE",
        "OCIBOT_DB_POOL_SIZE",
        "OCIBOT_DB_MAX_OVERFLOW",
        "OCIBOT_WORKER_ID",
    ):
        val = os.environ.get(key)
        if val is not None and str(val).strip() != "":
            flags.extend(["-e", f"{key}={val}"])
    return flags


def _compose_base_args(host_repo: str, project: str) -> list[str]:
    """compose argv prefix: always load web/.env so POSTGRES_PASSWORD is not lost."""
    args = [
        "compose",
        "-f",
        f"{host_repo}/docker-compose.yml",
        "-p",
        project,
    ]
    # Inside the API container the repo is at OCIBOT_HOST_DIR (/host/ocibot).
    # Inside the helper container the same files appear at host_repo.
    env_candidates = (
        _host_dir() / "web" / ".env",
        Path("/host/ocibot/web/.env"),
        Path(host_repo) / "web" / ".env",
    )
    if any(p.is_file() for p in env_candidates):
        args.extend(["--env-file", f"{host_repo}/web/.env"])
    return args


def _compose_via_cli_container(args: list[str], *, timeout: int = 1200) -> tuple[int, str]:
    """Run ``docker compose`` using docker:cli with host socket + same-path repo bind."""
    host_repo = _host_repo_on_host()
    # Project dir must exist on the HOST at host_repo. We bind that path into the
    # helper container at the *same* absolute path so compose volume sources like
    # ${OCIBOT_HOST_REPO}:/host/ocibot resolve correctly for the daemon.
    project = "ocibot"
    try:
        host = _host_dir()
        project = _project_name(host)
    except Exception:
        pass

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{host_repo}:{host_repo}",
        "-w",
        host_repo,
        *_compose_env_flags(host_repo),
        DOCKER_CLI_IMAGE,
        *_compose_base_args(host_repo, project),
        *args,
    ]
    return _run_cmd(cmd, timeout=timeout)


def _sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _write_restart_script(host: Path, host_repo: str, project: str, new_sha: str) -> Path:
    """Write a host-visible shell script that runs the same update as install.sh.

    Prefer: locate repo on host → ``bash scripts/install.sh update`` (via host
    namespace when possible). Fallback: pure ``docker compose build/up`` using the
    docker:cli image (no bash required).
    """
    scripts = host / "web" / "data"
    try:
        scripts.mkdir(parents=True, exist_ok=True)
    except Exception:
        scripts = host
    path = scripts / "self_update_restart.sh"
    # Pure POSIX sh — runs inside docker:cli (Alpine) with docker.sock.
    body = f"""#!/bin/sh
set -eu
REPO={_sh_quote(host_repo)}
PROJECT={_sh_quote(project)}
SHA={_sh_quote(new_sha or 'unknown')}
export OCIBOT_HOST_REPO="$REPO"
export OCIBOT_GIT_SHA="$SHA"
export OCIBOT_UPDATE_ENABLED=1
export OCIBOT_SKIP_GIT=1
cd "$REPO" || exit 1
if [ -f "$REPO/web/.env" ]; then
  set -- --env-file "$REPO/web/.env"
else
  set --
fi
log() {{ echo "[ocibot-update] $*"; }}
log "panel update begin repo=$REPO project=$PROJECT sha=$SHA"
log "step: docker compose build api worker"
if ! docker compose -f "$REPO/docker-compose.yml" -p "$PROJECT" "$@" build --pull api worker; then
  log "build --pull failed; retry without --pull"
  docker compose -f "$REPO/docker-compose.yml" -p "$PROJECT" "$@" build api worker
fi
log "step: docker compose up -d (full stack)"
docker compose -f "$REPO/docker-compose.yml" -p "$PROJECT" "$@" up -d
log "restart done"
docker compose -f "$REPO/docker-compose.yml" -p "$PROJECT" ps || true
# Best-effort health
sleep 3
if command -v wget >/dev/null 2>&1; then
  wget -qO- "http://127.0.0.1:${{OCIBOT_PORT:-8000}}/api/health" || true
elif command -v curl >/dev/null 2>&1; then
  curl -fsS "http://127.0.0.1:${{OCIBOT_PORT:-8000}}/api/health" || true
fi
log "panel update finished"
"""
    path.write_text(body, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o755)
    except Exception:
        pass
    return path


def _detach_host_install_sh(host_repo: str, new_sha: str) -> tuple[int, str]:
    """Try to run host ``bash scripts/install.sh update`` via nsenter into PID 1.

    This matches the operator SSH workflow: cd REPO && bash scripts/install.sh update.
    Requires privileged + pid=host (typical Docker CE on Linux VPS).
    """
    # Host-side one-liner; uses the host's bash/docker/git.
    remote_cmd = (
        f"export OCIBOT_HOST_REPO={_sh_quote(host_repo)}; "
        f"export OCIBOT_GIT_SHA={_sh_quote(new_sha or 'unknown')}; "
        f"export OCIBOT_UPDATE_ENABLED=1; "
        f"export OCIBOT_SKIP_GIT=1; "
        f"cd {_sh_quote(host_repo)} && bash scripts/install.sh update"
    )
    # No --rm: `docker run -d` returns 0 as soon as the container STARTS, so its
    # exit code says nothing about whether the update succeeded. Keeping the
    # container lets get_status() inspect its real exit code and logs afterwards.
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        "ocibot-self-update-restart",
        "--privileged",
        "--pid=host",
        # Small image; nsenter from util-linux. Pre-pull best-effort.
        "alpine:3.20",
        "sh",
        "-c",
        # install nsenter if missing, then enter host namespaces and run update
        "command -v nsenter >/dev/null 2>&1 || apk add --no-cache util-linux >/dev/null; "
        "nsenter -t 1 -m -u -i -n -- sh -c " + _sh_quote(remote_cmd),
    ]
    _run_cmd(["docker", "rm", "-f", "ocibot-self-update-restart"], timeout=30)
    # Ensure alpine present (best-effort, non-fatal if pull fails and image exists)
    _run_cmd(["docker", "image", "inspect", "alpine:3.20", "--format", "{{.Id}}"], timeout=20)
    code, out = _run_cmd(["docker", "pull", "alpine:3.20"], timeout=180)
    if code != 0:
        # still try local alpine
        log.warning("alpine pull failed: %s", out[-200:])
    return _run_cmd(cmd, timeout=60)


def _detach_stack_restart(host: Path, host_repo: str, project: str, new_sha: str) -> tuple[int, str]:
    """Start a detached helper that rebuilds/restarts the stack after API may die.

    Prefer host ``install.sh update`` (same as SSH). Fallback: compose via docker:cli.
    """
    # 1) Host install.sh (user-requested path)
    code, out = _detach_host_install_sh(host_repo, new_sha)
    if code == 0:
        return code, f"host install.sh path:\n{out}"

    log.warning("host install.sh detach failed (%s); falling back to compose script", out[-300:])
    # 2) Fallback: docker:cli + pure compose script on bound repo
    script = _write_restart_script(host, host_repo, project, new_sha)
    try:
        rel = script.relative_to(host).as_posix()
    except ValueError:
        rel = "web/data/self_update_restart.sh"
    script_on_host = f"{host_repo.rstrip('/')}/{rel}"

    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        "ocibot-self-update-restart",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{host_repo}:{host_repo}",
        "-w",
        host_repo,
        *_compose_env_flags(host_repo),
        "--entrypoint",
        "sh",
        DOCKER_CLI_IMAGE,
        script_on_host,
    ]
    _run_cmd(["docker", "rm", "-f", "ocibot-self-update-restart"], timeout=30)
    code2, out2 = _run_cmd(cmd, timeout=60)
    return code2, f"host install failed:\n{out}\ncompose fallback:\n{out2}"


def _humanize_build_error(out: str) -> str:
    low = (out or "").lower()
    tips: list[str] = []
    if "no space" in low or "disk quota" in low or "no space left" in low:
        tips.append("磁盘空间不足：在宿主机执行 df -h 与 docker system df，清理无用镜像 docker system prune -af")
    if "pull access denied" in low or "unauthorized" in low or "denied" in low and "pull" in low:
        tips.append("镜像拉取被拒绝：检查 Docker Hub / 镜像加速器配置")
    if "timeout" in low or "i/o timeout" in low or "tls handshake" in low or "connection reset" in low:
        tips.append("网络超时：OCI 机器访问 Docker Hub/GitHub 可能不稳定，可配置镜像加速或稍后重试")
    if "not found" in low and ("docker-compose" in low or "compose" in low):
        tips.append("compose 不可用：请升级到 0.4.2+ 或在宿主机执行 bash scripts/install.sh update")
    if "exec format error" in low or "no matching manifest" in low:
        tips.append("CPU 架构不匹配（arm64/amd64）：确认镜像支持当前架构")
    if "failed to resolve" in low or "name does not resolve" in low:
        tips.append("DNS 解析失败：检查 /etc/resolv.conf 与出网")
    if not tips:
        tips.append("请展开「更新日志」查看完整输出；也可 SSH 执行：cd ~/ocibot && bash scripts/install.sh update")
    return "；".join(tips)


def _apply_job(username: str) -> None:
    from web.backend.db import SessionLocal

    host = _host_dir()
    log_buf = ""
    with SessionLocal() as db:
        st = _read_status_raw(db)
        st.update(
            {
                "state": "running",
                "message": "正在更新…",
                "started_at": _utcnow(),
                "finished_at": "",
                "last_error": "",
                "triggered_by": username,
                "log_tail": "",
            }
        )
        _write_status(db, st)

    def save(state: str, message: str, **extra: Any) -> None:
        nonlocal log_buf
        with SessionLocal() as db:
            st = _read_status_raw(db)
            st["state"] = state
            st["message"] = message
            st["log_tail"] = log_buf
            for k, v in extra.items():
                st[k] = v
            if state in {"success", "error"}:
                st["finished_at"] = _utcnow()
            _write_status(db, st)

    try:
        # Load web/.env so compose interpolation gets POSTGRES_PASSWORD etc.
        _load_dotenv_into_environ(host / "web" / ".env")
        # Ensure host path env is correct for child compose
        host_repo = _host_repo_on_host()
        os.environ["OCIBOT_HOST_REPO"] = host_repo

        caps = capabilities()
        log_buf = _append_log(log_buf, f"capabilities={json.dumps(caps, ensure_ascii=False)}\n")
        free = _disk_free_gb("/")
        if free is not None:
            log_buf = _append_log(log_buf, f"container_disk_free_gb={free}\n")
            if free < 2.0:
                save(
                    "error",
                    f"磁盘空间不足（容器内可见约 {free} GB）。请在宿主机清理后重试。",
                    last_error="disk",
                    log_tail=log_buf,
                )
                return

        if not caps.get("can_apply"):
            save(
                "error",
                "当前部署未启用在线更新或条件不足（OCIBOT_UPDATE_ENABLED、仓库挂载、docker.sock）。",
                last_error=json.dumps(caps, ensure_ascii=False),
                log_tail=log_buf,
            )
            return

        branch = _branch()
        code, out = _run_cmd(["git", "-C", str(host), "rev-parse", "--short", "HEAD"], timeout=20)
        log_buf = _append_log(log_buf, f"local HEAD before: {out}\n")
        save("running", "正在 fetch 远程…", log_tail=log_buf)

        code, out = _run_cmd(
            ["git", "-C", str(host), "fetch", "--depth", "50", "origin", branch],
            timeout=180,
        )
        log_buf = _append_log(log_buf, f"$ git fetch origin {branch}\n{out}\n")
        if code != 0:
            code, out = _run_cmd(
                ["git", "-C", str(host), "fetch", "origin", branch],
                timeout=180,
            )
            log_buf = _append_log(log_buf, f"$ git fetch origin {branch} (full)\n{out}\n")
            if code != 0:
                save("error", "git fetch 失败（检查服务器访问 GitHub）", last_error=out[-800:], log_tail=log_buf)
                return

        env_src = host / "web" / ".env"
        env_bak = Path(f"/tmp/ocibot.env.backup.{os.getpid()}")
        if env_src.is_file():
            try:
                shutil.copy2(env_src, env_bak)
            except Exception as exc:  # noqa: BLE001
                log.warning("backup .env failed: %s", exc)

        code, out = _run_cmd(
            ["git", "-C", str(host), "checkout", "-B", branch, f"origin/{branch}"],
            timeout=60,
        )
        log_buf = _append_log(log_buf, f"$ git checkout -B {branch} origin/{branch}\n{out}\n")
        code, out = _run_cmd(
            ["git", "-C", str(host), "reset", "--hard", f"origin/{branch}"],
            timeout=60,
        )
        log_buf = _append_log(log_buf, f"$ git reset --hard origin/{branch}\n{out}\n")
        if code != 0:
            save(
                "error",
                "git reset 失败，请 SSH：cd ~/ocibot && git fetch && git reset --hard origin/main",
                last_error=out[-800:],
                log_tail=log_buf,
            )
            return

        if env_bak.is_file():
            try:
                (host / "web").mkdir(parents=True, exist_ok=True)
                shutil.copy2(env_bak, env_src)
                env_bak.unlink(missing_ok=True)
                log_buf = _append_log(log_buf, "restored web/.env\n")
                _load_dotenv_into_environ(env_src)
            except Exception as exc:  # noqa: BLE001
                log_buf = _append_log(log_buf, f"[warn] restore .env failed: {exc}\n")

        code, out = _run_cmd(["git", "-C", str(host), "rev-parse", "HEAD"], timeout=20)
        new_sha = out.strip() if code == 0 else ""
        if new_sha:
            os.environ["OCIBOT_GIT_SHA"] = new_sha
        log_buf = _append_log(log_buf, f"HEAD after reset={new_sha}\n")
        log_buf = _append_log(log_buf, f"host_repo_on_host={host_repo}\n")

        project = _project_name(host)
        # Hand off to host: cd $HOST_REPO && bash scripts/install.sh update
        # (same command operators use over SSH). Building inside this API process
        # used to die mid-restart; install.sh / compose helper run detached.
        save(
            "success",
            f"代码已对齐 {new_sha[:7] or 'ok'}，正在宿主机执行 install.sh update（构建+重启）…约 1–5 分钟后强刷。",
            log_tail=log_buf,
            applied_sha=new_sha,
        )
        code, out = _detach_stack_restart(host, host_repo, project, new_sha)
        log_buf = _append_log(log_buf, f"$ detach host update\n{out}\n")
        if code != 0:
            log_buf = _append_log(log_buf, "detach failed; synchronous compose up -d recovery\n")
            # Last chance: at least try to bring whatever image exists back up.
            code2, out2 = _compose_via_cli_container(["up", "-d"], timeout=600)
            log_buf = _append_log(log_buf, f"$ compose up -d (recovery)\n{out2}\n")
            if code2 != 0:
                save(
                    "error",
                    f"无法启动更新任务。请 SSH：cd {host_repo} && bash scripts/install.sh update。"
                    f" {_humanize_build_error(out2 or out)}",
                    last_error=(out2 or out)[-1500:],
                    log_tail=log_buf,
                )
                return
            save(
                "success",
                f"已通过恢复路径拉起现有服务；代码在磁盘上已是 {new_sha[:7] or 'ok'}。"
                f" 若版本未变请 SSH 执行 install.sh update。",
                log_tail=log_buf,
                applied_sha=new_sha,
            )
            return

        try:
            save(
                "success",
                f"已在宿主机排队更新（{new_sha[:7] or 'ok'}）。执行：cd {host_repo} && bash scripts/install.sh update。"
                f" 完成后约 1–5 分钟请 Ctrl+F5。",
                log_tail=log_buf,
                applied_sha=new_sha,
            )
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        log.exception("self-update failed")
        save("error", f"更新异常：{exc}", last_error=str(exc), log_tail=log_buf)
    finally:
        global _worker
        with _lock:
            _worker = None


def _worker_alive_unlocked() -> bool:
    global _worker
    return _worker is not None and _worker.is_alive()


def _status_age_sec(st: dict[str, Any]) -> Optional[float]:
    started = str(st.get("started_at") or "")
    if not started:
        return None
    try:
        ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def _recover_stale_running(
    st: dict[str, Any],
    *,
    max_age_sec: int = 45 * 60,
    worker_alive: Optional[bool] = None,
) -> dict[str, Any]:
    """If a previous update left state=running but no worker lives, clear it."""
    if st.get("state") != "running":
        return st
    if worker_alive is None:
        with _lock:
            alive = _worker_alive_unlocked()
    else:
        alive = worker_alive
    if alive:
        return st
    age = _status_age_sec(st)
    # No started_at → treat as stale. Otherwise only after max_age_sec.
    if age is not None and age <= max_age_sec:
        return st
    st = dict(st)
    st["state"] = "error"
    st["message"] = (
        "上次更新似乎中断（进程已退出）。若页面打不开请 SSH：bash scripts/install.sh update"
    )
    st["last_error"] = st.get("last_error") or "stale_running"
    if not st.get("finished_at"):
        st["finished_at"] = _utcnow()
    return st


def _helper_container_outcome() -> tuple[str, int, str]:
    """(status, exit_code, log_tail) of the detached update helper, if it still exists."""
    code, out = _run_cmd(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}} {{.State.ExitCode}}",
            "ocibot-self-update-restart",
        ],
        timeout=20,
    )
    if code != 0:
        return "", 0, ""
    parts = (out or "").strip().split()
    status = parts[0] if parts else ""
    try:
        exit_code = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        exit_code = 0
    logs = ""
    if status == "exited" and exit_code != 0:
        _, logs = _run_cmd(
            ["docker", "logs", "--tail", "200", "ocibot-self-update-restart"], timeout=30
        )
    return status, exit_code, logs or ""


def get_status(db: Session) -> dict[str, Any]:
    local = local_build_info()
    caps = capabilities()
    st = _read_status_raw(db)
    # The helper is launched detached, so "started successfully" was reported even
    # when the build/restart it performs failed. Reconcile against the container's
    # real exit code once the code on disk is still not what we applied.
    if st.get("state") == "success" and st.get("applied_sha"):
        applied = str(st.get("applied_sha") or "")
        current = str(local.get("git_sha") or "")
        if current and not applied.startswith(current) and not current.startswith(applied[:7]):
            status, exit_code, logs = _helper_container_outcome()
            if status == "exited" and exit_code != 0:
                st["state"] = "error"
                st["last_error"] = f"更新容器退出码 {exit_code}"
                st["message"] = (
                    f"宿主机更新任务失败（退出码 {exit_code}）。"
                    f"请 SSH 执行：cd {caps.get('host_repo_on_host')} && bash scripts/install.sh update"
                )
                if logs:
                    st["log_tail"] = _append_log(str(st.get("log_tail") or ""), "\n" + logs)
                try:
                    _write_status(db, st)
                except Exception:  # noqa: BLE001
                    pass
    recovered = _recover_stale_running(st)
    if recovered.get("state") != st.get("state"):
        try:
            _write_status(db, recovered)
            st = recovered
        except Exception:
            st = recovered
    remote = st.get("remote") if isinstance(st.get("remote"), dict) else None
    local_sha = (local.get("git_sha") or "").strip()
    remote_sha = str((remote or {}).get("sha") or "").strip()
    update_available = False
    if remote_sha and local_sha and local_sha not in {"unknown", "None"}:
        update_available = not (
            remote_sha.startswith(local_sha) or local_sha.startswith(remote_sha[:7])
        )
    elif remote_sha and local_sha in {"unknown", "None", ""}:
        update_available = True

    return {
        "enabled": caps["enabled"],
        "capabilities": caps,
        "local": local,
        "remote": remote,
        "update_available": update_available,
        "state": st.get("state") or "idle",
        "message": st.get("message") or "",
        "log_tail": st.get("log_tail") or "",
        "started_at": st.get("started_at") or "",
        "finished_at": st.get("finished_at") or "",
        "checked_at": st.get("checked_at") or "",
        "triggered_by": st.get("triggered_by") or "",
        "last_error": st.get("last_error") or "",
        "applied_sha": st.get("applied_sha") or "",
    }


def start_update(db: Session, *, username: str) -> dict[str, Any]:
    if not update_enabled():
        raise RuntimeError("在线更新未启用（OCIBOT_UPDATE_ENABLED≠1）")
    # Refresh dotenv + host path before capability check
    _load_dotenv_into_environ(_host_dir() / "web" / ".env")
    os.environ["OCIBOT_HOST_REPO"] = _host_repo_on_host()
    caps = capabilities()
    if not caps.get("can_apply"):
        raise RuntimeError(
            "缺少更新条件：需要宿主机仓库挂载、docker.sock、可用的 docker 守护进程。"
            f" 详情：{json.dumps(caps, ensure_ascii=False)}"
        )

    # Network call OUTSIDE the critical section so the mutual-exclusion window is
    # as short as possible.
    remote: Optional[dict[str, Any]] = None
    try:
        remote = fetch_remote_head()
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-update check failed: %s", exc)

    global _worker
    with _lock:
        st = _read_status_raw(db)
        alive = _worker_alive_unlocked()
        # Clear abandoned "running" markers (API was killed mid-update).
        recovered = _recover_stale_running(st, max_age_sec=20 * 60, worker_alive=alive)
        if recovered.get("state") != st.get("state"):
            st = recovered
            _write_status(db, st)

        if alive or st.get("state") == "running":
            raise RuntimeError("已有更新任务正在进行")

        # threading.Lock only covers THIS process, and the API runs
        # OCIBOT_API_WORKERS (default 2) of them, so two admins hitting apply at
        # once could both start a helper container. Re-read the status row with a
        # row lock inside the same transaction that flips it to "running", making
        # the DB row the real mutex. On SQLite with_for_update() is a no-op, but
        # that deployment is single-process anyway.
        if not get_settings().is_sqlite:
            try:
                from sqlalchemy import select

                from web.backend.models import AppMeta

                locked = db.scalar(
                    select(AppMeta).where(AppMeta.key == KEY_UPDATE_STATUS).with_for_update()
                )
                if locked is not None:
                    current = json.loads(locked.value or "{}")
                    if isinstance(current, dict) and current.get("state") == "running":
                        raise RuntimeError("已有更新任务正在进行")
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("update row lock unavailable: %s", exc)

        if remote is not None:
            st["remote"] = remote
            st["checked_at"] = _utcnow()
        st["state"] = "running"
        st["message"] = "已排队更新…"
        st["started_at"] = _utcnow()
        st["finished_at"] = ""
        st["triggered_by"] = username
        st["last_error"] = ""
        st["log_tail"] = ""
        _write_status(db, st)
        t = threading.Thread(target=_apply_job, args=(username,), name="ocibot-self-update", daemon=True)
        _worker = t
        t.start()
    return get_status(db)
