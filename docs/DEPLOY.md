# 部署与一键运维

## 生产默认：PostgreSQL

Docker Compose 固定使用 **PostgreSQL 16**（服务名 `db`）。  
`DATABASE_URL` 由 compose 注入，无需在 `.env` 手写（除非外部托管数据库）。

本地开发仍可用 SQLite（不设 `DATABASE_URL` 时的默认）。

## 一键安装

见根目录 [README.md](../README.md)。脚本：

- `scripts/install.sh` — Linux / macOS  
- `scripts/install.ps1` — Windows + Docker Desktop  

二者都会：

1. 确保 Docker / Compose 可用  
2. clone（或使用当前仓库）  
3. 若不存在则生成 `web/.env`（随机 `POSTGRES_PASSWORD` / `MASTER_KEY` / `JWT_SECRET`）  
4. `docker compose up -d --build`  
5. 健康检查 `/api/health`

## 一键更新

```bash
./scripts/install.sh update
```

- 有 `.git`：fast-forward `pull`  
- 保留 `web/.env` 与 named volume `ocibot_pg`  
- 重新 build 镜像并滚动 `api` / `worker`

## 性能相关

| 项 | 实现 |
|----|------|
| DB | PostgreSQL + `pool_pre_ping` + 可配 pool size |
| API | `OCIBOT_API_WORKERS`（默认 2）多进程 uvicorn |
| 响应 | `GZipMiddleware` 压缩 JSON / 静态 |
| 前端 | 生产构建由 API 同域托管，减少跨域 |
| 健康检查 | 容器 `HEALTHCHECK` + compose health，便于编排 |

调大并发示例（`web/.env`）：

```
OCIBOT_API_WORKERS=4
OCIBOT_DB_POOL_SIZE=20
OCIBOT_DB_MAX_OVERFLOW=40
```

> WebSSH 会话计数在进程内；多 worker 时每进程各自限额。需要更强终端并发可把 `OCIBOT_API_WORKERS=1` 或后续改为 Redis 计数。

## 备份

1. **应用层**：面板「备份恢复」→ 加密 ZIP（含租户私钥）  
2. **数据库**：`docker compose exec db pg_dump -U ocibot ocibot > backup.sql`  
3. **密钥**：备份 `web/.env`（尤其 `OCIBOT_MASTER_KEY`）

## HTTPS

前面加 Caddy / Nginx / Cloudflare Tunnel，并设置：

```
OCIBOT_COOKIE_SECURE=1
OCIBOT_CORS_ORIGINS=https://your.domain
OCIBOT_REQUIRE_SECURE_SECRETS=1
```
