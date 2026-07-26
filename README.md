# OCIBot

自托管的 **Oracle Cloud Infrastructure（OCI）** 多租户管理面板。

用于在一台服务器上统一管理多个 OCI 账号：创建与维护 Always Free / 付费实例、容量重试抢机、定时开关机、WebSSH、存储与防火墙，以及备份恢复。

当前主线为 Web 版（FastAPI + Vue 3 + PostgreSQL）。面板版本以 `/api/health` 的 `version` 与 [CHANGELOG.md](CHANGELOG.md) 为准。

---

## 功能

| 模块 | 能力 |
|------|------|
| 账号 | 注册 / 登录、HttpOnly Cookie 会话、可选 TOTP、管理员用户管理 |
| 租户 | 多 OCI API 配置、私钥 Fernet 加密存库、连接测试、账号等级识别 |
| 实例 | 列表 / 详情、电源操作、重命名、监控曲线、公网 IP / IPv6 |
| 创建 | 免费套餐预设、自动默认网络、容量不足自动重试（Worker 执行） |
| 终端 | 浏览器 WebSSH；串口 / VNC 控制台连接 |
| 存储 | 引导卷扩容与备份、块存储、对象存储 |
| 网络 | NSG 防火墙规则、保留公网 IP |
| 任务 | 容量重试任务、定时开关机 |
| 通知 | Telegram / Bark / ServerChan / Webhook / SMTP（含 SSRF 防护） |
| 运维 | 加密租户备份、面板内一键更新（Docker 部署） |

---

## 架构

```
浏览器 (Vue 3 SPA)
        │  HTTPS / Cookie
        ▼
   API 容器 (FastAPI + 静态前端)
        │
        ├── PostgreSQL
        └── Worker 容器（容量重试 / 定时 / 通知）
                │
                ▼
           Oracle Cloud API
```

- 生产推荐：`docker compose` 启动 `db` + `api` + `worker`
- API 进程同时托管前端构建产物，单入口访问
- OCI 私钥仅服务端加密存储，不回传浏览器

---

## 快速安装（推荐）

### Linux / macOS

```bash
export OCIBOT_REPO_URL=https://github.com/674542449/ocibot.git
export OCIBOT_BRANCH=main
curl -fsSL https://raw.githubusercontent.com/674542449/ocibot/main/scripts/install.sh | bash
```

### Windows（PowerShell + Docker Desktop）

```powershell
$env:OCIBOT_REPO_URL = "https://github.com/674542449/ocibot.git"
$env:OCIBOT_BRANCH = "main"
irm https://raw.githubusercontent.com/674542449/ocibot/main/scripts/install.ps1 | iex
```

安装完成后：

1. 打开 `http://127.0.0.1:8000`
2. **注册第一个账号**（自动成为管理员）
3. 进入 **租户**，粘贴 OCI API 配置与私钥
4. 在 **实例 / 创建实例** 开始使用

### 更新

```bash
# 安装目录内
./scripts/install.sh update

# Windows
.\scripts\install.ps1 update
```

更新会拉取代码、重建镜像并滚动重启，**保留 PostgreSQL 数据卷与 `web/.env` 密钥**。  
Docker 部署且已挂载仓库与 `docker.sock` 时，也可在管理员页使用「一键更新」。

### 常用命令

| 命令 | 说明 |
|------|------|
| `./scripts/install.sh status` | 容器状态与健康检查 |
| `./scripts/install.sh uninstall` | 停止服务（默认保留数据卷） |
| `OCIBOT_PURGE_DATA=1 ./scripts/install.sh uninstall` | 停止并删除数据卷 |

---

## 手动 Docker 部署

```bash
git clone https://github.com/674542449/ocibot.git
cd ocibot
cp web/.env.example web/.env
# 编辑 web/.env，至少设置：
#   POSTGRES_PASSWORD
#   OCIBOT_MASTER_KEY
#   OCIBOT_JWT_SECRET

# 必须：compose 的 ${VAR} 插值只读项目根目录的 .env，不读服务的 env_file。
# 少了这一步，POSTGRES_PASSWORD 会回退到内置默认密码。
ln -s web/.env .env

export OCIBOT_HOST_REPO="$(pwd -P)"   # 在线更新需要绝对路径
docker compose up -d --build
```

访问：`http://127.0.0.1:8000`  
健康检查：`curl -s http://127.0.0.1:8000/api/health`

---

## 配置说明

主要环境变量写在 `web/.env`（或 compose 环境）：

| 变量 | 建议 | 说明 |
|------|------|------|
| `POSTGRES_PASSWORD` | 强随机 | 数据库密码 |
| `OCIBOT_MASTER_KEY` | ≥24 位随机 | 加密 OCI 私钥 / TOTP 等 |
| `OCIBOT_JWT_SECRET` | ≥24 位随机 | 签发会话 JWT |
| `OCIBOT_REQUIRE_SECURE_SECRETS` | 生产 `1` | 拒绝使用内置弱密钥启动 |
| `OCIBOT_COOKIE_SECURE` | HTTPS 下 `1` | 仅通过 HTTPS 发送登录 Cookie |
| `OCIBOT_CORS_ORIGINS` | 精确来源列表 | 浏览器跨域白名单 |
| `OCIBOT_ALLOW_OPEN_REGISTRATION` | 默认 `0` | 首用户后是否开放注册 |
| `OCIBOT_TRUST_PROXY` | 默认 `0` | 是否信任 `X-Forwarded-For`（仅反代后开启） |
| `OCIBOT_FORWARDED_ALLOW_IPS` | 反代地址/CIDR | 允许携带代理头的来源，默认回环；勿用 `*` |
| `OCIBOT_API_WORKERS` | 默认 `2` | API 进程数 |
| `OCIBOT_PORT` | 默认 `8000` | 宿主机映射端口 |
| `OCIBOT_BIND` | 反代后设 `127.0.0.1` | 端口绑定的宿主机网卡，默认 `0.0.0.0` |
| `OCIBOT_UPDATE_ENABLED` | 默认 `0` | 面板内自更新开关（`install.sh` 会置 `1`） |
| `OCIBOT_HOST_REPO` | 宿主机绝对路径 | 自更新绑定的代码目录 |

安装脚本会生成随机密钥并默认开启 `OCIBOT_REQUIRE_SECURE_SECRETS=1`。

---

## 安全建议（上线前）

1. 使用强随机 `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET`（≥24 位），并设置 `OCIBOT_REQUIRE_SECURE_SECRETS=1`
   - 主密钥经单次 SHA-256 派生 Fernet 密钥：短密钥在数据库泄露后可被离线爆破
2. 前置 HTTPS 反代，设置 `OCIBOT_COOKIE_SECURE=1`（同时才会下发 HSTS），并设 `OCIBOT_BIND=127.0.0.1`
3. 限制 `OCIBOT_CORS_ORIGINS` 为真实访问域名。**`*` 会被忽略**：通配符 + Cookie 凭据
   等于任意站点都能以登录用户身份读取 API
4. 首用户注册后保持关闭开放注册；需要加用户时再临时打开
5. `OCIBOT_TRUST_PROXY=1` 只在受信反代后开启，并把 `OCIBOT_FORWARDED_ALLOW_IPS`
   设为反代地址。**直连部署务必保持 `0`**：否则客户端可伪造 `X-Forwarded-For`
   绕过登录限流，无限次尝试密码
6. 面板内自更新默认关闭。开启后它会驱动挂载的 `docker.sock`（并可能进入宿主机命名空间），
   **管理员失陷 ≈ 宿主机 root 失陷**；仅在所有管理员可信时设 `OCIBOT_UPDATE_ENABLED=1`
7. Webhook / Bark / SMTP 目标已拦截私网、元数据、NAT64/6to4 等地址，仍建议只给受信用户开通知配置
   - 已知残留风险：DNS rebinding（校验与连接之间 DNS 可变）；详见 [web/AUDIT.md](web/AUDIT.md)

更细的审计说明见 [web/AUDIT.md](web/AUDIT.md)。

---

## 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# API（可使用 SQLite：不设 DATABASE_URL 时默认本地文件库）
python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8000

# 另开终端：Worker
python -m web.backend.worker

# 前端开发
cd web/frontend
npm install
npm run dev
```

测试：

```bash
python -m pytest tests -q
```

---

## 目录结构

```
ocibot/
├── app/                 # OCI 业务层（与 Web 共用）
├── web/
│   ├── backend/         # FastAPI、Worker、认证、通知、自更新
│   ├── frontend/        # Vue 3 控制台
│   └── .env.example
├── scripts/
│   ├── install.sh       # Linux/macOS 安装与更新
│   └── install.ps1      # Windows 安装与更新
├── docker-compose.yml
├── CHANGELOG.md
└── README.md
```

---

## 使用提示

- **实例列表**默认显示第一个租户；可在下拉框切换，不再默认聚合全部租户
- **密码到期提醒**是面板本地策略（可改天数，`0` 关闭），不会替你修改 Oracle 控制台密码
- **容量重试**由 Worker 执行；侧栏会提示 Worker 是否在线
- **备份恢复**导出加密 ZIP，导入只创建当前用户名下的新租户，不会覆盖他人数据

---

## 故障排查

| 现象 | 处理 |
|------|------|
| 页面打不开 | `./scripts/install.sh status`；`docker compose logs api --tail=100` |
| 更新后版本不变 | `curl -s http://127.0.0.1:8000/api/health`；浏览器强刷；确认 `OCIBOT_HOST_REPO` 为绝对路径 |
| 容量重试不跑 | 侧栏 Worker 离线提示；检查 worker 容器日志 |
| 登录限流异常 | 直连部署保持 `OCIBOT_TRUST_PROXY=0`；反代需覆盖客户端 IP 头后再开启 |
| 私钥解密失败 | `OCIBOT_MASTER_KEY` 被更换；需用旧密钥或重新导入租户 / 恢复备份 |

---

## 许可证与免责

本项目按仓库内许可证条款提供。请遵守 Oracle Cloud 服务条款与当地法规；容量重试、自动操作等能力由使用者自行配置与承担风险。

---

## 链接

- 仓库：<https://github.com/674542449/ocibot>
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 安全审计摘记：[web/AUDIT.md](web/AUDIT.md)
