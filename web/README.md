# OCIBot Web

OCIBot 的网页应用：多租户 Oracle Cloud（OCI）实例管理面板。安装、配置、安全建议、
本地开发与故障排查都以仓库根目录的 [README.md](../README.md) 为准，这里只记录
`web/` 目录本身的结构。

- 后端：Python **FastAPI**（复用仓库根目录 `app/` 的 OCI 业务层）
- 前端：**Vue 3 + Vite + TypeScript**
- 数据库：**PostgreSQL 16**（生产 Docker 默认；本地开发不设 `DATABASE_URL` 时用 SQLite）
- 后台：独立 **Worker** 进程（容量重试与通知）

## 目录

```
web/
├── backend/
│   ├── main.py          # 应用工厂、安全响应头 / CSP、托管前端构建产物
│   ├── worker.py        # 容量重试 Worker
│   ├── routers/         # 按页面划分的 API 路由
│   ├── models.py        # SQLAlchemy 模型（_ensure_schema 只加列不删列）
│   └── schemas.py       # 请求 / 响应模型
├── frontend/
│   ├── public/          # logo.svg、favicon、PWA 清单与图标（位图由 scripts/make_favicon.py 生成）
│   └── src/             # views（页面）、layouts、components、styles.css（全站配色 token）
├── Dockerfile           # 多阶段构建：node 构建前端 → python 运行
├── docker-compose.yml   # 向后兼容：在 web/ 内也能起整套服务；推荐用仓库根目录的那份
├── .env.example         # 环境变量模板（三个密钥项故意留空，照抄会起不来）
└── AUDIT.md             # 安全审计记录与已知的、刻意接受的残留风险
```

## 常用环境变量

完整列表见根目录 README 的「配置说明」。

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | 本地 SQLite 文件 | 生产由 compose 注入 PostgreSQL 连接串 |
| `OCIBOT_MASTER_KEY` | 无（必填） | 加密 OCI 私钥、TOTP 等；≥24 位随机，改了之后已存的私钥全部解不开 |
| `OCIBOT_JWT_SECRET` | 无（必填） | 签发会话 JWT；≥24 位随机 |
| `OCIBOT_API_WORKERS` | `1` | uvicorn 进程数。负载是等 OCI 网络而不是烧 CPU，加进程不会更快 |
| `OCIBOT_DB_POOL_SIZE` | `10` | PostgreSQL 连接池大小 |

## 安全要点

- OCI 私钥只在服务端加密保存，永不下发浏览器
- HttpOnly Cookie 会话；HTTPS 部署下设 `OCIBOT_COOKIE_SECURE=1`
- 首个用户注册后默认关闭开放注册
- 更多见 [AUDIT.md](AUDIT.md)
