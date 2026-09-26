<p align="center">
  <img src="web/frontend/public/logo.svg" width="88" height="88" alt="OCIBot" />
</p>

<h1 align="center">OCIBot</h1>

<p align="center">
  自托管的 Oracle Cloud Infrastructure（OCI）多账号管理面板<br />
  在一处管理多个 OCI 租户：创建与维护实例、容量不足自动重试、WebSSH、存储与防火墙、备份恢复
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 16" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose" />
</p>

面板部署在你自己的服务器上，OCI API 私钥只在服务端加密保存、不回传浏览器。
当前版本以 `/api/health` 返回的 `version` 与 [CHANGELOG.md](CHANGELOG.md) 为准。

> 本项目与 Oracle 公司无关，不是 Oracle 官方产品。所有云端操作都通过 Oracle 官方
> OCI Python SDK 完成。

**文档导航**：[快速安装](#快速安装推荐) · [手动部署](#手动-docker-部署) · [配置说明](#配置说明) ·
[安全建议](#安全建议上线前) · [本地开发](#本地开发) · [故障排查](#故障排查) ·
[部署细节](docs/DEPLOY.md) · [访问方式](docs/ACCESS-MODES.md) · [换机重装](docs/REDEPLOY.md) ·
[NPM 反代](docs/NPM-REVERSE-PROXY.md) · [安全审计](web/AUDIT.md) · [变更记录](CHANGELOG.md)

---

## 功能

| 模块 | 能力 |
|------|------|
| 账号 | 注册 / 登录、HttpOnly Cookie 会话、可选 TOTP 两步验证、全设备退出、管理员用户管理 |
| 租户 | 粘贴 `~/.oci/config` + 私钥导入、私钥 Fernet 加密存库、测试连接、账号等级识别、「锁定默认」租户、多选批量删除、删除保护（受保护的排在最前）、区域显示 Oracle 官方中文名 |
| 副区 | 查看 / 开通同一账号的其他国家与地区，开通后自动添加同凭据的副区租户 |
| 实例 | 列表 / 搜索 / 导出 CSV、详情、开关机与重启、重命名、改 Flex 规格、终止保护、root 密码查看、监控曲线、引导日志、「以此为模板创建」 |
| 创建 | Always Free 额度预设与硬拦截、Ubuntu / Oracle Linux / 已有自定义镜像、自动准备默认网络、root + SSH 公钥 / root + 密码、首次启动脚本（cloud-init）、一次批量创建多台、可在副区创建 |
| 容量重试 | 容量不足时由 Worker 按合规间隔自动重试、一个任务可抢多台（开够为止）、可轮询全部 AD、Flex 降级候选、实时尝试日志、结果推送通知 |
| 网络 | 更换临时公网 IP、保留公网 IP、分配 / 取消 IPv6、实例安全组（NSG）与子网安全列表规则管理、一键放行 Cloudflare 网段、一键修复 / 收紧 |
| 终端 | 浏览器 WebSSH（主机密钥按实例校验）；串口 / VNC 控制台连接 |
| 存储 | 引导卷扩容与性能（VPU）调整（可选自动扩展文件系统）、引导卷备份、块存储、对象存储 |
| 账号用量 | Always Free 额度仪表盘、近 30 天费用、每月账单 |
| 通知 | Telegram / Bark / ServerChan / Webhook / SMTP（含 SSRF 防护） |
| 运维 | 审计日志、加密租户备份与恢复、面板内一键更新（Docker 部署，默认关闭） |

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
            云服务商 API
```

- 生产推荐：`docker compose` 启动 `db` + `api` + `worker`
- API 进程同时托管前端构建产物，单入口访问
- API 私钥仅服务端加密存储，不回传浏览器

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

安装时会问一句怎么访问，二选一（`curl | bash` 无终端时默认 IP 直连）：

1. **IP + 端口** —— `http://服务器IP:8000`，不用域名，明文
2. **域名 + HTTPS** —— `https://panel.example.com`，证书自动签发续期

安装完成后：

1. 打开脚本最后打印的那个地址
2. **注册第一个账号**（自动成为管理员）
3. 进入 **租户**，粘贴云账号的 API 配置与私钥
4. 在 **实例 / 创建实例** 开始使用

### 访问方式（随时可换，不影响数据）

面板自带 HTTPS 终结，**不需要另外装 Nginx Proxy Manager 或任何反代**：

```bash
# 域名 + 自动 HTTPS（内置 Caddy 签证书）
./scripts/install.sh domain panel.example.com

# 改回 IP + 端口直连
./scripts/install.sh ip
```

切换会成组改写 Cookie、限流、端口绑定这几项配置 —— 它们必须和访问方式一致，
配错不会报错，只会表现成「登录后立刻被登出」这类看不出原因的现象。
细节与排查见 [docs/ACCESS-MODES.md](docs/ACCESS-MODES.md)。

> 域名模式需要 DNS 已指向本机，且 **80 和 443 都放行**（80 是证书校验用的）。
> 云厂商安全组脚本改不到，需要自己在控制台放行。
>
> 换机器重装：[docs/REDEPLOY.md](docs/REDEPLOY.md) 是从零到可用的完整步骤。
> 仍想用 Nginx Proxy Manager：[docs/NPM-REVERSE-PROXY.md](docs/NPM-REVERSE-PROXY.md)。

### 更新

```bash
# 安装目录内
./scripts/install.sh update

# Windows
.\scripts\install.ps1 update
```

更新会拉取代码、重建镜像并滚动重启，**保留 PostgreSQL 数据卷与 `web/.env` 密钥**。

**面板内「一键更新」默认关闭**，需要显式打开：

```bash
./scripts/install.sh update-on    # 打开
./scripts/install.sh update-off   # 关掉
```

打开它 = API 容器改为 root 运行、挂载宿主机 `docker.sock`、把整个仓库目录（里面就有
`web/.env`：主密钥 / JWT 密钥 / 数据库密码）以可写方式挂进去，执行更新时还会进宿主机
命名空间。**任何一个管理员会话，或 API 进程里任何一处代码执行，都等于宿主机 root。**
多管理员、或管理员账号不完全可信的安装，请保持关闭并用上面的 SSH 方式更新。

> 0.4.80 之前这些挂载是无条件的：`OCIBOT_UPDATE_ENABLED=0` 只关掉了按钮，socket 和
> 仓库照样挂着；而且 `install.sh` 每次都导出 `=1` 覆盖掉操作者写在 `web/.env` 里的 `0`。
> 现在开关和挂载在同一个文件（`docker-compose.update.yml`）里，不会再分家。
> **既有安装升级后一键更新会变成关闭状态**，这是刻意的 —— 需要就跑一次 `update-on`。

### 常用命令

| 命令 | 说明 |
|------|------|
| `./scripts/install.sh status` | 容器状态、健康检查与当前访问地址 |
| `./scripts/install.sh domain <域名>` | 切换到域名 + 自动 HTTPS |
| `./scripts/install.sh ip` | 切换回 IP + 端口直连 |
| `./scripts/install.sh update-on` | 打开面板内一键更新（挂 `docker.sock`，管理员 ≈ 宿主机 root） |
| `./scripts/install.sh update-off` | 关闭面板内一键更新 |
| `./scripts/install.sh uninstall` | 停止服务（默认保留数据卷） |
| `OCIBOT_PURGE_DATA=1 ./scripts/install.sh uninstall` | 停止并删除数据卷 |

---

## 手动 Docker 部署

```bash
git clone https://github.com/674542449/ocibot.git
cd ocibot
cp web/.env.example web/.env

# 三个密钥项在 .env.example 里是空的，必须自己生成 —— 留空会让 API 拒绝启动
# （OCIBOT_REQUIRE_SECURE_SECRETS=1），compose 也会因 POSTGRES_PASSWORD 为空直接报错。
printf 'OCIBOT_MASTER_KEY=%s\n' "$(openssl rand -hex 48)" >> web/.env
printf 'OCIBOT_JWT_SECRET=%s\n' "$(openssl rand -hex 48)" >> web/.env
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 16)" >> web/.env
chmod 600 web/.env

# 必须：compose 的 ${VAR} 插值只读项目根目录的 .env，不读服务的 env_file。
ln -s web/.env .env

docker compose up -d --build
```

访问：`http://127.0.0.1:8000`  
健康检查：`curl -s http://127.0.0.1:8000/api/health`

> **不要照抄 `.env.example` 里的值就上线。** 以前它带着一组可用的默认主密钥，而主密钥
> 只经一次 SHA-256 派生 Fernet 密钥 —— 用默认值跑起来的面板，等于把每个租户的 OCI 私钥
> 交给任何看过这个公开仓库的人。现在那几项是空的，照抄会起不来，这是故意的。
>
> 手动部署默认**不含**面板内一键更新（不挂 `docker.sock`）。要它的话：
> ```bash
> export OCIBOT_HOST_REPO="$(pwd -P)"    # 必须是宿主机绝对路径
> docker compose -f docker-compose.yml -f docker-compose.update.yml up -d
> ```
> 代价见上面「更新」一节。

---

## 配置说明

主要环境变量写在 `web/.env`（或 compose 环境）：

| 变量 | 建议 | 说明 |
|------|------|------|
| `POSTGRES_PASSWORD` | 强随机 | 数据库密码 |
| `OCIBOT_MASTER_KEY` | ≥24 位随机 | 加密 API 私钥 / TOTP 等 |
| `OCIBOT_JWT_SECRET` | ≥24 位随机 | 签发会话 JWT |
| `OCIBOT_REQUIRE_SECURE_SECRETS` | 生产 `1` | 拒绝使用内置弱密钥启动 |
| `OCIBOT_COOKIE_SECURE` | HTTPS 下 `1` | 仅通过 HTTPS 发送登录 Cookie |
| `OCIBOT_CORS_ORIGINS` | 精确来源列表 | 浏览器跨域白名单 |
| `OCIBOT_ORIGIN_CHECK` | 默认 `1` | 拒绝 Origin 与本站不符的写请求（防 CSRF）。若反代改写了 `Host` 且未发 `X-Forwarded-Host`，先把公开地址加进 `OCIBOT_CORS_ORIGINS`，应急才设 `0` |
| `OCIBOT_AUDIT_RETENTION_DAYS` | 默认 `180` | 审计日志保留天数，`0` 为不限 |
| `OCIBOT_AUDIT_MAX_ROWS` | 默认 `50000` | 审计日志行数上限，超出删最旧；`0` 为不限 |
| `OCIBOT_ALLOW_OPEN_REGISTRATION` | 默认 `0` | 首用户后是否开放注册 |
| `OCIBOT_TRUST_PROXY` | 默认 `0` | 是否信任 `X-Forwarded-For`（仅反代后开启） |
| `OCIBOT_FORWARDED_ALLOW_IPS` | 反代地址/CIDR | 允许携带代理头的来源，默认回环；勿用 `*` |
| `OCIBOT_API_WORKERS` | 默认 `1` | API 进程数。这个负载是等 OCI 网络不是烧 CPU，加进程不会更快，每个多占约 158 MB |
| `OCIBOT_PORT` | 默认 `8000` | 宿主机映射端口 |
| `OCIBOT_BIND` | 反代后设 `127.0.0.1` | 端口绑定的宿主机网卡，默认 `0.0.0.0` |
| `OCIBOT_WORKER_BACKGROUND_OCI` | 默认 `1` | 设 `0` 则 Worker 完全不主动发起云 API 请求；容量重试任务将**不执行**（面板会明确提示） |
| `OCIBOT_UPDATE_ENABLED` | 默认 `0` | 面板内自更新。改这一行不够，用 `install.sh update-on/update-off`：它同时决定要不要叠加 `docker-compose.update.yml`（`docker.sock` + 可写仓库） |
| `OCIBOT_HOST_REPO` | 宿主机绝对路径 | 自更新绑定的代码目录（关闭自更新时以只读方式挂载，仅用于显示版本） |
| `OCIBOT_NODE_IMAGE` / `OCIBOT_PYTHON_IMAGE` | 建议钉 `@sha256:` | 构建用基础镜像。默认是可变标签，每次更新都会重新拉 |
| `OCIBOT_POSTGRES_IMAGE` / `OCIBOT_CADDY_IMAGE` / `OCIBOT_DOCKER_CLI_IMAGE` | 建议钉 `@sha256:` | 运行用镜像，同上 |

安装脚本会生成随机密钥并默认开启 `OCIBOT_REQUIRE_SECURE_SECRETS=1`。

镜像钉版写在 `web/.env` 里，**不要改 `web/Dockerfile`** —— `install.sh update` 会
`git reset --hard`，改仓库文件活不过一次更新。digest 用
`docker buildx imagetools inspect <标签>` 取。

---

## 安全建议（上线前）

1. 使用强随机 `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET`（≥24 位），并设置 `OCIBOT_REQUIRE_SECURE_SECRETS=1`
   - 主密钥经单次 SHA-256 派生 Fernet 密钥：短密钥在数据库泄露后可被离线爆破
2. 前置 HTTPS 反代，设置 `OCIBOT_COOKIE_SECURE=1`（同时才会下发 HSTS），并设 `OCIBOT_BIND=127.0.0.1`
   —— **注意**：反代若也是 Docker 容器，它访问不到宿主机的 `127.0.0.1`，需让它接入面板所在的
   Docker 网络后按容器名转发。一键配置：`bash scripts/setup-proxy.sh <你的域名>`；
   手动步骤与排查见 [docs/NPM-REVERSE-PROXY.md](docs/NPM-REVERSE-PROXY.md)
3. 限制 `OCIBOT_CORS_ORIGINS` 为真实访问域名。**`*` 会被忽略**：通配符 + Cookie 凭据
   等于任意站点都能以登录用户身份读取 API
4. 首用户注册后保持关闭开放注册；需要加用户时再临时打开
5. `OCIBOT_TRUST_PROXY=1` 只在受信反代后开启，并把 `OCIBOT_FORWARDED_ALLOW_IPS`
   设为反代地址。**直连部署务必保持 `0`**：否则客户端可伪造 `X-Forwarded-For`
   绕过登录限流，无限次尝试密码
6. 面板内自更新默认关闭，且**开关和挂载绑在一起**：`docker.sock` 与可写仓库只存在于
   `docker-compose.update.yml` 这个叠加层里，用 `./scripts/install.sh update-on` 才会装载。
   开启后它会驱动挂载的 `docker.sock`（并可能进入宿主机命名空间），**管理员失陷 ≈ 宿主机
   root 失陷**；仅在所有管理员可信时开启，否则用 SSH 更新
7. **不要给容器加回 root。** `api` / `worker` 以 UID 10001 运行，`cap_drop: [ALL]`、
   根文件系统只读、只有 `/tmp` 是 tmpfs。这个进程持有解密全部租户 OCI 私钥的主密钥，
   上述四项决定了「Web 层被打穿」和「宿主机被打穿」不是同一件事。要改动它们，先想清楚
   替代的边界在哪
8. 建议把基础镜像钉成 `@sha256:`（见配置表）。默认的可变标签配合每次更新的
   `compose build --pull`，等于每次更新都信任上游当天的内容；依赖同理，
   `web/backend/requirements.txt` 现在是精确 `==`，升级依赖应当是一次显式提交
9. Webhook / Bark / SMTP 目标已拦截私网、元数据、NAT64/6to4 等地址，仍建议只给受信用户开通知配置
   - 已知残留风险：DNS rebinding（校验与连接之间 DNS 可变）；详见 [web/AUDIT.md](web/AUDIT.md)
10. **WebSSH 会校验主机密钥**（首次连接记录指纹，之后不符即拒绝，且在发送任何凭据之前）。
    指纹按**实例 ID** 记录，所以换公网 IP 不会误报。重装系统后需在实例详情页
    「重置主机密钥」再连接

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

前端开发服务器默认 `http://127.0.0.1:5173`，会把 `/api` 代理到 8000 端口。
需要 Node.js 22（与 `web/Dockerfile` 构建用的 `node:22-alpine` 一致）。

### 提交前检查

```bash
# 后端：全量测试（并行；用 -n 6，不要用 -n auto，原因见 pytest.ini）
python -m pytest tests -q -n 6

# 前端：类型检查 + 生产构建（两者都只查类型 / 语法，逻辑要靠自己想清楚）
cd web/frontend
npx vue-tsc --noEmit -p tsconfig.json
npm run build
```

`tests/test_endpoint_smoke.py` 会用一个桩 OCI 会话把每个接口都调一遍、任何 5xx 都算失败 ——
新增接口请把它加进去。

### 发版约定

- **每次改动都要升版本**：`web/backend/config.py` 里的 `app_version` 与 `CHANGELOG.md`
  最新的 `## X.Y.Z` 标题必须在同一个提交里一起改（`tests/test_version_bump.py` 会检查）。
  `/api/health` 的版本号是运维确认「更新到底生效没有」的唯一手段
- **不要删除数据库里还存在的列**：`_ensure_schema()` 只加列不删列，功能下线时保留映射，
  否则老库的 INSERT 会因 NOT NULL 失败（详见 `CLAUDE.md`）
- 更多「刻意这样设计、不要改回去」的约定见 [CLAUDE.md](CLAUDE.md)

### 品牌标记与图标

全站只有一个标记，所有位置都来自同一份几何：

| 文件 | 用途 |
|------|------|
| `web/frontend/public/logo.svg` | 唯一的矢量源：侧边栏、手机顶栏、登录页、README 顶部都引用它 |
| `web/frontend/public/favicon.svg` | 标签页图标，与 `logo.svg` 逐字相同 |
| `favicon.ico` / `apple-touch-icon.png` / `icon-192.png` / `icon-512.png` | 由 `python scripts/make_favicon.py` 生成的位图（标签页兜底、iOS 主屏、Android / PWA） |

要改标记：先改 `scripts/make_favicon.py` 里的几何常量并重跑，再同步两个 SVG；
`tests/test_brand_mark.py` 会检查它们是否一致。

---

## 目录结构

```
ocibot/
├── app/                        # OCI 业务层（SDK 调用、额度计算、格式化），与 Web 共用
├── web/
│   ├── backend/                # FastAPI 应用、Worker、认证、通知、自更新
│   │   └── routers/            # 各页面对应的 API 路由
│   ├── frontend/               # Vue 3 + Vite + TypeScript 控制台
│   │   ├── public/             # logo、favicon、PWA 清单与图标
│   │   └── src/                # 页面（views）、布局、组件、样式
│   ├── Dockerfile
│   └── .env.example            # 环境变量模板（密钥项故意留空）
├── tests/                      # pytest 测试（含接口冒烟、安全回归）
├── docs/                       # 部署、访问方式、换机重装、反代说明
├── deploy/Caddyfile            # 域名模式下内置 Caddy 的配置
├── scripts/
│   ├── install.sh              # Linux / macOS 安装、更新、切换访问方式
│   ├── install.ps1             # Windows 安装与更新
│   ├── setup-proxy.sh          # 接入已有 Nginx Proxy Manager
│   ├── make_favicon.py         # 生成全部位图图标
│   └── trim_oci_sdk.py         # 构建镜像时裁掉用不到的 OCI SDK 模块
├── docker-compose.yml
├── docker-compose.update.yml   # 可选叠加层：面板内一键更新（docker.sock）
├── CHANGELOG.md
├── CLAUDE.md                   # 维护约定（发版、兼容性、刻意的设计）
└── README.md
```

---

## 使用提示

- **各页面不会自动拉取 Oracle 数据**，进入页面后点「刷新 / 加载配置」才发请求 ——
  这是为了把 API 调用额度留给容量重试，不是 bug
- **实例列表**一次显示一个租户（默认第一个，或「锁定默认」的那个）；可在下拉框切换，
  不提供跨租户的聚合列表
- **密码到期查询**（租户页按钮）读取 Oracle Identity Domain 的 `defaultPasswordPolicy`；
  若仍设着有效期，会顺手调用 Oracle 接口关闭强制改密，结果以提示条显示
- **删除保护**只挡删除（单个、批量都会拒绝），编辑、测试连接照常可用；副区随主租户一起删，
  所以任一副区受保护时，主租户也删不了
- **容量重试**由 Worker 执行；侧栏会提示 Worker 是否在线。它是唯一会主动发起云 API
  请求的后台功能，且仅在存在任务时运行；`OCIBOT_WORKER_BACKGROUND_OCI=0` 可完全停掉
- **备份恢复**导出加密 ZIP，导入只创建当前用户名下的新租户，不会覆盖他人数据
- **附加区域**（其他国家 / 地区）在「租户 → 副区管理」开通，面板会自动添加一条同凭据的
  租户记录，各页面把它当普通租户使用；注意已开通的区域**无法取消**，且**免费额度只在主区域生效**，
  附加区域的资源按量计费（因此默认为「允许超额计费」）

---

## 故障排查

| 现象 | 处理 |
|------|------|
| 页面打不开 | `./scripts/install.sh status`；`docker compose logs api --tail=100` |
| 更新后版本不变 | `curl -s http://127.0.0.1:8000/api/health`；浏览器强刷；确认 `OCIBOT_HOST_REPO` 为绝对路径 |
| 容量重试不跑 | 侧栏 Worker 离线提示；检查 worker 容器日志 |
| 登录限流异常 | 直连部署保持 `OCIBOT_TRUST_PROXY=0`；反代需覆盖客户端 IP 头后再开启 |
| 私钥解密失败 | `OCIBOT_MASTER_KEY` 被更换；需用旧密钥或重新导入租户 / 恢复备份 |
| compose 报 `POSTGRES_PASSWORD 未设置` | `web/.env` 里写一个随机值，并确认项目根有 `.env` → `web/.env` 的软链（以前这里会静默回落到内置默认密码） |
| API 起不来，日志说 `insecure defaults` / `must be at least 24 characters` | `OCIBOT_MASTER_KEY` / `OCIBOT_JWT_SECRET` 还是空的或太短，用 `openssl rand -hex 48` 生成 |
| 面板里「一键更新」变灰 / 提示未启用 | 默认如此。`./scripts/install.sh update-on`，或用 SSH：`./scripts/install.sh update` |
| 升级后容器报 `Permission denied` 写文件 | `api` / `worker` 现在以 UID 10001、只读根文件系统运行。它们只应写 `/tmp`（已挂 tmpfs）；出现别处的写入说明有新代码在往镜像里写东西，应改成写数据库或 `/tmp` |

---

## 用量与合规

面板对云 API 的调用受以下硬性约束，这些不是可调的偏好，是为了不把你的账号
用成滥用。**改动前请先明白它们各自挡住了什么。**

| 约束 | 值 | 作用 |
|------|-----|------|
| 容量重试最小间隔 | 60 秒（默认 180） | 填 1 秒也会被抬到 60 |
| 单次任务尝试上限 | 2000 次（默认 200） | 不允许无限循环，填 0 会退回默认 |
| 429 退避 | 60 秒起指数增长，上限 900 秒 | 连续限流会越退越久，不会硬顶 |
| LaunchInstance 的 SDK 重试 | **已禁用** | 否则 SDK 重试会叠加在任务循环上，实际频率翻数倍 |
| 每租户活跃抢机任务 | 最多 1 个 | 建第二个直接拒绝；Worker 侧另有并发防护 |
| 单次尝试的调用数 | 4（3 次列举 + 1 次创建） | 额度快照在预检与守卫间复用，避免重复枚举 |

由此得出的最坏速率：**单租户约 240 次/小时**（间隔取下限），默认配置约 **80 次/小时**。

其余与合规相关的设计：

- **除容量重试外，没有任何后台调用**。所有读取都由你的点击触发；
  `OCIBOT_WORKER_BACKGROUND_OCI=0` 可把容量重试也关掉（见上文环境变量表）
- **Worker 不执行任何删除 / 终止操作**。销毁类操作只存在于你手动触发的路由里
- **默认强制 Always Free 上限**（`free_only_mode` 默认开），防止意外产生账单
- 全部调用走**官方 SDK**，不使用未公开接口，代码中没有任何绕过配额或限制的逻辑

> **需要你自己判断的部分**：本项目只约束"面板如何调用 API"，不能替你判断
> **账号本身**是否合规。云服务商的免费套餐条款通常对「同一人可持有的免费账号数量」
> 有限制，用多个免费账号叠加免费资源可能违反其条款——面板支持多租户是为了
> 管理你**有权使用**的账号，用途是否合规由你自行确认。

---

## 许可证与免责

仓库目前尚未附带开源许可证文件（`LICENSE`）。

本项目与 Oracle 公司无关。请遵守 Oracle Cloud 的服务条款与当地法规；容量重试、
自动操作等能力由使用者自行配置并承担风险。

---

## 链接

- 仓库：<https://github.com/674542449/ocibot>
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 安全审计摘记：[web/AUDIT.md](web/AUDIT.md)
