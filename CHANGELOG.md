# Changelog

## 0.4.10 — 2026-07-26

### 修复
- 实例列表首屏不再双重拉取 OCI（bootstrap 门闩）
- 创建实例切换租户时重置/校验 AD，并丢弃过期 launch-meta 响应
- SMTP 发送时再次校验主机（防 DNS 重绑定 SSRF）

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.10
```

---

## 0.4.9 — 2026-07-26

### 修复
- **密码到期策略真正可写库并即时回显**：编辑租户可改天数（0=关闭）、快捷 90/120；「仅保存密码策略 / 保存全部 / 已改密」写入后列表徽章立刻更新；普通保存路径同步钳制天数
- **实例列表默认不再聚合全部租户**：默认选中第一个租户；下拉去掉「全部租户（聚合）」
- 切换「解析 IP」会重新拉取实例
- 监控曲线对非法采样值做防护，避免 `toFixed` 异常
- 正式版 README

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.9
```

---

## 0.4.8 — 2026-07-26

### UI · 玻璃质感 + 实例操作 / 监控
- 全局卡片、侧栏、表格、按钮改为毛玻璃（半透明 + blur + 高光边）
- 实例列表操作按钮收进胶囊按钮组，尺寸与风格统一
- 实例监控：鼠标/触控移到曲线上显示该时刻具体数值与时间，右上角实时读数

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.8
```

---

## 0.4.7 — 2026-07-26

### UI
- 全局修正 checkbox / radio 与文字不在同一水平线的问题（`.choice` 布局）
- 「开放 Guest 防火墙」改为通俗文案：**允许外网直接访问（放宽防火墙）**，并附说明
- 租户密码策略文案：明确 **0=关闭提醒**，可自行取消 120 天；保存写入数据库

### 在线更新
- 面板更新改为：**定位 `OCIBOT_HOST_REPO` → 执行与 SSH 相同的 `bash scripts/install.sh update`**
- `install.sh` 支持 `OCIBOT_SKIP_GIT=1`（代码已由面板拉取时跳过二次 git）
- 若宿主机 nsenter 路径不可用，回退到仓库内 `docker compose build && up -d`

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.7
```

---

## 0.4.6 — 2026-07-26

### 安全加固
- **Webhook / Bark SSRF**：校验 URL 协议与主机，解析 DNS 后拒绝私网/回环/链路本地/云元数据；禁止跟随重定向与信任环境代理
- **SMTP 主机**：同样拒绝内网目标
- **登录限流**：默认不信任 `X-Forwarded-For`（需 `OCIBOT_TRUST_PROXY=1` 且前置代理覆盖该头）
- **登录时序**：用户名不存在时仍走 bcrypt，降低枚举差异
- **WebSSH**：移除 query-string JWT（防日志/Referer 泄露），仅 cookie / Authorization Bearer
- **备份导入**：限制租户数量与解压后大小，忽略归档内 `owner_id`
- **HTTP 安全头**：`CSP` / `X-Frame-Options` / `nosniff` / `Referrer-Policy`
- **自更新 GitHub 请求**：仓库名/分支名白名单；`trust_env=False`

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.6
```

---

## 0.4.5 — 2026-07-26

### 修复 · 在线更新后服务挂掉 / 更新未完成
- **根因**：重启 API 时可能注入空的 `POSTGRES_PASSWORD`、缺少 `web/.env`，以及在 API 进程内同步 recreate 时被自己杀掉，栈停在半死状态
- compose 一律带 `--env-file web/.env`；不再传入空密钥环境变量
- **构建完成后**由独立 `docker:cli` 容器跑重启脚本：`worker → api`，失败则 `compose up -d` 全量拉起
- 状态机：中断遗留的 `running` 会在超时后自动标为 error，避免「更新中」卡死无法重试
- 后台重启失败时同步走 `compose up -d` 恢复路径，并给出 SSH 修复提示

### 服务器已挂时（先恢复）
```bash
cd /root/ocibot || cd ~/ocibot
cp -a web/.env /tmp/ocibot.env.bak 2>/dev/null || true
export OCIBOT_HOST_REPO="$(pwd -P)"
# 拉最新修复（若 git 可用）
git fetch origin main && git reset --hard origin/main
cp -a /tmp/ocibot.env.bak web/.env 2>/dev/null || true
bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 期望 0.4.5
```

之后面板「一键更新」应可在重启后自动回血。

---

## 0.4.4 — 2026-07-26

### 修复 · install.sh `cd: /root/ocibot\\n/root/ocibot`
- 根因：`export_build_env` 写成 `cd && pwd -P || cd && pwd`，shell 优先级导致 **pwd 执行两次**，`REPO_DIR` 变成带换行的双路径
- 现改为分步解析 `pwd -P`，失败再回退 `pwd`，并去掉 CR/LF
- `compose()` 增加目录有效性检查

### 服务器（你现在卡住时直接跑）
```bash
cd /root/ocibot
cp -a web/.env /tmp/ocibot.env.bak
# 先手动拉脚本修复（若 update 仍因旧脚本失败）：
curl -fsSL https://raw.githubusercontent.com/674542449/ocibot/main/scripts/install.sh -o scripts/install.sh
chmod +x scripts/install.sh
cp -a /tmp/ocibot.env.bak web/.env
export OCIBOT_HOST_REPO=/root/ocibot
bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.4
```

---

## 0.4.3 — 2026-07-26

### 修复 · 在线更新 build 失败（加强）
- **正确解析宿主机仓库绝对路径**（`/proc/self/mountinfo` + `OCIBOT_HOST_REPO`），避免 bind 到错误目录
- compose 通过 `docker run docker:27-cli` 执行（自带 compose 插件）
- 构建前检查磁盘空间、预拉 cli 镜像；失败时给出可读中文原因
- 从 `web/.env` 注入 `POSTGRES_PASSWORD` 等变量，避免 compose 插值缺参
- install.sh 导出**绝对路径** `OCIBOT_HOST_REPO`，并预拉 `docker:27-cli`
- compose 默认挂载改为 `${OCIBOT_HOST_REPO:-/root/ocibot}`（不再用相对 `.`）

### 服务器请先命令行升到本版
```bash
cd ~/ocibot
cp -a web/.env /tmp/ocibot.env.bak
git fetch origin main && git reset --hard origin/main
cp -a /tmp/ocibot.env.bak web/.env
# 确认绝对路径
export OCIBOT_HOST_REPO="$(pwd -P)"
bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.3
```

若仍失败，把管理页「更新日志」全文发出来。

---

## 0.4.2 — 2026-07-26

### 变更
- **关闭**实例详情「制作镜像」入口
- 后端 `POST .../create-image` 返回 403，提示改用引导卷备份
- 引导卷备份说明文案同步更新

---

## 0.4.1 — 2026-07-26

### 修复 · 在线更新 `docker compose build` 失败
- 根因：API 镜像内的静态 `docker` **没有 compose 插件**，`docker compose build` 必然失败
- 现改为通过 `docker run --rm docker:27-cli` 执行 compose（官方镜像自带插件）
- 构建时注入宿主机路径 `OCIBOT_HOST_REPO`，正确挂载代码目录
- **先重启 worker，再分离重启 api**，避免更新进程把自己杀掉导致中断
- 能力检测增加 `docker_daemon`；失败日志保留更多尾部输出

### 升级（服务器）
```bash
cd ~/ocibot
# 先命令行升到本修复（旧版网页更新仍会失败）
git fetch origin main && git reset --hard origin/main
# 保留密钥
# （若 reset 掉了 .env，从备份拷回 web/.env）
bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # version 0.4.1
```
之后即可在「用户管理 → 系统更新」使用一键更新。

---

## 0.4.0 — 2026-07-26

### UI · 字节系控制台风格
- **默认浅色**主题（飞书 / 火山引擎风格）：灰底 `#f2f3f5`、白卡片、主色 `#3370ff`
- 侧栏分区导航（工作台 / 资源 / 系统）、轻量图标、选中浅蓝底
- 按钮、输入框、表格、徽章、Tab、Toast 统一圆角与阴影
- 登录页品牌区 + 分段切换；暗色模式仍可选，色板同步调整
- 版本号 **0.4.0**

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # version 0.4.0
# 浏览器 Ctrl+F5；若仍是旧暗色可清 localStorage 的 ocibot_theme
```

---

## 0.3.0 — 2026-07-26

### 手机端适配（本次）
- **侧栏抽屉导航**：窄屏顶部栏 + ☰ 菜单，避免侧栏占满整屏
- **页面工具栏**：`.page-head` / `.page-tools` 在手机上纵向堆叠，控件全宽
- **表格**：横向滚动提示，减小强制最小宽度，操作按钮可换行
- **标签页**：实例详情 / 存储等 tab 可左右滑动
- **表单**：手机输入框 16px，减轻 iOS 聚焦放大
- **安全区**：支持刘海屏 `safe-area-inset`
- **WebSSH**：终端高度随视口自适应
- **登录页**：小屏字号与 padding 优化

### 如何验证版本
```bash
curl -s http://127.0.0.1:8000/api/health
# "version": "0.3.0"
```
侧栏底部「构建 xxxxxxx」；管理员页「系统更新」。

---

## 0.2.0 — 2026-07-26

### 功能
- 管理员**在线检查 / 一键更新**（GitHub + docker compose）
- 更新脚本对分叉仓库 **hard reset 到 origin**（保留 `web/.env`）
- 侧栏显示构建 git sha；健康检查返回 `app_version` / `git_sha`
- 系统更新入口置顶，导航改名「用户管理 / 更新」

### 部署
- API 镜像含 `git` + 多架构 `docker` CLI
- compose 挂载宿主机仓库与 `docker.sock`（`OCIBOT_UPDATE_ENABLED=1`）

---

## 0.1.x — 此前累计

- Web 化（Vue3 + Vite + TS + FastAPI），移除桌面 Tk 版
- Always Free 仪表盘、创建/改规格/扩容**额度守卫**
- WebSSH、引导卷 SSH 扩 FS、块/对象存储
- 复制改用 toast + HTTP 剪贴板回退
- Docker Compose + **PostgreSQL** 一键安装/更新脚本
- 公网 IP 单击复制修复

---

升级：

```bash
cd ~/ocibot
bash scripts/install.sh update
# 浏览器 Ctrl+F5
```
