# Web vs Desktop feature parity

Last checked: 2026-07-25

| Desktop feature | Web status | Notes |
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
| Create instance wizard | ✅ | Free-tier presets, auto network |
| Capacity retry (compliant) | ✅ | API + worker |
| Schedule power jobs | ✅ | 任务中心 |
| Replace ephemeral public IP | ✅ | 列表 + 详情 |
| Assign public IPv6 | ✅ | 详情页 |
| Update Flex shape | ✅ | 详情 → 引导卷/规格 |
| Boot volume resize / VPU | ✅ | 详情 |
| Metrics charts | ✅ | 详情 → 监控 sparkline |
| Account / limits dashboard | ✅ | 账号用量页 |
| Console serial/VNC | ✅ | 详情 → 控制台 |
| NSG firewall manager | ✅ | 详情 → 防火墙（规则增删 / 全开放） |
| Encrypted ZIP backup/restore | ✅ | 备份恢复页 |
| Cross-tenant aggregate view | ✅ | |

**Must run for full use:** `api` + `worker` + `frontend`.

## Pages

| Path | Purpose |
|------|---------|
| `/` | 实例列表 |
| `/instances/:tenantId/:instanceId` | 实例详情（监控/控制台/防火墙/引导卷） |
| `/launch` | 创建实例 |
| `/tenants` | 租户 |
| `/jobs` | 任务中心 |
| `/account` | 账号用量 |
| `/backup` | 备份恢复 |
