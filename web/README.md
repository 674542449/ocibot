# OCIBot Web

多租户 Oracle Cloud 实例管理面板的网页版。

- 后端：Python **FastAPI**（复用共享 `app/` OCI 业务层）
- 前端：**Vue 3 + Vite + TypeScript**
- 数据库：**PostgreSQL**（生产 Docker 默认；本地开发可用 SQLite）
- 任务：独立 **Worker** 进程

## 生产部署（推荐）

仓库根目录一键（PostgreSQL + API + Worker）：

```bash
# 见 scripts/install.sh 与 docs/DEPLOY.md
cp web/.env.example web/.env   # 改密钥
docker compose up -d --build   # 在仓库根目录
```

打开 http://127.0.0.1:8000 （API 托管前端构建产物）。

## 本地开发（SQLite）

在仓库根目录：

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8000
# 另开终端
python -m web.backend.worker
```

前端：

```bash
cd web/frontend
npm install
npm run dev            # http://127.0.0.1:5173
```

## 环境变量

见 `web/.env.example` 与根目录 `docs/DEPLOY.md`。

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | SQLite 本地文件 | 生产由 compose 设为 Postgres |
| `OCIBOT_MASTER_KEY` | dev 默认 | 加密 OCI 私钥 |
| `OCIBOT_JWT_SECRET` | dev 默认 | JWT |
| `OCIBOT_API_WORKERS` | `2` | uvicorn 进程数 |
| `OCIBOT_DB_POOL_SIZE` | `10` | PG 连接池 |

## 安全说明

- OCI 私钥永不下发浏览器  
- HttpOnly Cookie 会话；HTTPS 下 `OCIBOT_COOKIE_SECURE=1`  
- 开放注册默认关闭（首个用户除外）

## 目录

```
web/
├── backend/     # FastAPI + worker
├── frontend/    # Vue 3
├── Dockerfile
├── docker-compose.yml   # 亦可在仓库根用 docker-compose.yml
└── .env.example
```
