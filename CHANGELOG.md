# Changelog

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
