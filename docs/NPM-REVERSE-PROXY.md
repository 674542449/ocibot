# 用 Nginx Proxy Manager 反代 OCIBot（公网域名 + HTTPS）

面向「装好就想挂个域名用，不想研究安全参数」的场景。照做即可，每一步都说明**为什么**，
不需要理解也能用，但出问题时知道去哪看。

> **只想让它跑起来、不想读这些**：直接执行一条命令，下面所有服务器端步骤都会自动做完，
> 最后会打印出 NPM 网页里要填的几个值。
>
> ```bash
> cd ~/ocibot && bash scripts/setup-proxy.sh 你的域名
> ```
>
> 本文档剩下的部分是同样内容的手动版本，以及出问题时的排查依据。

前提：已用 `scripts/install.sh` 装好，`http://服务器IP:8000` 能打开面板。

---

## 0. 先明确一个坑

README 的安全清单让你设 `OCIBOT_BIND=127.0.0.1`，让面板端口不对公网暴露。这是对的，
**但如果你的 Nginx Proxy Manager 也跑在 Docker 里**，NPM 容器里的 `127.0.0.1` 是它自己，
不是宿主机 —— 填 `127.0.0.1:8000` 会 502。

所以下面分两种情况，**先确认你的 NPM 是怎么装的**：

```bash
docker ps --format '{{.Names}}	{{.Image}}'
```

在输出里找**镜像**是 `jc21/nginx-proxy-manager` 的那一行，左边就是容器名。

> 按容器名去 grep 是不可靠的：NPM 官方 compose 的服务名叫 `app`，容器通常是
> `npm-app-1` 或 `nginx-proxy-manager-app-1`，取决于 compose 文件放在哪个目录。
> 认镜像才准。

- **找到了** → NPM 在 Docker 里 → 走 **方案 A**（推荐，也最安全）
- **没有这行** → 要么还没装 NPM（见文末「附：还没装 NPM」），
  要么装在宿主机 / 另一台机器 → 走 **方案 B**

---

## 方案 A：NPM 在同一台机器的 Docker 里（推荐）

让 NPM 通过 Docker 内网直接连到 api 容器，**面板端口完全不对外**。

### A1. 把 NPM 接入 OCIBot 的网络

```bash
docker network connect ocibot_default <你的NPM容器名>
```

容器名就是上面 `docker ps` 查到的，通常是 `nginx-proxy-manager` 或 `npm-app-1`。
这一步是一次性的，重启不会丢。

验证 NPM 能连通（应输出健康信息）：

```bash
docker exec <你的NPM容器名> curl -fsS http://ocibot-api-1:8000/api/health
```

### A2. 改 `web/.env`

编辑 `~/ocibot/web/.env`，把下面这几行改成对应的值
（没有的行就加上，`panel.example.com` 换成你的域名）：

```bash
OCIBOT_COOKIE_SECURE=1
OCIBOT_COOKIE_SAMESITE=lax
OCIBOT_CORS_ORIGINS=https://panel.example.com
OCIBOT_BIND=127.0.0.1
OCIBOT_TRUST_PROXY=1
OCIBOT_FORWARDED_ALLOW_IPS=172.16.0.0/12
```

逐条解释：

| 参数 | 作用 | 不设会怎样 |
|------|------|-----------|
| `COOKIE_SECURE=1` | 登录 Cookie 只经 HTTPS 发送，同时开始下发 HSTS | Cookie 可能经明文链路泄露 |
| `CORS_ORIGINS` | 只允许你的域名带凭据访问 API | 保持 localhost 会让本机来源一直在白名单里 |
| `BIND=127.0.0.1` | 8000 端口只监听回环，公网直连不到 | 别人可用 `http://IP:8000` 绕过 HTTPS 直接访问 |
| `TRUST_PROXY=1` | 登录限流按**真实客户端 IP** 计数，而不是把所有人算作 NPM 一个 IP | 一个人试错会连累所有人；攻击者也更容易绕过限流 |
| `FORWARDED_ALLOW_IPS` | 只信任 Docker 内网来的代理头 | 填 `*` 等于让任何人伪造自己的 IP，限流形同虚设 |

> `172.16.0.0/12` 覆盖 Docker 默认的网段。因为上面 `BIND=127.0.0.1` 已经让端口不对公网开放，
> 只有同网络的容器能到达 api，所以信任这个私有网段是安全的。

生效：

```bash
cd ~/ocibot && docker compose up -d
```

### A3. NPM 里新建 Proxy Host

**Details 标签页**

| 字段 | 填 |
|------|-----|
| Domain Names | `panel.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | `ocibot-api-1` |
| Forward Port | `8000` |
| Cache Assets | 关 |
| Block Common Exploits | 开 |
| **Websockets Support** | **必须开** |

> Websockets 不开，网页 SSH 终端连不上（其余功能正常），很容易误判成"终端坏了"。
> Forward Hostname 填**容器名**而不是 IP：容器重启后 IP 会变，名字不会。

**SSL 标签页**

| 字段 | 填 |
|------|-----|
| SSL Certificate | Request a new SSL Certificate（Let's Encrypt） |
| Force SSL | 开 |
| HTTP/2 Support | 开 |
| HSTS Enabled | 开 |
| Email / 同意条款 | 填你的邮箱、勾选 |

**Advanced 标签页**，粘贴这一行：

```nginx
client_max_body_size 50m;
```

> 备份导入上限 20MB、对象存储上传更大，NPM 默认的请求体上限可能把它们截断成一个
> 看不懂的报错。

保存，等证书签发完成。

---

## 方案 B：NPM 不在本机 Docker 里

NPM 装在宿主机上，或在另一台服务器。

### B1. 改 `web/.env`

与方案 A 相同，**但 `OCIBOT_BIND` 和 `FORWARDED_ALLOW_IPS` 不同**：

- **NPM 装在同一台宿主机**（非 Docker）：
  ```bash
  OCIBOT_BIND=127.0.0.1
  OCIBOT_FORWARDED_ALLOW_IPS=127.0.0.1,::1
  ```
  NPM 里 Forward Hostname 填 `127.0.0.1`，端口 `8000`。

- **NPM 在另一台服务器**：面板端口必须对那台机器可达，
  ```bash
  OCIBOT_BIND=0.0.0.0
  OCIBOT_FORWARDED_ALLOW_IPS=<NPM服务器的IP>
  ```
  并且**必须用防火墙/安全组把 8000 端口只放行给 NPM 那台机器**，否则等于把明文面板挂在公网上。
  注意：Docker 发布的端口会绕过 ufw，需在云厂商安全组层面限制，或使用 `ufw-docker`。

### B2. NPM 配置

与方案 A3 相同，只有 Forward Hostname 按上面填。

---

## 完成后自检（4 条，都要通过）

```bash
# 1. HTTPS 能打开，且 HTTP 自动跳 HTTPS
curl -sI http://panel.example.com | head -1        # 期望 301
curl -sI https://panel.example.com | head -1       # 期望 200
```

```bash
# 2. 公网直连 8000 应该失败（方案 A / B-同机）
curl -m 5 http://<服务器公网IP>:8000/api/health    # 期望超时或拒绝
```

```bash
# 3. 版本正确
curl -s https://panel.example.com/api/health
```

```bash
# 4. 安全响应头齐全（应看到 Strict-Transport-Security）
curl -sI https://panel.example.com | grep -i strict-transport
```

第 4 条没有 HSTS，说明 `OCIBOT_COOKIE_SECURE=1` 没生效（应用只在该值为 1 时才下发）。

**浏览器里再确认一次**：登录后打开任意实例的「网页终端」，能出命令行提示符 = Websockets 正常。

---

## 还有两件事值得做

1. **给 GitHub 账号开 2FA**。你用面板内一键更新（`OCIBOT_UPDATE_ENABLED=1`），更新流程是
   从你自己的仓库拉代码后在宿主机执行 —— 你的 GitHub 账号失陷，等于对方拿到这台服务器的 root。
   这是整套部署里最短的攻击路径，比面板本身任何配置都关键。
2. **备份 `web/.env`**。里面的 `OCIBOT_MASTER_KEY` 丢了，所有已存的 OCI 私钥都解不开，
   只能重新导入租户。放到密码管理器里存一份。

---

## 常见问题

| 现象 | 原因 |
|------|------|
| NPM 显示 502 | Forward Hostname 填了 `127.0.0.1` 但 NPM 在 Docker 里 → 按方案 A 改成容器名；或忘了 `docker network connect` |
| 网页终端连不上，其他正常 | NPM 的 **Websockets Support** 没开 |
| 登录后立刻被登出 | `OCIBOT_COOKIE_SECURE=1` 但实际用 HTTP 访问；确认 Force SSL 已开 |
| 备份导入报错/上传失败 | Advanced 里没加 `client_max_body_size 50m;` |
| 登录提示请求过于频繁 | `TRUST_PROXY=1` 但 `FORWARDED_ALLOW_IPS` 不含 NPM 的来源，所有人被算作同一个 IP |

---

## 附：还没装 NPM

在服务器上任意目录（例如 `~/npm`）建 `docker-compose.yml`：

```yaml
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
```

```bash
cd ~/npm && docker compose up -d
```

**81 是 NPM 自己的管理后台，不要对公网开放。** 上面写成 `127.0.0.1:81:81` 后它只监听
回环，从你自己电脑用 SSH 隧道连过去管理：

```bash
ssh -L 8181:127.0.0.1:81 root@<服务器IP>
```

然后本机浏览器打开 `http://127.0.0.1:8181`。初始账号 `admin@example.com`，
密码 `changeme`，首次登录会强制修改。

> 这个后台能签发证书、能改所有反代规则，且默认密码众所周知 —— 在一篇讲「不要暴露端口」
> 的文档里把它开在公网上是自相矛盾的。确实想直接开 81，至少先改掉默认账号密码，
> 并限制来源 IP。

装好后回到 **方案 A** 第 1 步。
