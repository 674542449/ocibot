# Feature inventory (formerly desktop-parity)

Last checked: 2026-07-25. The desktop (Tkinter) version has been **retired**;
this table is now simply the feature set the web app provides — every capability
the old desktop panel had is available in the web UI.

| Feature | Web status | Notes |
|-----------------|------------|-------|
| Multi-tenant API configs | ✅ | Paste config + PEM file picker |
| Encrypted private key storage | ✅ | Server Fernet + OCIBOT_MASTER_KEY |
| Connection test | ✅ | |
| Account tier detect | ✅ | 账号用量页 + 租户「识别等级」 |
| Password expiry reminder | ✅ | 租户列表徽章 + 编辑字段 |
| Instance list / search | ✅ | 列表搜索过滤 |
| Export CSV | ✅ | 实例页导出 |
| Power actions | ✅ | START / SOFTSTOP / SOFTRESET / … |
| Terminate | ✅ | |
| Rename | ✅ | |
| Create instance wizard | ✅ | Free-tier presets, auto network；提交前 Always Free 额度硬拦 |
| Capacity retry (compliant) | ✅ | API + worker；入队与每次尝试前额度守卫 |
| Schedule power jobs | ✅ | 任务中心 |
| Replace ephemeral public IP | ✅ | 列表 + 详情 |
| Assign public IPv6 | ✅ | 详情页 |
| Update Flex shape | ✅ | 详情 → 引导卷/规格；改规格前额度守卫 |
| Boot volume resize / VPU | ✅ | 详情；可选 SSH 自动扩展文件系统 |
| Metrics charts | ✅ | 详情 → 监控 sparkline |
| Account / limits dashboard | ✅ | 账号用量页（Always Free 仪表盘：计算/块/对象/公网 IP） |
| Console serial/VNC | ✅ | 详情 → 控制台 |
| WebSSH browser terminal | ✅ | 详情 → WebSSH（session 凭证，不落库） |
| Block volume management | ✅ | 存储页 · 块卷 |
| Object storage management | ✅ | 存储页 · 对象存储 |
| NSG firewall manager | ✅ | 详情 → 防火墙（规则增删 / 全开放） |
| Encrypted ZIP backup/restore | ✅ | 备份恢复页 |
| Cross-tenant aggregate view | ✅ | |

**Must run for full use:** `api` + `worker` + `frontend`.

## Pages

| Path | Purpose |
|------|---------|
| `/` | 实例列表 |
| `/instances/:tenantId/:instanceId` | 实例详情（监控/控制台/WebSSH/防火墙/引导卷） |
| `/launch` | 创建实例 |
| `/tenants` | 租户 |
| `/jobs` | 任务中心 |
| `/account` | 账号用量 / Always Free 仪表盘 |
| `/storage` | 存储（引导卷 / 块卷 / 对象存储）；`/boot-volumes` 重定向至此 |
| `/backup` | 备份恢复 |
