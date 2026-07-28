#!/usr/bin/env bash
# 一键把 OCIBot 配置成「域名 + HTTPS 反代」模式。
#
#   bash scripts/setup-proxy.sh panel.example.com
#
# 做四件事：找到 / 安装 Nginx Proxy Manager、把它接进 OCIBot 的 Docker 网络、
# 按反代场景改好 web/.env、重启并自检。最后打印 NPM 网页里要填的几个值。
#
# 安全要点（脚本已代做，出问题时对照排查）：
#   - 面板 8000 端口改为只监听回环，公网只能经 NPM 的 HTTPS 进来
#   - 登录 Cookie 仅走 HTTPS；同时应用才会下发 HSTS
#   - 登录限流按真实客户端 IP 计数，且只信任 Docker 内网来的代理头
set -euo pipefail

log()  { printf '\033[32m[配置]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  printf '请输入你的域名（例如 panel.example.com）： '
  read -r DOMAIN
fi
# 转小写很关键，不只是美观：OCIBOT_CORS_ORIGINS 是精确字符串比对，而浏览器发出的
# Origin 头里主机名永远是小写。用户输入 Panel.Example.com 会导致比对不上。
DOMAIN="$(printf '%s' "$DOMAIN" | tr -d ' \r' | tr 'A-Z' 'a-z' | sed -e 's#^https\?://##' -e 's#/.*$##')"
case "$DOMAIN" in
  ""|*[!A-Za-z0-9.-]*|.*|*.) die "域名看起来不对：'$DOMAIN'" ;;
  *.*) : ;;
  *)   die "域名看起来不对：'$DOMAIN'（需要形如 panel.example.com）" ;;
esac

command -v docker >/dev/null 2>&1 || die "找不到 docker"
docker info >/dev/null 2>&1 || die "无法连接 Docker（可能需要 sudo）"

# ---------------------------------------------------------------- 定位面板
REPO_DIR="${OCIBOT_DIR:-}"
if [ -z "$REPO_DIR" ]; then
  here="$(cd "$(dirname "$0")/.." && pwd)"
  if [ -f "$here/docker-compose.yml" ]; then REPO_DIR="$here"; else REPO_DIR="$HOME/ocibot"; fi
fi
[ -f "$REPO_DIR/docker-compose.yml" ] || die "在 $REPO_DIR 找不到 docker-compose.yml，请在面板目录里运行本脚本"
ENV_FILE="$REPO_DIR/web/.env"
[ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE，请先执行 scripts/install.sh"
log "面板目录：$REPO_DIR"

# 容器名按 compose 标签查，不靠猜（服务名固定为 api，项目名固定为 ocibot）。
API_CT="$(docker ps -q --filter 'label=com.docker.compose.project=ocibot' \
                      --filter 'label=com.docker.compose.service=api' \
          | head -n1 | xargs -r docker inspect -f '{{.Name}}' | sed 's#^/##')"
[ -n "$API_CT" ] || die "面板 api 容器没在运行。先执行：cd $REPO_DIR && docker compose up -d"
log "面板容器：$API_CT"

NET="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$API_CT" | awk '{print $1}')"
[ -n "$NET" ] || die "无法确定面板所在的 Docker 网络"
log "面板网络：$NET"

# ---------------------------------------------------- 定位 / 安装 NPM（按镜像认）
find_npm() {
  docker ps -q | while read -r id; do
    img="$(docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null || true)"
    case "$img" in
      *jc21/nginx-proxy-manager*) docker inspect -f '{{.Name}}' "$id" | sed 's#^/##'; return ;;
    esac
  done
}
NPM_CT="$(find_npm || true)"

if [ -z "$NPM_CT" ]; then
  warn "没有找到正在运行的 Nginx Proxy Manager。"
  printf '要现在安装一个吗？[Y/n] '
  read -r ans
  case "${ans:-Y}" in
    [Nn]*) die "请先自行安装 NPM，然后重新运行本脚本" ;;
  esac
  NPM_DIR="${NPM_DIR:-$HOME/npm}"
  mkdir -p "$NPM_DIR"
  # 81 是 NPM 自己的管理后台：能签证书、能改所有反代规则，且默认密码公开，
  # 所以只监听回环，通过 SSH 隧道访问。
  cat > "$NPM_DIR/docker-compose.yml" <<'YAML'
services:
  app:
    image: jc21/nginx-proxy-manager:latest
    restart: unless-stopped
    ports:
      - '80:80'
      - '443:443'
      - '127.0.0.1:81:81'
    volumes:
      - ./data:/data
      - ./letsencrypt:/etc/letsencrypt
YAML
  log "已写入 $NPM_DIR/docker-compose.yml，正在启动…"
  (cd "$NPM_DIR" && docker compose up -d)
  sleep 5
  NPM_CT="$(find_npm || true)"
  [ -n "$NPM_CT" ] || die "NPM 启动失败，请查看：cd $NPM_DIR && docker compose logs"
  NPM_FRESH=1
fi
log "反代容器：$NPM_CT"

# ------------------------------------------------------------ 接入同一网络
if docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$NPM_CT" | grep -qw "$NET"; then
  log "反代已在面板网络中"
else
  docker network connect "$NET" "$NPM_CT"
  log "已把 $NPM_CT 接入 $NET"
fi

# ---------------------------------------------------------------- 改 .env
BACKUP="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
cp -p "$ENV_FILE" "$BACKUP"
log "已备份原配置：$BACKUP"

# set_env KEY VALUE —— 有则改（含被注释掉的同名行、重复行），无则追加。
#
# 用 awk 而不是 sed：值里含 '/'（如 https://…）和 '#'，任何单字符分隔符都可能
# 和模式或值撞上。第一版用 '#' 作分隔符，正好和模式里的 '#?' 冲突，sed 报错但
# 脚本继续跑完并打印成功 —— 面板会留在 COOKIE_SECURE=0、BIND=0.0.0.0 的状态。
set_env() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$val" '
    BEGIN { done = 0 }
    {
      probe = $0
      sub(/^[[:space:]]*#?[[:space:]]*/, "", probe)
      if (index(probe, k "=") == 1) {
        if (!done) { print k "=" v; done = 1 }
        next
      }
      print
    }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; die "写入 $ENV_FILE 失败（$key）"; }
  # 回写进原文件而不是 mv，保住原有的属主与权限（里面有主密钥）。
  cat "$tmp" > "$ENV_FILE" || { rm -f "$tmp"; die "写入 $ENV_FILE 失败（$key）"; }
  rm -f "$tmp"
  grep -qx "${key}=${val}" "$ENV_FILE" || die "校验失败：$key 未写入 $ENV_FILE"
}

set_env OCIBOT_COOKIE_SECURE 1
set_env OCIBOT_COOKIE_SAMESITE lax
set_env OCIBOT_CORS_ORIGINS "https://${DOMAIN}"
set_env OCIBOT_BIND 127.0.0.1
set_env OCIBOT_TRUST_PROXY 1
set_env OCIBOT_FORWARDED_ALLOW_IPS 172.16.0.0/12
log "已更新 web/.env"

# ------------------------------------------------------------------ 重启
log "正在重启面板…"
(cd "$REPO_DIR" && docker compose up -d --force-recreate api worker >/dev/null)
sleep 6

# ------------------------------------------------------------------ 自检
ok=1
if docker exec "$NPM_CT" sh -c "wget -qO- http://${API_CT}:8000/api/health >/dev/null 2>&1 || curl -fsS http://${API_CT}:8000/api/health >/dev/null 2>&1"; then
  log "自检 1/2：反代能连到面板 ✓"
else
  warn "自检 1/2：反代连不到面板 ✗（下一步在 NPM 里填的地址会 502）"
  ok=0
fi

PUBIP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
if [ -n "$PUBIP" ]; then
  if curl -fsS -m 5 "http://${PUBIP}:8000/api/health" >/dev/null 2>&1; then
    warn "自检 2/2：公网仍能直连 8000 端口 ✗（明文面板还暴露着）"
    ok=0
  else
    log "自检 2/2：公网已无法直连 8000 端口 ✓"
  fi
else
  warn "自检 2/2：无法获取公网 IP，跳过"
fi

# ------------------------------------------------------------------ 结果
cat <<EOF

================================================================
  服务器这边已经配好了。剩下最后一步，在 NPM 网页里点几下。
================================================================

EOF

if [ "${NPM_FRESH:-0}" = "1" ]; then
  cat <<EOF
NPM 是刚装的，管理后台只能从本机访问。在**你自己的电脑**上开一个终端执行：

    ssh -L 8181:127.0.0.1:81 $(whoami)@${PUBIP:-<服务器IP>}

保持这个窗口不关，然后浏览器打开   http://127.0.0.1:8181
初始账号 admin@example.com   密码 changeme  （首次登录会强制改）

EOF
fi

cat <<EOF
在 NPM 里点 Hosts → Proxy Hosts → Add Proxy Host，按下面填：

  【Details 标签页】
    Domain Names ............ ${DOMAIN}
    Scheme .................. http
    Forward Hostname / IP ... ${API_CT}
    Forward Port ............ 8000
    Block Common Exploits ... 打开
    Websockets Support ...... 打开   ← 不开的话网页终端连不上

  【SSL 标签页】
    SSL Certificate ......... Request a new SSL Certificate
    Force SSL ............... 打开
    HTTP/2 Support .......... 打开
    HSTS Enabled ............ 打开
    Email ................... 填你的邮箱，并勾选同意条款

  【Advanced 标签页】粘贴这一行：
    client_max_body_size 50m;

点 Save，等证书签发完成（约十几秒）。

前提：${DOMAIN} 的 DNS 必须已经解析到这台服务器${PUBIP:+（$PUBIP）}，
否则证书签发会失败。

完成后打开  https://${DOMAIN}  即可。
出问题先看：$REPO_DIR/docs/NPM-REVERSE-PROXY.md 最后的「常见问题」

EOF

[ "$ok" = "1" ] || warn "上面有自检未通过，先解决再继续。"
log "如需还原本次改动：cp $BACKUP $ENV_FILE && cd $REPO_DIR && docker compose up -d"
