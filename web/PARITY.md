# Feature inventory (formerly desktop-parity)

Last checked: 2026-09-26 (0.4.122). The desktop (Tkinter) version has been
**retired**; this table is simply the feature set the web app provides.

| Feature | Status | Where / notes |
|---------|--------|---------------|
| Multi-tenant API configs | ✅ | 租户页：粘贴 `~/.oci/config` + 选择私钥文件，或手动填写 |
| Encrypted private key storage | ✅ | 服务端 Fernet + `OCIBOT_MASTER_KEY`，私钥不回传浏览器 |
| Connection test | ✅ | 租户页「测试连接」，读不到时给出缺哪条 IAM 策略 |
| Account tier detect | ✅ | 租户页「等级查询」、账号用量页 |
| Default tenant | ✅ | 租户页「锁定默认」，按账号保存、跨设备生效 |
| Bulk delete / deletion protection | ✅ | 租户页多选删除；删除保护（受保护的排在最前） |
| Secondary regions (副区) | ✅ | 租户页「副区管理」：查看 / 开通并自动添加副区租户 |
| Region names | ✅ | 显示 Oracle 官方中文名（`app/formatting.py`，只收有出处的） |
| Console password expiry | ✅ | 租户页「密码到期查询」：读 `defaultPasswordPolicy`，仍有有效期时关闭强制改密 |
| Instance list / search / CSV | ✅ | 实例页（一次一个租户；跨租户聚合列表刻意不提供） |
| Power actions | ✅ | START / SOFTSTOP / SOFTRESET，列表支持批量 |
| Terminate / termination protection | ✅ | 详情页；保护标记存在 OCI 实例标签上 |
| Rename / change Flex shape | ✅ | 详情页；改规格前过 Always Free 额度守卫 |
| Create instance wizard | ✅ | Free-tier presets、auto network、root + 密钥 / 密码、cloud-init、批量创建 |
| Capacity retry (compliant) | ✅ | 任务中心；Worker 执行，最小间隔 60 s、429 指数退避、实时尝试日志 |
| Replace ephemeral public IP / reserved IP | ✅ | 列表 + 详情 |
| Assign / remove public IPv6 | ✅ | 详情页（单个地址） |
| Firewall | ✅ | 详情 → 防火墙：NSG 与子网安全列表规则、Cloudflare 网段、一键修复 / 收紧 |
| Boot volume resize / VPU / backups | ✅ | 详情页；可选 SSH 自动扩展文件系统 |
| Metrics charts / boot log | ✅ | 详情 → 监控、引导日志 |
| Console serial/VNC | ✅ | 详情 → 控制台 |
| WebSSH browser terminal | ✅ | 详情 → WebSSH（主机密钥按实例 ID 校验，凭据不落库） |
| Block / object storage | ✅ | 存储页 |
| Usage / invoices / Always Free dashboard | ✅ | 账号用量页 |
| Notifications | ✅ | 设置页：Telegram / Bark / ServerChan / Webhook / SMTP（容量重试结果） |
| Audit log | ✅ | 审计日志页 |
| Encrypted ZIP backup/restore | ✅ | 备份恢复页 |
| Self-update | ✅ | 用户管理 / 更新页（默认关闭，`install.sh update-on` 打开） |

Removed on purpose (do not re-add without reading the CHANGELOG entry):
scheduled power jobs, budget alerts and the daily egress check (0.4.36),
the local password-expiry reminder, and IPv6 CIDR ranges (0.4.100 / 0.4.118).

**Must run for full use:** `api` + `worker` (the API also serves the built frontend).

## Pages

| Path | Purpose |
|------|---------|
| `/login` | 登录 / 注册 |
| `/` | 实例列表 |
| `/instances/:tenantId/:instanceId` | 实例详情（监控 / 控制台 / WebSSH / 防火墙 / 引导卷） |
| `/launch` | 创建实例 |
| `/jobs` | 任务中心（容量重试） |
| `/storage` | 存储（引导卷 / 块卷 / 对象存储）；`/boot-volumes` 重定向至此 |
| `/tenants` | 租户 / API 配置 |
| `/account` | 账号用量 / Always Free 仪表盘 / 账单 |
| `/backup` | 备份恢复 |
| `/audit` | 审计日志 |
| `/settings` | 设置（通知、两步验证、修改密码） |
| `/admin` | 用户管理 / 系统更新（管理员） |
