# OCIBot Web

多租户 Oracle Cloud 实例管理面板的 **网页版**。

- 后端：Python **FastAPI**（复用桌面版 `app/oci_client.py` 等业务层）
- 前端：**Vue 3 + Vite + TypeScript**
- 数据库：**PostgreSQL**（本地开发也可用 SQLite）
- 任务：独立 **Worker** 进程（容量重试 / 定时开关机）

桌面版 Tk 程序仍可独立使用；Web 与桌面共享 `app/` 下的 OCI 逻辑。

## 架构

```
浏览器 (Vue)
    │  /api/*
    ▼
FastAPI  ──► PostgreSQL
    │
    │  (同一套 DB)
    ▼
Worker   ──► Oracle Cloud API
             (oci_client)
```

## 功能（当前骨架）

| 模块 | 状态 |
|------|------|
| 注册 / 登录（JWT） | ✅ |
| 租户 CRUD + 连接测试 | ✅ |
| 实例列表（单租户 / 全租户） | ✅ |
| 电源操作 / 终止 / 重命名 API | ✅ |
| 容量重试任务（DB + Worker） | ✅ API + Worker；创建向导 UI 后续补 |
| 定时开关机 | ✅ |
| 创建实例向导 / 账单 / 监控 / NSG | ⏳ 后续迭代（后端可继续挂 `oci_client`） |

## 本地开发（推荐先 SQLite）

### 1. 后端

在仓库根目录：

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt -r web/backend/requirements.txt

# 可选环境变量（.env 放在启动目录或导出）
set OCIBOT_MASTER_KEY=your-long-random-string
set OCIBOT_JWT_SECRET=your-jwt-secret
# 默认 SQLite: web_data/ocibot_web.db

# 启动 API
python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8000

# 另开终端：启动 Worker（容量重试 / 定时任务需要）
python -m web.backend.worker
```

API 文档：<http://127.0.0.1:8000/docs>

### 2. 前端

```bash
cd web/frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5173>  
Vite 已将 `/api` 代理到 `8000`。

### 3. 首次使用

1. 打开网页 → **注册** 本地账号  
2. **租户** 页添加 OCI API（User/Tenancy/Fingerprint/Region/私钥）  
3. **测试连接** → **实例** 页刷新  

## Docker Compose（PostgreSQL）

```bash
cd web
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
# 编辑 .env 中的密码与密钥

docker compose up -d --build
```

- API: <http://127.0.0.1:8000>  
- 前端开发服: <http://127.0.0.1:5173>  
- Postgres: `localhost:5432` / 库 `ocibot`  

生产环境请：

1. 更换 `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET` / `POSTGRES_PASSWORD`  
2. 前面加 HTTPS 反代（Caddy / Nginx / Cloudflare Tunnel）  
3. 关闭或限制开放注册（改 `Settings.allow_open_registration`）  
4. 仅内网或强鉴权暴露管理面板  

## 目录

```
web/
├── backend/
│   ├── main.py          # FastAPI app
│   ├── worker.py        # 后台任务
│   ├── models.py        # SQLAlchemy
│   ├── routers/         # auth / tenants / instances / jobs
│   ├── oci_bridge.py    # Tenant 行 → app.oci_client
│   └── requirements.txt
├── frontend/            # Vue 3
├── docker-compose.yml
├── Dockerfile
└── README.md
```

## 安全说明

- OCI **私钥永不下发浏览器**；库中存 Fernet 密文，密钥来自 `OCIBOT_MASTER_KEY`  
- 更换 master key 后旧密文无法解密  
- 危险操作（终止实例）需前端确认；建议生产再加审计与二次验证  
- 容量重试沿用桌面版合规限制（最小间隔 60s、默认 180s、有限次数、429 退避、同租户串行）  

## 与桌面版关系

| | 桌面 (Tk) | Web |
|--|-----------|-----|
| 入口 | `python main.py` | `uvicorn` + Vue |
| 配置 | `tenants.json` + `.secret` | PostgreSQL / SQLite |
| 任务 | GUI 进程内 BackgroundRunner | 独立 worker 进程 |
| OCI 调用 | `app/oci_client.py` | **同一模块** |

## 下一步可做

1. 创建实例向导（镜像 / Shape / 自动网络）挂到 Web  
2. 账单与监控图表  
3. 串口/VNC 控制台、NSG 防火墙  
4. 加密 ZIP 备份/恢复 API  
5. 生产用 Nginx 托管 `frontend/dist` 静态资源  

## 免责声明

电源、创建、终止与自动化重试会影响线上资源与账单。请遵守 Oracle 服务条款；勿绕过限流保护。
