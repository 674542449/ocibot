# OCIBot

多租户 **Oracle Cloud Infrastructure** 实例管理面板 —— 自托管 Web 应用（FastAPI + Vue 3 + **PostgreSQL**）。

> 早期桌面（Tkinter）版已移除；仓库仅保留网页版。共用 OCI 业务层在 `app/`。

---

## 一键安装（推荐 · Docker + PostgreSQL）

**Linux / macOS**

```bash
# 将 <OWNER>/<REPO> 换成你的 GitHub 仓库
export OCIBOT_REPO_URL=https://github.com/<OWNER>/<REPO>.git
curl -fsSL https://raw.githubusercontent.com/<OWNER>/<REPO>/master/scripts/install.sh | bash
```

**Windows（PowerShell + Docker Desktop）**

```powershell
$env:OCIBOT_REPO_URL = "https://github.com/<OWNER>/<REPO>.git"
irm https://raw.githubusercontent.com/<OWNER>/<REPO>/master/scripts/install.ps1 | iex
# 或克隆后：
#   .\scripts\install.ps1 install
```

安装完成后打开 **http://127.0.0.1:8000** → **注册**首个账号（自动成为管理员）→ **租户**页粘贴 OCI API。

### 一键更新

```bash
# Linux/macOS（在安装目录或设置 OCIBOT_DIR）
./scripts/install.sh update

# Windows
.\scripts\install.ps1 update
```

脚本会：`git pull`（若是 clone）→ `docker compose up -d --build`，**保留 PostgreSQL 数据卷与 `web/.env` 密钥**。

### 常用命令

| 命令 | 作用 |
|------|------|
| `./scripts/install.sh status` | 容器状态 + 健康检查 |
| `./scripts/install.sh uninstall` | 停服务（默认保留数据库卷） |
| `OCIBOT_PURGE_DATA=1 ./scripts/install.sh uninstall` | 停服务并删库 |

---

## 功能一览

- 多用户账号（注册 / 登录 / JWT，**HttpOnly Cookie**，可选 **TOTP**）
- 多租户 OCI API（私钥 Fernet 加密存库，永不下发浏览器）
- 加密备份 / 恢复（密码 ZIP）
- Always Free 仪表盘 + **创建 / 改规格 / 扩容额度守卫**
- 实例列表 / 详情 / 电源 / 重命名 / 监控
- 创建向导（免费套餐预设、自动网络、容量重试）
- WebSSH 网页终端 + 串口/VNC 控制台
- 引导卷扩容 + SSH 自动扩展文件系统
- 块存储 / 对象存储管理
- 防火墙 NSG、公网 IP / IPv6、备份、自定义镜像
- 任务中心（容量重试 / 定时开关机）与多渠道通知

---

## 架构

```
浏览器 (Vue 3)
    │  /api/*  (HttpOnly cookie)
    ▼
API  (uvicorn multi-worker)  ──►  PostgreSQL 16
    │                                  ▲
Worker (容量重试 / 定时 / 通知)  ──────┘
    │
Oracle Cloud API   (app/oci_client.py)
```

生产默认 **Docker Compose**：`db` + `api` + `worker`；API 同时托管前端静态资源。

---

## 手动 Docker 部署

```bash
git clone https://github.com/<OWNER>/<REPO>.git ocibot
cd ocibot
cp web/.env.example web/.env
# 编辑 web/.env：POSTGRES_PASSWORD / OCIBOT_MASTER_KEY / OCIBOT_JWT_SECRET
# 生产建议：OCIBOT_REQUIRE_SECURE_SECRETS=1  OCIBOT_COOKIE_SECURE=1（HTTPS）

docker compose up -d --build
# http://127.0.0.1:8000
```

性能相关环境变量（`web/.env` 或 compose）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `OCIBOT_API_WORKERS` | `2` | uvicorn 进程数 |
| `OCIBOT_DB_POOL_SIZE` | `10` | SQLAlchemy 连接池 |
| `OCIBOT_DB_MAX_OVERFLOW` | `20` | 池溢出连接 |
| `OCIBOT_PORT` | `8000` | 宿主机端口 |

---

## 本地开发（SQLite 亦可）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# API
python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8000

# Worker（另开终端）
python -m web.backend.worker

# 前端
cd web/frontend && npm install && npm run dev   # http://127.0.0.1:5173
```

开发默认 SQLite：`web_data/ocibot_web.db`。要连本机 PostgreSQL 时设置：

```bash
export DATABASE_URL=postgresql+psycopg://ocibot:pass@127.0.0.1:5432/ocibot
```

---

## 安全要点

- **私钥永不下发浏览器**；库中 Fernet 密文，密钥 = `OCIBOT_MASTER_KEY`（换 key 后旧密文失效）
- 首个注册用户为管理员；之后默认关闭开放注册
- 公网务必：HTTPS + `OCIBOT_COOKIE_SECURE=1` + 强随机密钥 + `OCIBOT_REQUIRE_SECURE_SECRETS=1`
- 备份 `web/.env` 与 PostgreSQL 卷；可用面板「备份恢复」导出加密 ZIP

---

## 项目结构

```
ocibot/
├── docker-compose.yml      # 生产：Postgres + API + Worker
├── scripts/install.sh      # Linux/macOS 一键安装/更新
├── scripts/install.ps1     # Windows 一键安装/更新
├── app/                    # OCI 业务层
├── web/
│   ├── backend/            # FastAPI + worker
│   ├── frontend/           # Vue 3
│   ├── Dockerfile
│   └── .env.example
├── tests/
└── README.md
```

---

## 权限提示（OCI 策略）

```
Allow group <group> to manage instance-family in compartment <compute-compartment>
Allow group <group> to manage virtual-network-family in compartment <network-compartment>
Allow group <group> to manage volume-family in compartment <compute-compartment>
Allow group <group> to manage object-family in compartment <compute-compartment>
Allow group <group> to inspect compartments in tenancy
```

---

## 许可证 / 免责

电源、创建、终止与自动化重试会影响线上资源与账单。请遵守 Oracle 服务条款；勿绕过限流保护。
