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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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

# The detached helper that actually builds and restarts the stack, and the
# foreground compose container used by the recovery path. Both carry a fixed name
# because a name is the only handle left once the CLI that started them is gone:
# `_run_cmd`'s timeout kills the local `docker` process, not the container it
# started, and an unnamed orphan then keeps rewriting the same compose project
# while the operator is being told to SSH in and do it by hand.
HELPER_CONTAINER = "ocibot-self-update-restart"
RECOVERY_CONTAINER = "ocibot-self-update-compose"
HELPER_LABEL = "ocibot.self-update=1"

# Docker container states that mean "still doing the update". Anything else has
# settled (or the container is gone).
_LIVE_CONTAINER_STATES = {"created", "running", "restarting", "paused"}

# The helper does `compose build --pull` + `up -d`; a cold build on a small OCI
# instance is minutes, so the ceiling is generous. It only bounds the worker
# thread — the container itself is never killed by us.
_HELPER_POLL_SEC = 5
_HELPER_WAIT_MAX_SEC = 40 * 60


# Env vars whose VALUES must never reach a command line, a log line, or the
# admin-visible update log. OCIBOT_MASTER_KEY is the one that matters most: it
# derives the Fernet key that decrypts every stored OCI private key.
SECRET_ENV_KEYS = (
    "OCIBOT_MASTER_KEY",
    "OCIBOT_JWT_SECRET",
    "POSTGRES_PASSWORD",
    "OCIBOT_GITHUB_TOKEN",
    "GITHUB_TOKEN",
)


# Credentials embedded in a URL: `https://user:pass@host/…` or `https://ghp_x@…`.
# Env-name matching alone is not enough here. A private-repo install commonly bakes
# the PAT into the git remote itself, so the token lives in .git/config and NOT in
# OCIBOT_GITHUB_TOKEN — and any network blip makes git print
# `fatal: unable to access 'https://ghp_xxxx@github.com/owner/ocibot.git/'` verbatim
# into output that is persisted in app_meta and returned by GET /api/admin/update.
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@]+)@")
# Standalone GitHub tokens (also appear in `Authorization:` echoes and curl traces).
_GH_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")


def _redact(text: str) -> str:
    """Replace any configured secret value found in ``text`` with a placeholder.

    Defence in depth for everything that is logged or returned to an admin: the
    command builders below pass secrets by name rather than by value, but a
    docker/compose error message can still echo an interpolated value back.
    Short values are skipped — replacing a 3-character string would corrupt
    unrelated output without protecting anything.
    """
    out = text or ""
    for key in SECRET_ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if len(value) >= 8 and value in out:
            out = out.replace(value, f"***{key}***")
    out = _GH_TOKEN_RE.sub("***token***", out)
    return _URL_CREDENTIAL_RE.sub(r"\1***@", out)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo() -> str:
    return _checked_repo((os.environ.get("OCIBOT_UPDATE_REPO") or DEFAULT_REPO).strip())


def _branch() -> str:
    raw = (
        os.environ.get("OCIBOT_UPDATE_BRANCH")
        or os.environ.get("OCIBOT_BRANCH")
        or DEFAULT_BRANCH
    ).strip()
    return _checked_branch(raw)


def _checked_repo(repo: str) -> str:
    """owner/name only. Rejected values never reach an argv.

    The leading dash is refused for the same reason as the branch: it only
    lands in a URL and an `-e KEY=value` today, neither of which parses it as an
    option, but that is a property of the current call sites rather than of the
    value.
    """
    if repo.startswith("-") or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""
    ):
        raise RuntimeError(f"非法仓库名：{repo!r}")
    return repo


def _checked_branch(branch: str) -> str:
    """Branch name for `git fetch origin <branch>`.

    The leading-dash rejection is the part that matters: git parses an argument
    beginning with "-" as an option, so a branch of "--upload-pack=<cmd>" turns a
    fetch into arbitrary command execution. argv is a list here, so there is no
    shell to inject into — but there is still an option parser, and this runs on
    the update path, which is the highest privilege the panel has.
    """
    if (
        not branch
        or branch.startswith("-")
        or ".." in branch
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
    ):
        raise RuntimeError(f"非法分支名：{branch!r}")
    return branch


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


# Every admin-visible string in the status row, not just the log. `log_tail` was
# redacted at `_append_log` while `last_error` / `message` were written straight
# from `git fetch` / `compose` stdout+stderr — so the *same* failure showed
# ***OCIBOT_MASTER_KEY*** in one field and the value in the other. Redacting in the
# single funnel that persists the row means a new save() call site cannot miss it.
_REDACTED_STATUS_FIELDS = ("message", "last_error", "log_tail")


def _write_status(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    for field in _REDACTED_STATUS_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            data[field] = _redact(value)
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
    log.info("update cmd: %s (cwd=%s)", _redact(" ".join(cmd)), cwd or "")
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
    # _repo()/_branch() validate; both paths that build an argv share the rules.
    repo = _repo()
    branch = _branch()
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
        # Re-raised redacted: the admin router turns this into the 502 `detail`,
        # which is the one copy of the message that does not go through
        # _write_status. An httpx/git error can carry a credentialed URL.
        raise RuntimeError(_redact(str(exc))) from exc
    return get_status(db)


def _append_log(existing: str, chunk: str, limit: int = 20000) -> str:
    # Redacted on the way in: log_tail is persisted and returned by
    # GET /api/admin/update, and command output can echo interpolated values.
    text = (existing or "") + _redact(chunk)
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
    # Passed by NAME, not value: `docker run -e KEY` makes the CLI read KEY from
    # its own environment, which _run_cmd already forwards via env=. Writing
    # `-e KEY=value` put OCIBOT_MASTER_KEY into the argv — visible in the host
    # process table for the life of the call, and written verbatim into the API
    # log, since _run_cmd logs the command it runs. The master key derives the
    # Fernet key for every stored OCI private key, so a log shipper or a
    # `docker logs` was enough to walk away with all of them.
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
        # Access-mode keys (scripts/install.sh domain|ip). COMPOSE_PROFILES is the
        # one that is not plain interpolation: it decides whether the compose run
        # includes the `tls` profile at all, i.e. whether the HTTPS front end is
        # part of the stack being brought up. The API container receives all of
        # these from web/.env via env_file, so passing them BY NAME hands the
        # helper container the same view the operator's shell would have.
        "COMPOSE_PROFILES",
        "OCIBOT_DOMAIN",
        "OCIBOT_BIND",
        "OCIBOT_TRUST_PROXY",
        "OCIBOT_FORWARDED_ALLOW_IPS",
    ):
        val = os.environ.get(key)
        if val is not None and str(val).strip() != "":
            flags.extend(["-e", key])
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
        # Named even though this one runs in the foreground: _run_cmd's timeout
        # kills the local docker CLI, never the container, and this container is
        # running `compose up -d` (which builds when the image is missing — the
        # exact state after a failed detach). Without a name the survivor is an
        # invisible process rewriting the same compose project while the operator
        # is told the update failed and to run install.sh by hand.
        "--name",
        RECOVERY_CONTAINER,
        "--label",
        HELPER_LABEL,
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
        HELPER_CONTAINER,
        "--label",
        HELPER_LABEL,
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
    # `rm -f` on a *live* helper SIGKILLs an update that is mid `compose up -d`.
    # Only the leftover, already-exited record of the previous run may be removed.
    _assert_no_live_helper()
    _run_cmd(["docker", "rm", "-f", HELPER_CONTAINER], timeout=30)
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

    # No --rm here either: the container's exit code and logs are what
    # _helper_container_outcome() inspects to detect a failed update. The
    # `docker rm -f` below keeps the fixed name reusable.
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        HELPER_CONTAINER,
        "--label",
        HELPER_LABEL,
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
    _assert_no_live_helper()
    _run_cmd(["docker", "rm", "-f", HELPER_CONTAINER], timeout=30)
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


def _finish_after_helper(
    save: Callable[..., None],
    name: str,
    *,
    host_repo: str,
    new_sha: str,
    log_buf: str,
) -> None:
    """Hold the job open until ``name`` settles, then write the terminal state."""
    settled = _wait_for_helper(
        name, host_repo=host_repo, base={"applied_sha": new_sha, "log_tail": log_buf}
    )
    if settled is None:
        # Still running after the ceiling. We never kill it — a SIGKILL between
        # `compose up -d`'s db/api/worker steps is how the panel ends up with no
        # API container at all. Say so instead of sending the operator into a
        # concurrent install.sh.
        save(
            "error",
            f"宿主机更新任务超过 {_HELPER_WAIT_MAX_SEC // 60} 分钟仍未结束，容器 {name} 还在运行。"
            f" 请先 docker logs -f {name} 查看，不要在它结束前另外执行 install.sh update。",
            last_error="helper_timeout",
            log_tail=log_buf,
        )
        return
    save(
        settled.get("state") or "error",
        settled.get("message") or "",
        last_error=settled.get("last_error") or "",
        log_tail=settled.get("log_tail") or log_buf,
        applied_sha=new_sha,
    )


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
                # See start_update: a stale name here would be reconciled against
                # the previous run's container.
                "helper_container": "",
            }
        )
        _write_status(db, st)

    def save(state: str, message: str, **extra: Any) -> None:
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

        # Held in memory, never written to /tmp. The previous version copied
        # web/.env — which holds OCIBOT_MASTER_KEY, the JWT secret and the DB
        # password — to /tmp/ocibot.env.backup.<pid> and only deleted it on the
        # success path, so every early return after a failed git reset left the
        # master key sitting in a world-readable directory indefinitely. Same
        # reasoning as TenantSession keeping decrypted keys out of temp files.
        env_src = host / "web" / ".env"
        env_backup: Optional[bytes] = None
        if env_src.is_file():
            try:
                env_backup = env_src.read_bytes()
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

        if env_backup is not None:
            try:
                (host / "web").mkdir(parents=True, exist_ok=True)
                existed = env_src.is_file()
                env_src.write_bytes(env_backup)
                if not existed:
                    # Recreated by us, so it would otherwise take the process
                    # umask. This file holds the master key.
                    try:
                        env_src.chmod(0o600)
                    except OSError:
                        pass
                log_buf = _append_log(log_buf, "restored web/.env\n")
                _load_dotenv_into_environ(env_src)
            except Exception as exc:  # noqa: BLE001
                log_buf = _append_log(log_buf, f"[warn] restore .env failed: {exc}\n")
            finally:
                env_backup = None

        code, out = _run_cmd(["git", "-C", str(host), "rev-parse", "HEAD"], timeout=20)
        new_sha = out.strip() if code == 0 else ""
        # Deliberately NOT written into os.environ: local_build_info() reads
        # OCIBOT_GIT_SHA, so mutating it made this worker report the *target*
        # commit as the running build. get_status() then saw applied_sha ==
        # local git_sha and skipped the failed-update reconciliation, reporting
        # success even when the helper's build failed and the container was never
        # replaced. The helpers receive new_sha as an argument already, and
        # install.sh recomputes it from git regardless.
        log_buf = _append_log(log_buf, f"HEAD after reset={new_sha}\n")
        log_buf = _append_log(log_buf, f"host_repo_on_host={host_repo}\n")

        project = _project_name(host)
        # Hand off to host: cd $HOST_REPO && bash scripts/install.sh update
        # (same command operators use over SSH). Building inside this API process
        # used to die mid-restart; install.sh / compose helper run detached.
        #
        # State stays "running" across the whole handoff. It used to be flipped to
        # "success" HERE, before the helper had even been started: `docker run -d`
        # returns in seconds, this thread then exited and cleared _worker, and the
        # row no longer said "running" — so neither mutex (the in-process one nor
        # the SELECT … FOR UPDATE re-check, both of which only reject the literal
        # "running") excluded anything for the 1–5 minutes the host-side build was
        # still going. The SPA re-enabled its button on the same signal. A second
        # apply then `docker rm -f`d the live --privileged helper mid `compose up -d`
        # and git-reset the build context underneath it, which can destroy the API
        # container without recreating it — recovery by SSH only.
        save(
            "running",
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
            if code2 == 124 and _container_is_live(RECOVERY_CONTAINER):
                # The timeout reaped the local docker CLI, not the container: it is
                # still building/starting the stack. Telling the operator to SSH in
                # and run install.sh here would put two writers on the same compose
                # project. Report what is actually happening and keep waiting.
                save(
                    "running",
                    f"恢复任务仍在后台执行（容器 {RECOVERY_CONTAINER}）。"
                    f"请勿同时 SSH 执行 install.sh update——两者会同时改动同一套 compose 项目。"
                    f" 可用 docker logs -f {RECOVERY_CONTAINER} 观察进度。",
                    log_tail=log_buf,
                    applied_sha=new_sha,
                    helper_container=RECOVERY_CONTAINER,
                )
                _finish_after_helper(
                    save, RECOVERY_CONTAINER, host_repo=host_repo, new_sha=new_sha, log_buf=log_buf
                )
                return
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

        # helper_container is what lets ANOTHER API process (or this one after the
        # helper recreates our container mid-build) finish the job: get_status
        # reconciles a "running" row against that container's real exit code.
        save(
            "running",
            f"已在宿主机启动更新（{new_sha[:7] or 'ok'}）：cd {host_repo} && bash scripts/install.sh update。"
            f" 构建+重启约 1–5 分钟，完成后请 Ctrl+F5。",
            log_tail=log_buf,
            applied_sha=new_sha,
            helper_container=HELPER_CONTAINER,
        )
        _finish_after_helper(
            save, HELPER_CONTAINER, host_repo=host_repo, new_sha=new_sha, log_buf=log_buf
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("self-update failed")
        save("error", f"更新异常：{exc}", last_error=str(exc), log_tail=log_buf)
    finally:
        global _worker
        with _lock:
            _worker = None


def _worker_alive_unlocked() -> bool:
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


def _container_state(name: str) -> tuple[str, int]:
    """(docker state, exit code) for ``name``.

    "missing" and "unknown" are deliberately distinct. Only a daemon that answers
    *and* says the object does not exist proves the helper is gone; a failed
    inspect during the seconds when compose is recreating the stack must NOT be
    read as "finished", or a perfectly healthy update gets reported as failed.
    """
    code, out = _run_cmd(
        ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name],
        timeout=20,
    )
    if code != 0:
        low = (out or "").lower()
        if "no such object" in low or "no such container" in low:
            return "missing", 0
        return "unknown", 0
    parts = (out or "").strip().split()
    status = parts[0] if parts else "unknown"
    try:
        exit_code = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        exit_code = 0
    return status, exit_code


def _container_is_live(name: str) -> bool:
    return _container_state(name)[0] in _LIVE_CONTAINER_STATES


def _assert_no_live_helper() -> None:
    """Refuse to start anything while a previous helper is still working.

    `_detach_host_install_sh` opens with an unconditional `docker rm -f`, so a
    second apply SIGKILLs the first one's `--privileged --pid=host` container in
    the middle of `compose up -d` — which recreates db, then api, then worker, and
    when killed between them can leave the API container destroyed and never
    recreated. The status row is the primary mutex; this is the backstop for when
    the row has been cleared (stale-running recovery, a wiped app_meta, a manual
    edit) while the container is still very much alive.
    """
    for name in (HELPER_CONTAINER, RECOVERY_CONTAINER):
        if _container_is_live(name):
            raise RuntimeError(f"已有更新任务正在进行（容器 {name} 仍在运行）")


def _helper_container_outcome(name: str = HELPER_CONTAINER) -> tuple[str, int, str]:
    """(status, exit_code, log_tail) of the detached update helper, if it still exists."""
    status, exit_code = _container_state(name)
    logs = ""
    if status == "exited" and exit_code != 0:
        _, logs = _run_cmd(["docker", "logs", "--tail", "200", name], timeout=30)
    return status, exit_code, logs or ""


def _settle_from_helper(
    st: dict[str, Any], *, name: str, host_repo: str
) -> Optional[dict[str, Any]]:
    """Turn a finished helper container into a terminal status, or None if it is
    still running.

    Returning None is what keeps the mutex armed: the status row stays "running",
    which both `start_update` checks reject, until the container that is actually
    rewriting the stack has exited.
    """
    status, exit_code = _container_state(name)
    if status in _LIVE_CONTAINER_STATES or status == "unknown":
        return None

    st = dict(st)
    applied = str(st.get("applied_sha") or "")
    short = applied[:7] or "ok"
    if status == "exited" and exit_code == 0:
        st["state"] = "success"
        st["last_error"] = ""
        st["message"] = f"更新完成（{short}）。若界面未变化请 Ctrl+F5 强制刷新。"
    elif status == "exited":
        _, _, logs = _helper_container_outcome(name)
        st["state"] = "error"
        st["last_error"] = f"更新容器退出码 {exit_code}"
        st["message"] = (
            f"宿主机更新任务失败（退出码 {exit_code}）。"
            f"请 SSH 执行：cd {host_repo} && bash scripts/install.sh update"
        )
        if logs:
            st["log_tail"] = _append_log(str(st.get("log_tail") or ""), "\n" + logs)
    else:
        # "missing": the recovery container runs with --rm, so a clean finish
        # erases the exit code. The commit the API process reports is then the
        # only evidence of whether the new code is actually running.
        local_sha = (local_build_info().get("git_sha") or "").strip()
        landed = bool(
            applied
            and local_sha
            and local_sha not in {"unknown", "None"}
            and (applied.startswith(local_sha) or local_sha.startswith(applied[:7]))
        )
        if landed:
            st["state"] = "success"
            st["last_error"] = ""
            st["message"] = f"更新完成（{short}）。若界面未变化请 Ctrl+F5 强制刷新。"
        else:
            st["state"] = "error"
            st["last_error"] = "helper_container_missing"
            st["message"] = (
                f"更新任务容器已结束但无法确认结果，当前运行版本仍是 {local_sha or '未知'}。"
                f"请 SSH 执行：cd {host_repo} && bash scripts/install.sh update"
            )
    st["finished_at"] = _utcnow()
    return st


def _wait_for_helper(
    name: str,
    *,
    host_repo: str,
    base: dict[str, Any],
    max_wait_sec: int = _HELPER_WAIT_MAX_SEC,
    poll_sec: int = _HELPER_POLL_SEC,
) -> Optional[dict[str, Any]]:
    """Block the worker thread until the helper settles. None on timeout.

    The wait IS the fix for the "second apply kills the first" race: while this
    thread lives the in-process mutex holds, and the status row it left behind
    still says "running" for the other API worker processes. If our own container
    is recreated mid-wait (it usually is — the helper rebuilds api), this thread
    simply dies with it and `get_status` finishes the reconciliation instead.
    """
    deadline = time.monotonic() + max_wait_sec
    while True:
        settled = _settle_from_helper(base, name=name, host_repo=host_repo)
        if settled is not None:
            return settled
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_sec)


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

    # A "running" row that named a helper container is finished by whoever asks
    # next. The worker thread that started it normally does NOT survive to write
    # the terminal state — the update it launched recreates the api container it
    # lives in — so without this the row would sit at "running" until the 45-minute
    # stale sweep, blocking every further apply and never reporting the outcome.
    helper_busy = False
    if st.get("state") == "running" and st.get("helper_container"):
        with _lock:
            worker_alive = _worker_alive_unlocked()
        if not worker_alive:
            settled = _settle_from_helper(
                st,
                name=str(st.get("helper_container") or ""),
                host_repo=str(caps.get("host_repo_on_host") or ""),
            )
            if settled is None:
                helper_busy = True  # still building; keep the mutex armed
            else:
                st = settled
                try:
                    _write_status(db, st)
                except Exception:  # noqa: BLE001
                    pass

    # Stale-running recovery must not fire while the helper is demonstrably alive:
    # a slow cold build on a small instance can outlast max_age_sec, and declaring
    # it dead would re-open the apply button on top of a live update.
    recovered = st if helper_busy else _recover_stale_running(st)
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

    # Independent of any bookkeeping: if the previous update's container is still
    # working, a second apply would `docker rm -f` it mid `compose up -d` and
    # git-reset the build context it is reading. Checked before the row is touched
    # so the stale-running sweep below cannot clear the marker of a live update.
    _assert_no_live_helper()

    global _worker
    with _lock:
        st = _read_status_raw(db)
        alive = _worker_alive_unlocked()
        # Same reconciliation get_status does, so an apply is not refused for the
        # full stale window just because nobody polled the status after the helper
        # finished (the worker thread rarely survives to write it itself).
        if not alive and st.get("state") == "running" and st.get("helper_container"):
            settled = _settle_from_helper(
                st,
                name=str(st.get("helper_container") or ""),
                host_repo=str(caps.get("host_repo_on_host") or ""),
            )
            if settled is not None:
                st = settled
                _write_status(db, st)
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
                    select(AppMeta)
                    .where(AppMeta.key == KEY_UPDATE_STATUS)
                    .with_for_update()
                    # Without populate_existing the identity map returns the row
                    # this session already loaded, so the re-check inspected stale
                    # data and the cross-process mutex excluded nothing.
                    .execution_options(populate_existing=True)
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
        # Must be cleared: the previous run's container still exists (it is only
        # removed when the next helper starts), so a leftover name would make
        # get_status reconcile THIS run against the LAST run's exit code and call
        # it finished before the new helper has even been launched.
        st["helper_container"] = ""
        _write_status(db, st)
        t = threading.Thread(target=_apply_job, args=(username,), name="ocibot-self-update", daemon=True)
        _worker = t
        t.start()
    return get_status(db)
