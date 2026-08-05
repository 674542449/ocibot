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
# 访问方式：ip = 直接 http://服务器IP:端口；domain = 域名 + 自动 HTTPS（内置 Caddy）。
# 空值表示「沿用 web/.env 里已有的设置」，update/status 不会因此改动配置。
ACCESS_MODE="${OCIBOT_ACCESS_MODE:-}"
PANEL_DOMAIN="${OCIBOT_DOMAIN:-}"

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
  local i
  for i in $(seq 1 20); do
    if compose exec -T db pg_isready -U ocibot -d ocibot >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  # Read the value compose ACTUALLY interpolated rather than re-parsing the file.
  # A hand-rolled `cut` does not match dotenv semantics (surrounding quotes,
  # trailing CR, inline comments), so it could set a password different from the
  # one api/worker connect with — which is exactly the failure this function
  # exists to prevent.
  local pw
  pw="$(compose exec -T db printenv POSTGRES_PASSWORD 2>/dev/null | tr -d '\r\n')"
  if [ -z "$pw" ]; then
    warn "无法读取数据库容器的 POSTGRES_PASSWORD，跳过密码同步"
    return 0
  fi
  # Single-quoted SQL literal; escape any embedded quote. Passed over stdin so the
  # password never appears in a process command line (visible to any local user
  # via ps) or in psql's history.
  local esc
  esc="$(printf '%s' "$pw" | sed "s/'/''/g")"
  if printf "ALTER USER ocibot WITH PASSWORD '%s';\n" "$esc" \
      | compose exec -T db psql -U ocibot -d ocibot -v ON_ERROR_STOP=1 -f - >/dev/null 2>&1; then
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
OCIBOT_BIND=0.0.0.0
OCIBOT_TRUST_PROXY=0
COMPOSE_PROFILES=
EOF
  chmod 600 "$envf" || true
  link_root_env
  warn "已写入随机密钥到 web/.env —— 请备份该文件；丢失 OCIBOT_MASTER_KEY 将无法解密租户私钥"
}

# ---------------------------------------------------------------- 访问方式
#
# 两种模式，任选其一，装完后随时可以互换：
#
#   ip     —— http://服务器IP:8000 直接打开。零依赖，但是明文。
#   domain —— https://你的域名 自动签证书（内置 Caddy），8000 端口只监听回环。
#
# 两者的差别不止「有没有证书」：Cookie 是否仅限 HTTPS、限流按谁的 IP 计数、
# 端口对不对公网开放，三项必须和访问方式一致，配错任何一项都不会报错，只会在
# 「登录后立刻被登出」或「一个人试错连累所有人」这类现象里暴露出来。所以这里
# 成组写入，不让操作者逐项去对。

# set_env_kv KEY VALUE —— 有则改（含被注释掉的同名行、重复行），无则追加。
#
# 用 awk 而不是 sed：值里含 '/'（如 https://…）和 '#'，任何单字符分隔符都可能和
# 模式或值撞上。scripts/setup-proxy.sh 第一版用 '#' 作分隔符，正好和模式里的 '#?'
# 冲突，sed 报错但脚本继续跑完并打印成功 —— 面板会停在 COOKIE_SECURE=0 的状态。
set_env_kv() {
  local key="$1" val="$2" envf="$REPO_DIR/web/.env" tmp
  [ -f "$envf" ] || die "找不到 $envf"
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
  ' "$envf" > "$tmp" || { rm -f "$tmp"; die "写入 web/.env 失败（$key）"; }
  # 回写进原文件而不是 mv，保住原有的属主与权限（里面有主密钥）。
  cat "$tmp" > "$envf" || { rm -f "$tmp"; die "写入 web/.env 失败（$key）"; }
  rm -f "$tmp"
  grep -qx "${key}=${val}" "$envf" || die "校验失败：$key 未写入 web/.env"
}

get_env_kv() {
  local key="$1" envf="$REPO_DIR/web/.env"
  [ -f "$envf" ] || return 0
  sed -n "s/^${key}=//p" "$envf" | tail -n1 | tr -d '\r'
}

# 从终端读一行。不能直接 `read`：一键安装是 `curl … | bash`，此时 stdin 是脚本
# 本身，read 会把脚本后面的内容当成用户输入吃掉。
ask() {
  local prompt="$1" def="${2:-}" ans=""
  if [ -r /dev/tty ]; then
    printf '%s' "$prompt" >/dev/tty
    read -r ans </dev/tty || ans=""
  fi
  printf '%s' "${ans:-$def}"
}

# 规范化域名。转小写不只是美观：OCIBOT_CORS_ORIGINS 是精确字符串比对，而浏览器
# 发出的 Origin 头里主机名永远是小写，输入 Panel.Example.com 会比对不上。
normalize_domain() {
  local d
  d="$(printf '%s' "${1:-}" | tr -d ' \r' | tr 'A-Z' 'a-z' | sed -e 's#^https\?://##' -e 's#/.*$##')"
  # 这一行不只是「输入校验」：这个值会被原样写进 web/.env 的 OCIBOT_DOMAIN= 后面。
  # 字符白名单里没有换行，所以带换行的参数会在这里被拒 —— 否则一个精心构造的
  # 「域名」可以往 .env 里再插一行，覆盖掉 OCIBOT_MASTER_KEY 之类的键。
  case "$d" in
    ""|*[!a-z0-9.-]*|.*|*.|-*|*-) die "域名看起来不对：'${1:-}'" ;;
    *-.*|*.-*)                    die "域名看起来不对：'${1:-}'（标签不能以 - 开头或结尾）" ;;
    *..*)                         die "域名看起来不对：'${1:-}'（有连续的点）" ;;
  esac
  # 纯 IP 走不了 Let's Encrypt（公共 CA 不给裸 IP 签证书），Caddy 会退回自签，
  # 浏览器每次都会拦一道警告。这种情况直接用 IP 模式，不要伪装成域名模式。
  case "$d" in
    *[!0-9.]*) : ;;
    *)  die "'$d' 是 IP 不是域名。IP 访问请执行：$0 ip" ;;
  esac
  case "$d" in
    *.*) : ;;
    *)   die "域名看起来不对：'${1:-}'（需要形如 panel.example.com）" ;;
  esac
  printf '%s' "$d"
}

# 80/443 被别的东西占着时，compose 只会抛一句 "port is already allocated"，
# 不会说是谁占的。这里先查出来并给出可执行的下一步。
check_web_ports_free() {
  local holder=""
  holder="$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null \
            | grep -E ':(80|443)->' | awk '{print $1}' | grep -v '^ocibot-caddy' || true)"
  if [ -n "$holder" ]; then
    warn "80/443 端口已被这些容器占用："
    printf '%s\n' "$holder" | sed 's/^/    /'
    die "先停掉它们（docker stop <名字>），或继续用原有反代而不启用本模式"
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -Hltn 2>/dev/null | awk '{print $4}' | grep -qE '[:.](80|443)$'; then
      warn "宿主机上已有进程监听 80/443（可能是 nginx/apache），Caddy 可能起不来"
      warn "查看：ss -ltnp | grep -E ':(80|443) '"
    fi
  fi
}

open_web_ports() {
  # 尽力而为：只在明确启用的防火墙上加放行规则，不去改 iptables 原始规则。
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
    ufw allow 80/tcp >/dev/null 2>&1 && ufw allow 443/tcp >/dev/null 2>&1 \
      && log "已在 ufw 放行 80/443" || warn "ufw 放行失败，请手动执行：ufw allow 80,443/tcp"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=http >/dev/null 2>&1
    firewall-cmd --permanent --add-service=https >/dev/null 2>&1
    firewall-cmd --reload >/dev/null 2>&1 && log "已在 firewalld 放行 80/443" || true
  fi
  warn "云厂商的安全组/入站规则是另一层，脚本改不到 —— 请确认 80 和 443 已放行"
  warn "（80 端口是 Let's Encrypt 校验域名归属用的，只开 443 签不出证书）"
}

# 域名是否套了 CDN。按响应头认，不按 IP 段认：各家的 IP 段会变，Server 头不会，
# 而且这个判断只用来决定「要不要提醒」，不做任何拦截。
detect_cdn() {
  curl -sI -m 8 "http://$1/" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Ss]erver: //p' | tail -n1
}

# CDN 回源用明文时会和 Caddy 的 HTTP→HTTPS 跳转打成死循环，浏览器报
# ERR_TOO_MANY_REDIRECTS。这是套 CDN 的部署里最容易撞上的一件事，而症状完全
# 不指向「回源协议」，所以在切换前后都明确说一次。
warn_cdn_tls_mode() {
  local server="$1"
  case "$(printf '%s' "$server" | tr 'A-Z' 'a-z')" in
    *cloudflare*)
      warn "检测到 Cloudflare 代理（橙云）。必须把 SSL/TLS 加密模式设为「完全（严格）」："
      warn "    后台 → SSL/TLS → 概述 → 加密模式 → 完全（严格）/ Full (strict)"
      warn "  留在「灵活 / Flexible」会明文回源，和本机的 HTTP→HTTPS 跳转形成死循环，"
      warn "  浏览器报 ERR_TOO_MANY_REDIRECTS。"
      warn "  另外先关掉 SSL/TLS → Edge Certificates → Always Use HTTPS：它在边缘就拦下"
      warn "  HTTP，而证书签发的域名校验正是走 HTTP 到达本机的。签发成功后可再开回来。"
      ;;
    ?*)
      warn "域名前面有 CDN/代理（Server: $server）。请确认它是以 HTTPS 回源的 ——"
      warn "  明文回源会和本机的 HTTP→HTTPS 跳转形成死循环。"
      ;;
  esac
}

# 域名解析是否指向本机。不匹配只警告不中断：套了 CDN 时解析到的本来就是 CDN 的
# IP，这时签发仍然可行。
check_dns() {
  local domain="$1" pubip resolved server
  pubip="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
  resolved="$(getent hosts "$domain" 2>/dev/null | awk '{print $1}' | head -n1 || true)"
  if [ -z "$resolved" ]; then
    warn "解析不到 $domain —— 如果 DNS 还没配好，证书会签发失败"
    return 0
  fi
  server="$(detect_cdn "$domain")"
  if [ -n "$pubip" ] && [ "$resolved" != "$pubip" ]; then
    warn "$domain 解析到 $resolved，本机公网 IP 是 $pubip"
    [ -n "$server" ] || warn "  如果没套 CDN，说明 DNS 指错了机器，证书签不出来"
  else
    log "DNS 检查通过：$domain → $resolved"
  fi
  [ -z "$server" ] || warn_cdn_tls_mode "$server"
}

# 面板是否真的能从公网打开。
#
# 不能用 `curl -fsS`：-f 只在 400 及以上失败，308 会被当成成功返回空 body ——
# 于是明文回源导致的重定向死循环会被报成「HTTPS 已就绪 ✓」，恰好是操作者最需要
# 被告知的那一种坏法。这里认状态码，并在 30x 指回同一个 https 地址时点破成因。
verify_public_https() {
  local domain="$1" url="https://$1/api/health" code loc
  code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
  case "$code" in
    200)
      log "HTTPS 已就绪 ✓（https://$domain）"
      return 0
      ;;
    30*)
      loc="$(curl -sI -m 15 "$url" 2>/dev/null | tr -d '\r' | sed -n 's/^[Ll]ocation: //p' | tail -n1)"
      case "$loc" in
        https://"$domain"/*)
          warn "HTTPS 返回 $code 且跳回自己（Location: $loc）—— 这是重定向死循环"
          warn_cdn_tls_mode "$(detect_cdn "$domain")"
          # 308/301 是「永久」重定向，浏览器会把它缓存下来。服务端改好之后，
          # 之前打开过页面的浏览器仍会凭自己的缓存继续跳，看起来像没修好。
          warn "修好之后浏览器可能仍在循环：308 是永久重定向，会被浏览器缓存。"
          warn "  用无痕窗口验证，或清掉该站点的缓存与 Cookie 再试。"
          ;;
        *) warn "HTTPS 返回 $code，跳到了 $loc" ;;
      esac
      return 1
      ;;
    000)
      warn "连不上 https://$domain。排查顺序：DNS → 80/443 是否放行 → $COMPOSE logs caddy"
      return 1
      ;;
    *)
      warn "https://$domain 返回 $code（期望 200）。查看：$COMPOSE logs caddy"
      return 1
      ;;
  esac
}

apply_mode_domain() {
  local domain="$1"
  log "访问方式 → 域名 + HTTPS（https://$domain）"
  set_env_kv OCIBOT_DOMAIN "$domain"
  set_env_kv OCIBOT_CORS_ORIGINS "https://$domain"
  # Cookie 仅经 HTTPS 下发；应用也以这一项作为「前面有 TLS」的信号来下发 HSTS。
  set_env_kv OCIBOT_COOKIE_SECURE 1
  set_env_kv OCIBOT_COOKIE_SAMESITE lax
  # 限流按真实客户端 IP 计数，否则所有人被算作 Caddy 一个 IP：一个人试错就会
  # 把其他人一起挡在门外，攻击者反过来也更容易耗光公共额度。
  set_env_kv OCIBOT_TRUST_PROXY 1
  # 只信任 Docker 内网来的代理头。配合下面 BIND=127.0.0.1（8000 不对公网开放），
  # 能到达 api 的只有同网络的容器，所以信任这个私有网段是安全的；填 '*' 则等于
  # 让任何人伪造自己的 IP，限流形同虚设。
  set_env_kv OCIBOT_FORWARDED_ALLOW_IPS 172.16.0.0/12
  # 明文端口收回回环，公网只能经 Caddy 的 HTTPS 进来。
  set_env_kv OCIBOT_BIND 127.0.0.1
  set_env_kv COMPOSE_PROFILES tls
}

apply_mode_ip() {
  local port="${OCIBOT_PORT:-$(get_env_kv OCIBOT_PORT)}"
  port="${port:-8000}"
  log "访问方式 → IP 直连（http://服务器IP:$port）"
  set_env_kv OCIBOT_CORS_ORIGINS "http://127.0.0.1:${port},http://localhost:${port}"
  # 明文访问下 COOKIE_SECURE=1 会让浏览器根本不回传登录 Cookie，表现为
  # 「登录成功后立刻又跳回登录页」，是这两种模式之间最容易踩的一脚。
  set_env_kv OCIBOT_COOKIE_SECURE 0
  set_env_kv OCIBOT_COOKIE_SAMESITE lax
  # 没有反代时开 TRUST_PROXY 等于把限流身份交给客户端自己声明。
  set_env_kv OCIBOT_TRUST_PROXY 0
  set_env_kv OCIBOT_FORWARDED_ALLOW_IPS "127.0.0.1,::1"
  set_env_kv OCIBOT_BIND 0.0.0.0
  set_env_kv COMPOSE_PROFILES ""
  set_env_kv OCIBOT_DOMAIN ""
  # 切回来时 Caddy 还占着 80/443，必须显式删掉：`compose up` 不会去动一个
  # 当前 profile 之外的运行中容器。
  compose --profile tls rm -sf caddy >/dev/null 2>&1 || true
}

# 装的时候问一次；非交互（curl | bash 且没有 tty）时保持原有的 IP 直连行为。
choose_access_mode() {
  [ -z "$ACCESS_MODE" ] || return 0
  if [ -n "$PANEL_DOMAIN" ]; then
    ACCESS_MODE="domain"
    return 0
  fi
  if [ ! -r /dev/tty ]; then
    log "非交互安装，默认 IP 直连；之后可执行：bash scripts/install.sh domain <域名>"
    ACCESS_MODE="ip"
    return 0
  fi
  cat >/dev/tty <<'EOF'

怎么访问这个面板？
  1) IP + 端口     http://服务器IP:8000        —— 不用域名，明文
  2) 域名 + HTTPS  https://panel.example.com  —— 自动签证书，无需另装反代
EOF
  local pick
  pick="$(ask '选择 [1/2]（默认 1）： ' 1)"
  case "$pick" in
    2) ACCESS_MODE="domain"; PANEL_DOMAIN="$(ask '你的域名（如 panel.example.com）： ')" ;;
    *) ACCESS_MODE="ip" ;;
  esac
}

apply_access_mode() {
  case "$ACCESS_MODE" in
    domain)
      PANEL_DOMAIN="$(normalize_domain "$PANEL_DOMAIN")"
      check_web_ports_free
      check_dns "$PANEL_DOMAIN"
      open_web_ports
      apply_mode_domain "$PANEL_DOMAIN"
      ;;
    ip)
      apply_mode_ip
      ;;
    "")
      : ;;  # update/status：沿用现有配置
    *)
      die "OCIBOT_ACCESS_MODE 只能是 ip 或 domain（当前：$ACCESS_MODE）"
      ;;
  esac
}

# FORWARDED_ALLOW_IPS 默认写 172.16.0.0/12，覆盖 Docker 默认的地址池
# （172.17.0.0/16 … 172.31.0.0/16）。但宿主机配过 default-address-pools、
# 或那段被占满时，compose 会从 192.168.x 之类的网段分配 —— 那样 Caddy 的来源 IP
# 不在信任列表里，代理头被忽略，所有人重新被算作同一个 IP。
#
# 这个失效是静默的：面板照常能用，只有在「一个人试错把所有人挡在门外」时才暴露。
# 所以起来之后按实际网段核对一次，不对就改掉并重建 api。
reconcile_proxy_subnet() {
  [ "$(get_env_kv COMPOSE_PROFILES)" = "tls" ] || return 0
  local subnet current
  subnet="$(docker network inspect ocibot_default \
              -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
            | awk '{print $1}' | tr -d '\r')"
  [ -n "$subnet" ] || return 0
  case "$subnet" in
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;  # 已在 172.16.0.0/12 内
  esac
  current="$(get_env_kv OCIBOT_FORWARDED_ALLOW_IPS)"
  [ "$current" = "$subnet" ] && return 0
  warn "Docker 网络是 $subnet，不在默认信任的 172.16.0.0/12 内 —— 已按实际网段修正"
  set_env_kv OCIBOT_FORWARDED_ALLOW_IPS "$subnet"
  compose up -d --force-recreate api >/dev/null 2>&1 || true
}

# 装完/切换完打印真正能打开的地址，而不是让操作者去猜。
print_access_summary() {
  local mode domain port
  mode="$(get_env_kv COMPOSE_PROFILES)"
  domain="$(get_env_kv OCIBOT_DOMAIN)"
  port="$(get_env_kv OCIBOT_PORT)"; port="${port:-8000}"
  echo
  if [ "$mode" = "tls" ] && [ -n "$domain" ]; then
    log "面板地址：https://$domain"
    log "首次访问会等十几秒签发证书；卡住就看：cd $REPO_DIR && $COMPOSE logs caddy"
    log "切回 IP 直连：bash scripts/install.sh ip"
  else
    local pubip
    pubip="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
    log "面板地址：http://${pubip:-<服务器IP>}:$port"
    warn "当前是明文 HTTP，登录密码在链路上不加密。有域名的话建议改用："
    warn "    bash scripts/install.sh domain <你的域名>"
  fi
}

compose() {
  # Guard against corrupted REPO_DIR (e.g. embedded newlines from old bug).
  local dir
  dir="$(printf '%s' "$REPO_DIR" | tr -d '\r\n')"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    die "REPO_DIR 无效：$REPO_DIR"
  fi
  # web/.env 里的 COMPOSE_PROFILES=tls 本身就该被 compose 读到（它通过符号链接
  # 也是项目根的 .env），但那依赖具体 compose 版本对 .env 里 COMPOSE_* 变量的处理。
  # 这里显式再传一次 --profile：多传是幂等的，而漏传的后果是 HTTPS 前端被静默跳过，
  # 表现成「装完了但域名打不开」，且哪条日志都不会提到 profile。
  local profile_args=()
  if [ -f "$dir/web/.env" ] && grep -qx 'COMPOSE_PROFILES=tls' "$dir/web/.env"; then
    profile_args=(--profile tls)
  fi
  # ${a[@]+"${a[@]}"}：set -u 下展开空数组在 bash 4.3 及更早会报 unbound variable。
  (cd "$dir" && $COMPOSE ${profile_args[@]+"${profile_args[@]}"} "$@")
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
  choose_access_mode
  apply_access_mode
  export_build_env
  log "构建并启动（PostgreSQL + API + Worker）…"
  compose up -d --build db
  sync_db_password
  compose up -d --build
  log "等待健康检查…"
  local i
  for i in $(seq 1 40); do
    # 走回环而不是公网地址：域名模式下证书可能还没签好，但那不影响面板本身是否就绪。
    if curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" >/dev/null 2>&1; then
      reconcile_proxy_subnet
      log "首次打开页面 → 注册管理员账号"
      log "管理员「用户管理」页可检查/执行在线更新"
      print_access_summary
      return 0
    fi
    sleep 3
  done
  warn "健康检查超时，请运行：$0 status  或  docker compose -f $REPO_DIR/docker-compose.yml logs"
}

# 装完之后换访问方式，不重装、不动数据库。
do_switch_mode() {
  ensure_docker
  ensure_repo
  [ -f "$REPO_DIR/web/.env" ] || die "还没安装过，请先执行：$0 install"
  ensure_env
  apply_access_mode
  export_build_env
  log "应用新配置…"
  # --force-recreate：改的全是进程启动时读一次的环境变量，不重建容器不会生效。
  compose up -d --force-recreate
  log "等待面板就绪…"
  local i
  for i in $(seq 1 20); do
    curl -fsS "http://127.0.0.1:${OCIBOT_PORT:-8000}/api/health" >/dev/null 2>&1 && break
    sleep 3
  done
  reconcile_proxy_subnet
  if [ "$ACCESS_MODE" = "domain" ]; then
    log "等待证书签发…"
    local ready=0
    for i in $(seq 1 20); do
      # 静默探一次，只在最后一次把结论和成因打出来 —— 中途的失败是正常的等待过程。
      if [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' \
                "https://${PANEL_DOMAIN}/api/health" 2>/dev/null || echo 000)" = "200" ]; then
        ready=1
        break
      fi
      sleep 5
    done
    [ "$ready" = "1" ] && log "HTTPS 已就绪 ✓（https://${PANEL_DOMAIN}）" \
                       || verify_public_https "$PANEL_DOMAIN" || true
  fi
  print_access_summary
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
  print_access_summary
}

do_uninstall() {
  ensure_docker
  ensure_repo
  warn "将停止容器。默认保留 PostgreSQL 数据卷 ocibot_pg。"
  # 显式带上 tls profile：如果 web/.env 里没有 COMPOSE_PROFILES=tls（例如手工改过），
  # 不带这个参数会把 caddy 留在后台继续占着 80/443。
  compose --profile tls down
  if [ "${OCIBOT_PURGE_DATA:-0}" = "1" ]; then
    warn "OCIBOT_PURGE_DATA=1 → 删除数据卷"
    compose --profile tls down -v
  fi
  log "已停止。仓库目录仍在：$REPO_DIR"
}

cmd="${1:-install}"
case "$cmd" in
  install|up) do_install ;;
  update|upgrade) do_update ;;
  status|ps) do_status ;;
  uninstall|down) do_uninstall ;;
  domain|https)
    ACCESS_MODE="domain"
    PANEL_DOMAIN="${2:-$PANEL_DOMAIN}"
    [ -n "$PANEL_DOMAIN" ] || PANEL_DOMAIN="$(ask '你的域名（如 panel.example.com）： ')"
    do_switch_mode
    ;;
  ip|http)
    ACCESS_MODE="ip"
    do_switch_mode
    ;;
  *)
    cat <<EOF
用法: $0 {install|update|status|uninstall|domain <域名>|ip}

访问方式（装完后可随时互换，不影响数据）:
  $0 domain panel.example.com   域名 + 自动 HTTPS（内置 Caddy 签证书，不需要另装反代）
  $0 ip                         直接 http://服务器IP:8000（明文，无需域名）

环境变量:
  OCIBOT_REPO_URL     git clone 地址（远程一键安装时必填）
  OCIBOT_DIR          安装目录（默认 \$HOME/ocibot）
  OCIBOT_BRANCH       分支（默认 main）
  OCIBOT_PORT         映射端口（默认 8000，需与 web/.env 一致）
  OCIBOT_ACCESS_MODE  ip | domain（非交互安装时指定，省去询问）
  OCIBOT_DOMAIN       域名（配合 OCIBOT_ACCESS_MODE=domain）
  OCIBOT_PURGE_DATA   卸载时删库（=1）
EOF
    exit 1
    ;;
esac
