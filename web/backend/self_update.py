"""In-panel self-update (admin): check GitHub + apply host docker compose update.

Designed for the install.sh / docker-compose production layout:

- API container may mount the host repo at OCIBOT_HOST_DIR (default /host/ocibot)
- and the Docker socket at /var/run/docker.sock
- Apply runs: git pull + docker compose up -d --build on the host project

Status is stored in AppMeta so multi-worker API processes share progress.
"""

from __future__ import annotations

import json
import logging
import os
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

# Defaults match the public repo; override via env.
DEFAULT_REPO = "674542449/ocibot"
DEFAULT_BRANCH = "main"

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


def update_enabled() -> bool:
    """Opt-in. install.sh / compose sets OCIBOT_UPDATE_ENABLED=1."""
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
        # Dev / non-docker: try git describe in repo root
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


def capabilities() -> dict[str, Any]:
    host = _host_dir()
    sock = Path("/var/run/docker.sock")
    docker_bin = shutil.which("docker") or ""
    git_bin = shutil.which("git") or ""
    compose_file = host / "docker-compose.yml"
    return {
        "enabled": update_enabled(),
        "host_dir": str(host),
        "host_dir_exists": host.is_dir(),
        "compose_file_exists": compose_file.is_file(),
        "docker_sock": sock.exists(),
        "docker_bin": bool(docker_bin),
        "git_bin": bool(git_bin),
        "can_apply": bool(
            update_enabled()
            and host.is_dir()
            and compose_file.is_file()
            and sock.exists()
            and docker_bin
            and git_bin
        ),
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
    """Return latest commit on the configured branch (public GitHub API)."""
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
        # Cannot compare precisely; still surface remote info.
        update_available = True

    return {
        "enabled": caps["enabled"],
        "capabilities": caps,
        "local": local,
        "remote": remote,
        "update_available": update_available,
        "state": st.get("state") or "idle",  # idle|checking|running|success|error
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


def _compose_cmd(host: Path) -> list[str]:
    """Prefer `docker compose` plugin; fall back to docker-compose binary."""
    code, _ = _run_cmd(["docker", "compose", "version"], timeout=20)
    if code == 0:
        return ["docker", "compose", "-f", str(host / "docker-compose.yml")]
    if shutil.which("docker-compose"):
        return ["docker-compose", "-f", str(host / "docker-compose.yml")]
    return ["docker", "compose", "-f", str(host / "docker-compose.yml")]


def _append_log(existing: str, chunk: str, limit: int = 12000) -> str:
    text = (existing or "") + chunk
    if len(text) > limit:
        return text[-limit:]
    return text


def _apply_job(username: str) -> None:
    """Background thread body. Opens its own DB session."""
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
        caps_ok = (
            update_enabled()
            and host.is_dir()
            and (host / "docker-compose.yml").is_file()
            and Path("/var/run/docker.sock").exists()
            and shutil.which("docker")
            and shutil.which("git")
        )
        if not caps_ok:
            save(
                "error",
                "当前部署未启用在线更新（需要 OCIBOT_UPDATE_ENABLED=1、挂载宿主机仓库与 docker.sock、镜像内含 git/docker）",
                last_error="capabilities",
            )
            return

        # 1) git fetch/pull on host repo
        branch = _branch()
        code, out = _run_cmd(["git", "-C", str(host), "status", "--porcelain"], timeout=30)
        log_buf = _append_log(log_buf, f"$ git status\n{out}\n")
        if code != 0:
            save("error", "无法读取宿主机仓库 git 状态", last_error=out[-500:])
            return
        dirty = bool(out.strip())
        if dirty:
            log_buf = _append_log(log_buf, "[warn] working tree dirty; pull may fail\n")

        code, out = _run_cmd(["git", "-C", str(host), "fetch", "--depth", "1", "origin", branch], timeout=120)
        log_buf = _append_log(log_buf, f"$ git fetch origin {branch}\n{out}\n")
        save("running", "已 fetch，正在合并…", log_tail=log_buf)

        code, out = _run_cmd(["git", "-C", str(host), "checkout", branch], timeout=60)
        log_buf = _append_log(log_buf, f"$ git checkout {branch}\n{out}\n")
        code, out = _run_cmd(
            ["git", "-C", str(host), "pull", "--ff-only", "origin", branch],
            timeout=120,
        )
        log_buf = _append_log(log_buf, f"$ git pull --ff-only origin {branch}\n{out}\n")
        if code != 0:
            save(
                "error",
                "git pull 失败（可能有本地改动）。请 SSH 上服务器手动处理后再更新。",
                last_error=out[-800:],
                log_tail=log_buf,
            )
            return

        # Record new sha
        code, out = _run_cmd(["git", "-C", str(host), "rev-parse", "HEAD"], timeout=20)
        new_sha = out.strip() if code == 0 else ""
        log_buf = _append_log(log_buf, f"HEAD={new_sha}\n")

        # 2) docker compose build/up (recreates api/worker; db volume preserved)
        compose = _compose_cmd(host)
        save("running", "正在构建并重启容器（可能需几分钟）…", log_tail=log_buf)
        code, out = _run_cmd(
            [*compose, "up", "-d", "--build"],
            cwd=str(host),
            timeout=1200,
        )
        log_buf = _append_log(log_buf, f"$ {' '.join(compose)} up -d --build\n{out}\n")
        if code != 0:
            save(
                "error",
                "docker compose 更新失败，请查看日志或 SSH 手动执行 ./scripts/install.sh update",
                last_error=out[-800:],
                log_tail=log_buf,
            )
            return

        save(
            "success",
            f"更新完成（{new_sha[:7] or 'ok'}）。请强制刷新浏览器；若页面短暂不可用属正常现象。",
            log_tail=log_buf,
            applied_sha=new_sha,
        )
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
            "缺少更新条件：需要宿主机仓库挂载、docker.sock、以及镜像内 git/docker。"
            f" 详情：{json.dumps(caps, ensure_ascii=False)}"
        )

    global _worker
    with _lock:
        st = _read_status_raw(db)
        if st.get("state") == "running" or (_worker is not None and _worker.is_alive()):
            raise RuntimeError("已有更新任务正在进行")
        # Refresh remote tip best-effort before apply
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
