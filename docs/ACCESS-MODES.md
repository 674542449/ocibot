# 访问方式：IP 直连 / 域名 + HTTPS

面板自带 HTTPS 终结，**不需要另外装 Nginx Proxy Manager 或任何反代**。
两种访问方式二选一，装完之后随时可以互换，不影响数据库和已导入的租户。

```bash
# 域名 + 自动 HTTPS
bash scripts/install.sh domain panel.example.com

# 改回 IP + 端口直连
bash scripts/install.sh ip
```

首次安装时脚本会直接问你选哪种；`curl … | bash` 这种没有终端的场景默认走 IP 直连，
之后再执行上面的命令切换即可。

---

## 两种模式的差别

| | IP 直连 | 域名 + HTTPS |
|---|---|---|
| 打开地址 | `http://服务器IP:8000` | `https://panel.example.com` |
| 需要域名 | 否 | 是，且 DNS 已指向本机 |
| 传输加密 | **无**，登录密码在链路上是明文 | 有，证书自动签发与续期 |
| 占用端口 | 8000 | 80、443（8000 收回回环） |
| 额外组件 | 无 | 一个 `caddy:2-alpine` 容器 |

没有域名就用 IP 模式，它能用；但要清楚**登录密码是明文传输的**，
只适合临时用或者只在内网访问。

---

## 域名模式做了什么

一条命令改了六个环境变量并起了一个容器。这六项必须成组生效，
所以脚本成组写入，而不是让你逐项去对：

| 键 | 值 | 不这样会怎样 |
|---|---|---|
| `COMPOSE_PROFILES` | `tls` | 不启动 Caddy，等于没开 HTTPS |
| `OCIBOT_DOMAIN` | 你的域名 | Caddy 不知道给谁签证书 |
| `OCIBOT_CORS_ORIGINS` | `https://你的域名` | — |
| `OCIBOT_COOKIE_SECURE` | `1` | Cookie 可能经明文链路泄露；也不会下发 HSTS |
| `OCIBOT_TRUST_PROXY` | `1` | 限流把所有人算作 Caddy 一个 IP，一个人试错连累全部 |
| `OCIBOT_FORWARDED_ALLOW_IPS` | `172.16.0.0/12` | 填 `*` 等于让任何人伪造自己的 IP，限流形同虚设 |
| `OCIBOT_BIND` | `127.0.0.1` | `http://IP:8000` 仍能直连，绕过 HTTPS |

反过来切回 IP 模式时全部还原，其中 `COOKIE_SECURE` 必须同时改回 `0` ——
明文访问下浏览器根本不会回传 Secure Cookie，表现是**登录成功后立刻跳回登录页，
且没有任何报错**。这是两种模式之间最容易踩的一脚，也是脚本成组写入的原因。

证书和 ACME 账号存在 `ocibot_caddy_data` 卷里。**不要删这个卷**：
每次重建都重新签发很容易撞上 Let's Encrypt 的速率限制（同一组域名 5 次/周），
撞上之后要等一周。

---

## 切换前的前提

域名模式需要三件事都成立，缺一个证书就签不出来：

1. **DNS 已解析到这台服务器**。脚本会检查并在不一致时提示，但套了 CDN（如
   Cloudflare 橙云）时解析到的本来就是 CDN 的 IP，此时提示可以忽略。
2. **80 和 443 都放行**。脚本会尽力改 ufw / firewalld，但**云厂商的安全组改不到**，
   需要你自己在控制台放行。只开 443 是不够的 —— 80 端口是 Let's Encrypt
   校验域名归属用的。
3. **80/443 没被别的东西占着**。已经在跑 Nginx Proxy Manager 或宿主机 nginx 的话，
   脚本会先把占用者列出来并停下，不会去抢端口。

---

## 自检

```bash
# HTTP 自动跳 HTTPS
curl -sI http://panel.example.com | head -1        # 期望 308

# HTTPS 正常，且版本号是最新的
curl -s https://panel.example.com/api/health

# 公网不该还能直连 8000
curl -m 5 http://<服务器公网IP>:8000/api/health    # 期望超时或拒绝

# 安全响应头
curl -sI https://panel.example.com | grep -i strict-transport
```

最后一条没有 `Strict-Transport-Security`，说明 `OCIBOT_COOKIE_SECURE=1` 没生效
（应用只在该值为 1 时才下发 HSTS）。

**浏览器里再确认一次**：登录后打开任意实例的「网页终端」，能出命令行提示符
= WebSocket 正常。Caddy 默认透传 Upgrade，不需要像 NPM 那样单独打开开关。

---

## 排查

| 现象 | 原因 |
|---|---|
| 打不开，`docker compose logs caddy` 里是 ACME 报错 | DNS 没指向本机，或 80 端口没放行 |
| 容器反复重启 | `OCIBOT_DOMAIN` 是空的；执行 `bash scripts/install.sh domain <域名>` 重设 |
| 端口被占用起不来 | 还有别的反代在跑，先 `docker stop` 掉 |
| 登录后立刻被登出 | 用 `http://IP:8000` 访问了域名模式的面板；改用域名，或切回 IP 模式 |
| 登录提示请求过于频繁 | `TRUST_PROXY` 与实际部署不符，重跑一次切换命令即可复位 |

```bash
cd ~/ocibot && docker compose logs --tail 50 caddy
```

---

## 还是想用 Nginx Proxy Manager

也支持，见 [NPM-REVERSE-PROXY.md](NPM-REVERSE-PROXY.md)。
那条路的服务器端配置由 `scripts/setup-proxy.sh` 代做，但域名、证书、
**Websockets 开关**和**请求体上限**仍需在 NPM 网页里手动点一遍 ——
后两项漏掉时的表现分别是「网页终端连不上」和「备份导入报错」，
都不指向反代，很难联想到。内置方式没有这几步。

两者互斥：NPM 占着 80/443，就不要再启用 `tls` profile。
