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

ensure_env() {
  local envf="$REPO_DIR/web/.env"
  if [ -f "$envf" ]; then
    log "保留已有 web/.env"
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
  warn "已写入随机密钥到 web/.env —— 请备份该文件；丢失 OCIBOT_MASTER_KEY 将无法解密租户私钥"
}

compose() {
  (cd "$REPO_DIR" && $COMPOSE "$@")
}

do_install() {
  ensure_docker
  ensure_repo
  ensure_env
  log "构建并启动（PostgreSQL + API + Worker）…"
  compose up -d --build
  log "等待健康检查…"
  local i
  for i in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" >/dev/null 2>&1; then
      log "就绪：http://127.0.0.1:${OCIBOT_PORT:-8000}"
      log "首次打开页面 → 注册管理员账号"
      return 0
    fi
    sleep 3
  done
  warn "健康检查超时，请运行：$0 status  或  docker compose -f $REPO_DIR/docker-compose.yml logs"
}

do_update() {
  ensure_docker
  ensure_repo
  if [ -d "$REPO_DIR/.git" ]; then
    log "拉取最新代码（$BRANCH）…"
    need_cmd git
    git -C "$REPO_DIR" fetch --depth 1 origin "$BRANCH" || true
    git -C "$REPO_DIR" checkout "$BRANCH" || true
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" || \
      warn "git pull 失败（可能是本地改动）— 继续用当前树 rebuild"
  fi
  ensure_env
  log "重新构建并滚动更新…"
  compose up -d --build
  log "更新完成。健康检查：curl -fsS http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health"
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
  OCIBOT_BRANCH     分支（默认 master）
  OCIBOT_PORT       映射端口（默认 8000，需与 web/.env 一致）
  OCIBOT_PURGE_DATA 卸载时删库（=1）
EOF
    exit 1
    ;;
esac
