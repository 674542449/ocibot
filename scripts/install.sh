#!/usr/bin/env bash
# OCIBot one-click installer / updater (Docker Compose + PostgreSQL)
#
# Install (blank server):
#   curl -fsSL https://raw.githubusercontent.com/<OWNER>/<REPO>/master/scripts/install.sh | bash
#
# Or from a clone:
#   ./scripts/install.sh install
#   ./scripts/install.sh update
#   ./scripts/install.sh status
#   ./scripts/install.sh uninstall
#
set -euo pipefail

REPO_URL="${OCIBOT_REPO_URL:-}"
REPO_DIR="${OCIBOT_DIR:-$HOME/ocibot}"
BRANCH="${OCIBOT_BRANCH:-main}"
COMPOSE="docker compose"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

rand_hex() {
  # 48 bytes → 96 hex chars
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 48
  else
    head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "需要 Docker Compose 插件（docker compose）或 docker-compose"
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 Docker，尝试安装（需要 root）…"
    if [ "$(id -u)" -ne 0 ]; then
      die "请先安装 Docker，或以 root 运行本脚本"
    fi
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -y
      apt-get install -y ca-certificates curl
      curl -fsSL https://get.docker.com | sh
      systemctl enable --now docker || true
    else
      die "请手动安装 Docker 后再运行"
    fi
  fi
  detect_compose
}

ensure_repo() {
  if [ -d "$REPO_DIR/.git" ]; then
    log "已有仓库：$REPO_DIR"
    return
  fi
  if [ -z "$REPO_URL" ]; then
    # Script running from inside a clone?
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [ -f "$here/docker-compose.yml" ] && [ -d "$here/web" ]; then
      REPO_DIR="$here"
      log "使用当前目录：$REPO_DIR"
      return
    fi
    die "请设置 OCIBOT_REPO_URL=https://github.com/<owner>/<repo>.git 或在仓库内运行"
  fi
  log "克隆 $REPO_URL → $REPO_DIR"
  need_cmd git
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR"
}

# Compose reads ${VAR} interpolation from a .env in the PROJECT directory, not
# from a service's env_file. Without this link the random POSTGRES_PASSWORD in
# web/.env was never seen by compose, so ${POSTGRES_PASSWORD:-ocibot_dev_pass}
# silently fell back to the built-in default for every install.
link_root_env() {
  local target="$REPO_DIR/web/.env"
  local link="$REPO_DIR/.env"
  [ -f "$target" ] || return 0
  if [ -L "$link" ]; then
    return 0
  fi
  if [ -e "$link" ]; then
    warn "$link 已存在且不是符号链接，跳过（compose 插值将使用它）"
    return 0
  fi
  ln -s "web/.env" "$link" 2>/dev/null || cp -a "$target" "$link"
  log "已关联 .env → web/.env（compose 变量插值）"
}

# Make the database role match web/.env. POSTGRES_PASSWORD only takes effect at
# initdb, so a volume created before the link above kept the default password;
# this ALTER is idempotent and converges both cases. Runs inside the container as
# the local superuser, so it does not need the current password.
sync_db_password() {
  local envf="$REPO_DIR/web/.env"
  [ -f "$envf" ] || return 0
  local pw
  pw="$(grep -E '^POSTGRES_PASSWORD=' "$envf" | head -n1 | cut -d= -f2-)"
  [ -n "$pw" ] || return 0
  local i
  for i in $(seq 1 20); do
    if compose exec -T db pg_isready -U ocibot -d ocibot >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  # Single-quoted SQL literal; escape any embedded quote.
  local esc
  esc="$(printf '%s' "$pw" | sed "s/'/''/g")"
  if compose exec -T db psql -U ocibot -d ocibot -v ON_ERROR_STOP=1 \
      -c "ALTER USER ocibot WITH PASSWORD '$esc';" >/dev/null 2>&1; then
    log "数据库密码已与 web/.env 对齐"
  else
    warn "无法同步数据库密码（数据库可能未就绪）；如 api 连不上库请手动执行 ALTER USER"
  fi
}

ensure_env() {
  local envf="$REPO_DIR/web/.env"
  if [ -f "$envf" ]; then
    log "保留已有 web/.env"
    link_root_env
    return
  fi
  log "生成 web/.env（随机密钥）"
  mkdir -p "$REPO_DIR/web"
  local pg_pass master jwt
  pg_pass="$(rand_hex | cut -c1-32)"
  master="$(rand_hex)"
  jwt="$(rand_hex)"
  cat >"$envf" <<EOF
POSTGRES_PASSWORD=${pg_pass}
OCIBOT_MASTER_KEY=${master}
OCIBOT_JWT_SECRET=${jwt}
OCIBOT_REQUIRE_SECURE_SECRETS=1
OCIBOT_CORS_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
OCIBOT_COOKIE_SECURE=0
OCIBOT_COOKIE_SAMESITE=lax
OCIBOT_ALLOW_OPEN_REGISTRATION=0
OCIBOT_JWT_EXPIRE_MINUTES=720
OCIBOT_API_WORKERS=2
OCIBOT_DB_POOL_SIZE=10
OCIBOT_DB_MAX_OVERFLOW=20
OCIBOT_PORT=8000
EOF
  chmod 600 "$envf" || true
  link_root_env
  warn "已写入随机密钥到 web/.env —— 请备份该文件；丢失 OCIBOT_MASTER_KEY 将无法解密租户私钥"
}

compose() {
  # Guard against corrupted REPO_DIR (e.g. embedded newlines from old bug).
  local dir
  dir="$(printf '%s' "$REPO_DIR" | tr -d '\r\n')"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    die "REPO_DIR 无效：$REPO_DIR"
  fi
  (cd "$dir" && $COMPOSE "$@")
}

export_build_env() {
  # Absolute host path is required for in-panel self-update (docker run -v).
  # Resolve REPO_DIR to a real absolute path so containers bind the same location.
  #
  # NOTE: do NOT write `cd && pwd -P || cd && pwd` — shell precedence makes the
  # final `pwd` always run, producing a path with an embedded newline
  # ("/root/ocibot\n/root/ocibot") which breaks `cd` later.
  local abs=""
  if [ -d "$REPO_DIR" ]; then
    abs="$(cd "$REPO_DIR" && pwd -P 2>/dev/null)" || true
    if [ -z "$abs" ]; then
      abs="$(cd "$REPO_DIR" && pwd)" || true
    fi
  fi
  # Strip CR/LF/spaces that can sneak in from command substitution.
  abs="$(printf '%s' "$abs" | tr -d '\r\n' | head -c 512)"
  if [ -z "$abs" ] || [ ! -d "$abs" ]; then
    die "无法解析安装目录绝对路径（REPO_DIR=$REPO_DIR）"
  fi
  REPO_DIR="$abs"
  export OCIBOT_HOST_REPO="$abs"
  export OCIBOT_UPDATE_ENABLED="${OCIBOT_UPDATE_ENABLED:-1}"
  export OCIBOT_UPDATE_BRANCH="${OCIBOT_UPDATE_BRANCH:-$BRANCH}"
  export OCIBOT_DOCKER_CLI_IMAGE="${OCIBOT_DOCKER_CLI_IMAGE:-docker:27-cli}"
  if [ -d "$REPO_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    export OCIBOT_GIT_SHA
    OCIBOT_GIT_SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    OCIBOT_GIT_SHA="$(printf '%s' "$OCIBOT_GIT_SHA" | tr -d '\r\n')"
  else
    export OCIBOT_GIT_SHA="${OCIBOT_GIT_SHA:-unknown}"
  fi
  log "OCIBOT_HOST_REPO=$OCIBOT_HOST_REPO"
  # Pre-pull helper image used by in-panel updates (best-effort).
  if command -v docker >/dev/null 2>&1; then
    docker pull "$OCIBOT_DOCKER_CLI_IMAGE" >/dev/null 2>&1 || \
      warn "预拉取 $OCIBOT_DOCKER_CLI_IMAGE 失败（在线更新时会再试）"
  fi
}

do_install() {
  ensure_docker
  ensure_repo
  ensure_env
  export_build_env
  log "构建并启动（PostgreSQL + API + Worker）…"
  compose up -d --build db
  sync_db_password
  compose up -d --build
  log "等待健康检查…"
  local i
  for i in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" >/dev/null 2>&1; then
      log "就绪：http://127.0.0.1:${OCIBOT_PORT:-8000}"
      log "首次打开页面 → 注册管理员账号"
      log "管理员「用户管理」页可检查/执行在线更新"
      return 0
    fi
    sleep 3
  done
  warn "健康检查超时，请运行：$0 status  或  docker compose -f $REPO_DIR/docker-compose.yml logs"
}

sync_repo_to_origin() {
  # Force local tree to match origin/<branch>, preserving only web/.env.
  # Shallow clones + local commits (or mixed histories) often break --ff-only.
  need_cmd git
  if [ ! -d "$REPO_DIR/.git" ]; then
    warn "不是 git 仓库，跳过代码同步：$REPO_DIR"
    return 0
  fi
  log "同步代码到 origin/$BRANCH …"
  # Keep secrets even if reset is hard.
  if [ -f "$REPO_DIR/web/.env" ]; then
    cp -a "$REPO_DIR/web/.env" "/tmp/ocibot.env.backup.$$" || true
  fi
  # Show divergence for operators (best-effort).
  git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null | awk '{print "本地 HEAD: "$0}' || true
  if ! git -C "$REPO_DIR" fetch --depth 50 origin "$BRANCH"; then
    warn "git fetch 失败，将尝试全量 fetch…"
    git -C "$REPO_DIR" fetch origin "$BRANCH" || die "无法从 origin 拉取 $BRANCH"
  fi
  if ! git -C "$REPO_DIR" rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
    die "找不到 origin/$BRANCH，请检查远程仓库"
  fi
  local remote_sha
  remote_sha="$(git -C "$REPO_DIR" rev-parse --short "origin/$BRANCH")"
  log "远程 origin/$BRANCH: $remote_sha"
  # Discard local commits/dirty tracked files. `reset --hard` already realigns
  # every tracked file, which is all this function promises — a `git clean -fd`
  # here additionally deleted UNTRACKED operator files such as
  # docker-compose.override.yml, TLS certs or helper scripts kept in the repo
  # directory. Opt in with OCIBOT_CLEAN_UNTRACKED=1 if you really want that.
  git -C "$REPO_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
  git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
  if [ "${OCIBOT_CLEAN_UNTRACKED:-0}" = "1" ]; then
    warn "OCIBOT_CLEAN_UNTRACKED=1：将删除以下未跟踪文件"
    git -C "$REPO_DIR" clean -nd --exclude=web/.env --exclude=web_data || true
    git -C "$REPO_DIR" clean -fd --exclude=web/.env --exclude=web_data || true
  fi
  if [ -f "/tmp/ocibot.env.backup.$$" ]; then
    mkdir -p "$REPO_DIR/web"
    mv -f "/tmp/ocibot.env.backup.$$" "$REPO_DIR/web/.env"
    log "已恢复 web/.env"
  fi
  log "代码已对齐：$(git -C "$REPO_DIR" rev-parse --short HEAD)"
}

do_update() {
  ensure_docker
  ensure_repo
  if [ "${OCIBOT_SKIP_GIT:-0}" = "1" ]; then
    log "跳过 git 同步（OCIBOT_SKIP_GIT=1，代码已由面板拉取）"
  elif [ -d "$REPO_DIR/.git" ]; then
    sync_repo_to_origin
  else
    warn "目录无 .git，仅重建当前文件树（不会拉 GitHub 新代码）"
  fi
  ensure_env
  export_build_env
  log "重新构建并滚动更新（无缓存前端层）…"
  # --pull refreshes base images; build runs with current tree after hard reset.
  compose build --pull api worker || compose build api worker
  # Bring the database up and align its password BEFORE the api starts, otherwise
  # an install whose volume predates the .env link would hand api the new password
  # while the role still has the old one.
  compose up -d db
  sync_db_password
  compose up -d
  log "更新完成。请验证："
  log "  curl -fsS http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health"
  log "  期望 version 为最新（当前源码 app_version 见 web/backend/config.py）"
  curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" && echo || warn "API 暂未就绪，稍候再 curl"
}

do_status() {
  ensure_docker
  ensure_repo
  compose ps
  echo
  curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" && echo || warn "API 未响应"
}

do_uninstall() {
  ensure_docker
  ensure_repo
  warn "将停止容器。默认保留 PostgreSQL 数据卷 ocibot_pg。"
  compose down
  if [ "${OCIBOT_PURGE_DATA:-0}" = "1" ]; then
    warn "OCIBOT_PURGE_DATA=1 → 删除数据卷"
    compose down -v
  fi
  log "已停止。仓库目录仍在：$REPO_DIR"
}

cmd="${1:-install}"
case "$cmd" in
  install|up) do_install ;;
  update|upgrade) do_update ;;
  status|ps) do_status ;;
  uninstall|down) do_uninstall ;;
  *)
    cat <<EOF
用法: $0 {install|update|status|uninstall}

环境变量:
  OCIBOT_REPO_URL   git clone 地址（远程一键安装时必填）
  OCIBOT_DIR        安装目录（默认 \$HOME/ocibot）
  OCIBOT_BRANCH     分支（默认 main）
  OCIBOT_PORT       映射端口（默认 8000，需与 web/.env 一致）
  OCIBOT_PURGE_DATA 卸载时删库（=1）
EOF
    exit 1
    ;;
esac
