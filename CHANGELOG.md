# Changelog

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
