"""In-panel self-update (admin): check GitHub + apply host docker compose update.

Designed for the install.sh / docker-compose production layout:

- API container mounts the host repo at OCIBOT_HOST_DIR (default /host/ocibot)
- and the Docker socket at /var/run/docker.sock
- Apply runs on the host via: docker run --rm -v /var/run/docker.sock ...
  docker:cli  (has the compose plugin). Plain `docker` static binary inside the
  API image does NOT include `docker compose`, which caused build failures.

Status is stored in AppMeta so multi-worker API processes share progress.
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
# Official image that bundles the compose plugin (multi-arch).
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
    raw = (os.environ.get("OCIBOT_HOST_DIR") or "/host/ocibot").strip()
    return Path(raw)


def _host_repo_on_host() -> str:
    """Absolute path of the repo *on the Docker host* (for -v binds)."""
    return (os.environ.get("OCIBOT_HOST_REPO") or str(_host_dir())).strip() or "/host/ocibot"


def update_enabled() -> bool:
    v = (os.environ.get("OCIBOT_UPDATE_ENABLED") or "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


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


def _docker_works() -> bool:
    if not shutil.which("docker"):
        return False
    if not Path("/var/run/docker.sock").exists():
        return False
    code, _ = _run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
    return code == 0


def capabilities() -> dict[str, Any]:
    host = _host_dir()
    sock = Path("/var/run/docker.sock")
    docker_bin = bool(shutil.which("docker"))
    git_bin = bool(shutil.which("git"))
    compose_file = host / "docker-compose.yml"
    docker_ok = False
    try:
        docker_ok = _docker_works()
    except Exception:
        docker_ok = False
    can = bool(
        update_enabled()
        and host.is_dir()
        and compose_file.is_file()
        and sock.exists()
        and docker_bin
        and git_bin
        and docker_ok
    )
    return {
        "enabled": update_enabled(),
        "host_dir": str(host),
        "host_dir_exists": host.is_dir(),
        "compose_file_exists": compose_file.is_file(),
        "docker_sock": sock.exists(),
        "docker_bin": docker_bin,
        "git_bin": git_bin,
        "docker_daemon": docker_ok,
        "compose_via": "docker:cli-container",
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
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
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


def get_status(db: Session) -> dict[str, Any]:
    local = local_build_info()
    caps = capabilities()
    st = _read_status_raw(db)
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
    }


def check_for_update(db: Session) -> dict[str, Any]:
    st = _read_status_raw(db)
    if st.get("state") == "running":
        return get_status(db)
    try:
        remote = fetch_remote_head()
        st["remote"] = remote
        st["checked_at"] = _utcnow()
        st["state"] = st.get("state") if st.get("state") in {"running"} else "idle"
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


def _append_log(existing: str, chunk: str, limit: int = 16000) -> str:
    text = (existing or "") + chunk
    if len(text) > limit:
        return text[-limit:]
    return text


def _project_name(host: Path) -> str:
    # Prefer explicit name from compose file.
    try:
        text = (host / "docker-compose.yml").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"(?m)^name:\s*([A-Za-z0-9_-]+)\s*$", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    # Fallback: directory name (compose default)
    return host.name or "ocibot"


def _compose_via_cli_container(host: Path, args: list[str], *, timeout: int = 1200) -> tuple[int, str]:
    """Run `docker compose ...` using the official docker:cli image.

    The API image only ships a static docker client (no compose plugin). Spawning
    docker:cli with the host socket + host repo bind gives a reliable compose.
    """
    host_repo = _host_repo_on_host()
    project = _project_name(host)
    # Ensure the helper image exists (best-effort pull; offline cache ok).
    _run_cmd(["docker", "pull", DOCKER_CLI_IMAGE], timeout=180)
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
        "-e",
        f"OCIBOT_HOST_REPO={host_repo}",
        "-e",
        f"OCIBOT_GIT_SHA={os.environ.get('OCIBOT_GIT_SHA', 'unknown')}",
        "-e",
        f"POSTGRES_PASSWORD={os.environ.get('POSTGRES_PASSWORD', '')}",
        "-e",
        f"OCIBOT_PORT={os.environ.get('OCIBOT_PORT', '8000')}",
        "-e",
        f"OCIBOT_CORS_ORIGINS={os.environ.get('OCIBOT_CORS_ORIGINS', '')}",
        "-e",
        f"OCIBOT_API_WORKERS={os.environ.get('OCIBOT_API_WORKERS', '2')}",
        "-e",
        "OCIBOT_UPDATE_ENABLED=1",
        DOCKER_CLI_IMAGE,
        "compose",
        "-f",
        f"{host_repo}/docker-compose.yml",
        "-p",
        project,
        *args,
    ]
    return _run_cmd(cmd, timeout=timeout)


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
        caps = capabilities()
        if not caps.get("can_apply"):
            save(
                "error",
                "当前部署未启用在线更新（需要 OCIBOT_UPDATE_ENABLED=1、挂载宿主机仓库与 docker.sock、docker 可用）",
                last_error=json.dumps(caps, ensure_ascii=False),
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
                save("error", "git fetch 失败", last_error=out[-800:], log_tail=log_buf)
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
                "git reset 失败，请 SSH：git fetch && git reset --hard origin/main",
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
            except Exception as exc:  # noqa: BLE001
                log_buf = _append_log(log_buf, f"[warn] restore .env failed: {exc}\n")

        # Refresh OCIBOT_GIT_SHA for the upcoming build
        code, out = _run_cmd(["git", "-C", str(host), "rev-parse", "HEAD"], timeout=20)
        new_sha = out.strip() if code == 0 else ""
        if new_sha:
            os.environ["OCIBOT_GIT_SHA"] = new_sha
        log_buf = _append_log(log_buf, f"HEAD after reset={new_sha}\n")
        log_buf = _append_log(log_buf, f"host_repo_on_host={_host_repo_on_host()}\n")

        # Build images with docker:cli (has compose plugin)
        save("running", "正在构建镜像（首次可能较慢，请耐心等待）…", log_tail=log_buf)
        code, out = _compose_via_cli_container(
            host,
            ["build", "--pull", "api", "worker"],
            timeout=1800,
        )
        log_buf = _append_log(log_buf, f"$ compose build --pull\n{out}\n")
        if code != 0:
            code, out = _compose_via_cli_container(
                host,
                ["build", "api", "worker"],
                timeout=1800,
            )
            log_buf = _append_log(log_buf, f"$ compose build (retry no --pull)\n{out}\n")
            if code != 0:
                save(
                    "error",
                    "docker compose build 失败（详见日志）。常见原因：镜像源/网络、磁盘不足、架构不匹配。",
                    last_error=out[-1200:],
                    log_tail=log_buf,
                )
                return

        # Recreate only worker first, then api last — recreating api kills this process.
        save("running", "正在滚动重启 worker…", log_tail=log_buf)
        code, out = _compose_via_cli_container(
            host,
            ["up", "-d", "--no-deps", "worker"],
            timeout=300,
        )
        log_buf = _append_log(log_buf, f"$ compose up worker\n{out}\n")
        if code != 0:
            save(
                "error",
                "worker 启动失败",
                last_error=out[-800:],
                log_tail=log_buf,
            )
            return

        # Mark success BEFORE recreating api (this container will be replaced).
        save(
            "success",
            f"镜像已构建（{new_sha[:7] or 'ok'}），正在重启 API…请 30 秒后强制刷新（Ctrl+F5）。",
            log_tail=log_buf,
            applied_sha=new_sha,
        )

        # Detach: kick api recreate and exit. Use a sibling container so the
        # command survives after this API process is killed.
        host_repo = _host_repo_on_host()
        project = _project_name(host)
        kick = [
            "docker",
            "run",
            "-d",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{host_repo}:{host_repo}",
            "-w",
            host_repo,
            "-e",
            f"OCIBOT_HOST_REPO={host_repo}",
            "-e",
            f"OCIBOT_GIT_SHA={new_sha or 'unknown'}",
            "-e",
            f"POSTGRES_PASSWORD={os.environ.get('POSTGRES_PASSWORD', '')}",
            "-e",
            f"OCIBOT_PORT={os.environ.get('OCIBOT_PORT', '8000')}",
            DOCKER_CLI_IMAGE,
            "compose",
            "-f",
            f"{host_repo}/docker-compose.yml",
            "-p",
            project,
            "up",
            "-d",
            "--no-deps",
            "api",
        ]
        code, out = _run_cmd(kick, timeout=60)
        log_buf = _append_log(log_buf, f"$ detach compose up api\n{out}\n")
        # Best-effort final log write (may not complete if we die instantly).
        try:
            save(
                "success",
                f"更新已提交（{new_sha[:7] or 'ok'}）。API 正在重启，请稍后刷新。",
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


def start_update(db: Session, *, username: str) -> dict[str, Any]:
    if not update_enabled():
        raise RuntimeError("在线更新未启用（OCIBOT_UPDATE_ENABLED≠1）")
    caps = capabilities()
    if not caps.get("can_apply"):
        raise RuntimeError(
            "缺少更新条件：需要宿主机仓库挂载、docker.sock、以及可用的 docker 守护进程。"
            f" 详情：{json.dumps(caps, ensure_ascii=False)}"
        )

    global _worker
    with _lock:
        st = _read_status_raw(db)
        if st.get("state") == "running" or (_worker is not None and _worker.is_alive()):
            raise RuntimeError("已有更新任务正在进行")
        try:
            remote = fetch_remote_head()
            st["remote"] = remote
            st["checked_at"] = _utcnow()
        except Exception as exc:  # noqa: BLE001
            log.warning("pre-update check failed: %s", exc)
        st["state"] = "running"
        st["message"] = "已排队更新…"
        st["started_at"] = _utcnow()
        st["finished_at"] = ""
        st["triggered_by"] = username
        st["last_error"] = ""
        _write_status(db, st)
        t = threading.Thread(target=_apply_job, args=(username,), name="ocibot-self-update", daemon=True)
        _worker = t
        t.start()
    return get_status(db)
