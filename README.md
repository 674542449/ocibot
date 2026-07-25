# OCI Bot

多租户 Oracle Cloud Infrastructure（甲骨文云）实例管理面板。

## 功能一览（v1.4）

- 可保存 / 管理 **多个 API 配置（租户）**，私钥本机加密存储，支持连接测试与跨租户聚合视图
- **加密备份 / 恢复**：将全部租户（含 API 私钥）导出为 **密码加密 ZIP**（WinZip AES-256），恢复时选择 ZIP 并输入密码
- **账号状态识别**：按**实际账单消费**判断 Always Free / PAYG（服务配额在免费账号上也可能非零，不作为付费依据），避免误判
- **账单查询**：按服务分类的账单明细、每日费用曲线、预算与超额告警；免费账号/无权限时给出清晰提示而非报错
- **密码到期提醒**：本地跟踪甲骨文登录密码有效期（默认 120 天，可自定义），临期/过期在租户列表与启动日志中高亮提醒
- 实例列表、详情、搜索、Compartment/子 Compartment 扫描和批量电源操作
- 创建向导仅显示官方 **Canonical Ubuntu** 镜像
- **自动网络**：账号没有 VCN/Subnet 时，创建实例前自动建好公网 VCN + Subnet（含 Internet Gateway / 默认路由 / 开放 Security List）；已有网络则直接默认选用。创建页不再显示 Compartment / VCN / Subnet（统一用默认）
- **快捷配置**：一键填入免费套餐（E2.1.Micro 50G / A1.Flex 4C24G 100G / A1.Flex 4C24G 200G，硬盘性能 120）
- Shape 选择标记 `VM.Standard.A1.Flex`（免费 ARM）和 `VM.Standard.E2.1.Micro`（免费 AMD）
- **修改 Flex 规格**：为已有实例调整 OCPU / 内存
- **串口 / VNC 控制台**：系统起不来（改坏 SSH / 防火墙）时的救援通道
- **实例监控**：CPU / 内存 / 网络入出流量曲线，支持 1–24 小时范围
- **公网 IPv6**：为已有实例的主 VNIC 分配公网 IPv6（需 IPv6-enabled Subnet）
- **成本与用量**：最近 7/30/90 天费用趋势、每日柱状图、预算与超额告警、免费额度追踪
- 可选择 **root + SSH 公钥** 或 **root + 服务器密码**；密码不保存到 `jobs.json`
- 默认分配公网 IPv4；IPv6-enabled Subnet 可选 IPv6
- 创建时可选择 Boot Volume 大小及 10–120 VPUs/GB 性能
- 创建时建立实例专用 NSG，并将 OCI IPv4/IPv6（可用时）入站、出站所有协议及 Ubuntu Guest 防火墙全部开放
- 实例详情支持管理所有关联 NSG：新增、删除、删除全部后全开放
- 支持分配/更换 **EPHEMERAL 临时公网 IPv4**，不会删除 Reserved IP
- **容量重试**（仅 root+密钥模式）：区域多可用域（AD）轮询；**合规限速**（默认间隔 180s、最小 60s、有限次数、遇 429 指数退避、同租户串行）
- 定时开关机和任务中心
- 重命名、导出 CSV、复制 root SSH / OCID / 公网 IP
- 导入 `~/.oci/config`、导入/导出 JSON，私钥本机加密存储

## Windows 便携版（推荐）

Windows x64 用户使用构建好的 `dist\OCIBot\` **整个文件夹**：

1. 双击 `OCIBot.exe`，不会显示命令行控制台；目标电脑不需要安装 Python。
2. 程序把配置和任务保存在 exe 同级 `data\` 中。复制到另一台 Windows x64 电脑时，必须复制整个 `OCIBot` 文件夹。
3. 便携版第一次启动且 `data\` 为空时，会从旧版 `%APPDATA%\ocibot` 复制 `tenants.json`、`jobs.json`、`.secret` 和可选 `.salt`；旧数据不会删除，已有便携数据也不会被覆盖。
4. 不要把程序放到 `Program Files` 等普通用户不可写目录；建议放在桌面、文档或其他自己的文件夹。

> **重要安全提示**：便携版 `data\.secret` 能解密同目录 `tenants.json` 内的 OCI API 私钥。复制完整目录等同于携带这些访问凭据，请勿上传到网盘公共链接或发给他人。跨电脑传输优先使用菜单中的 **密码加密 ZIP 备份/恢复**，并使用强密码。

### 构建 Windows 便携版

在 Windows x64、Python 3.13 环境运行：

```bat
build-windows.bat
```

脚本会创建独立 `.build-venv`，按 `requirements-windows-lock.txt` 安装已测试的运行版本和 `requirements-build.txt`，然后根据 `packaging\ocibot.spec` 生成 `dist\OCIBot\OCIBot.exe`。发行时只复制 `dist\OCIBot\`，不要复制开发用 `.venv`、`.claude`、缓存或任何真实 `data`/AppData 凭据。依赖版本会随安全更新调整，构建前应重新验证并更新锁定文件。

## 源码开发

- Python 3.10+
- Windows / macOS / Linux

```bash
cd ocibot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

Windows 开发者也可运行 `start.bat`（创建/检查开发环境后使用 `pythonw.exe` 启动），或在环境已就绪后双击 `start.vbs` 静默启动。它们不是跨电脑发行包。

## 添加租户（API 配置）

在左侧点击 **「+ 添加租户」**，填写：

| 字段 | 说明 |
|------|------|
| 显示名称 | 面板里显示的名字，如 `公司A-东京` |
| User OCID | `ocid1.user.oc1..xxx` |
| Tenancy OCID | `ocid1.tenancy.oc1..xxx` |
| Fingerprint | API Key 指纹 |
| Region | 如 `ap-tokyo-1` |
| 私钥 PEM | 创建 API Key 时下载的 `.pem` |
| Compartment | 可选；留空则使用 Tenancy 根 |

也可：

1. **导入 OCI Config**：读取本机 `~/.oci/config` + `key_file`
2. **导入配置**：导入之前导出的 JSON（支持多租户）

### 本地数据目录

| 内容 | Windows | macOS / Linux |
|------|---------|----------------|
| 租户配置 | 源码版：`%APPDATA%\ocibot\tenants.json`；便携版：`data\tenants.json` | `~/.config/ocibot/tenants.json` |
| 定时/容量重试任务 | 源码版：`%APPDATA%\ocibot\jobs.json`；便携版：`data\jobs.json` | `~/.config/ocibot/jobs.json` |
| 加密密钥 | 源码版：`%APPDATA%\ocibot\.secret`；便携版：`data\.secret` | `~/.config/ocibot/.secret` |

私钥使用本地随机密钥加密后写入，不会以 PEM 明文保存在 `tenants.json`。该密钥不是 Windows 账号或硬件绑定；`.secret` 与 `tenants.json` 同时被复制后可以在另一台电脑解密，这是便携迁移能够工作的原因，也是必须保护整个数据目录的原因。

> **加密备份**：`文件 → 备份 API Key（加密 ZIP）` 会把全部租户（含私钥）导出为 AES-256 加密 ZIP。密码由你设置，**丢失将无法恢复**。恢复用 `文件 → 从加密 ZIP 恢复`。该 ZIP 可用 7-Zip / WinRAR 等支持 WinZip AES 的工具打开。

## 常用操作

1. 左侧选择租户 → 点 **刷新实例**
2. 中间列表点击实例 → 右侧查看详情
3. 勾选多台 → 顶部 **批量** 栏执行开关机
4. 点 **创建实例** → 若账号尚无 VCN/Subnet 会自动创建默认公网网络；再选择官方 Ubuntu、带免费资格标记的 Shape、root 登录方式、IPv4/IPv6 与引导卷性能；密钥模式容量不足可加入重试
5. 点 **任务中心** → 管理定时开关机 / 容量重试任务
6. 危险操作（强制关机、终止）会二次确认

## 网页版（实验）

同仓库提供 **FastAPI + Vue 3 + PostgreSQL/SQLite** 网页版骨架，复用 `app/oci_client.py` 等业务层：

- 说明与启动：见 [`web/README.md`](web/README.md)
- 本地：`uvicorn web.backend.main:app` + `cd web/frontend && npm run dev`
- Docker：`web/docker-compose.yml`

桌面版与网页版可并存；Web 侧密钥存数据库并加密，任务由独立 worker 进程执行。

## 项目结构

```
ocibot/
├── main.py                 # 桌面启动入口
├── start.bat               # Windows 一键启动
├── requirements.txt
├── README.md
├── app/                    # 桌面 + Web 共用业务层
│   ├── config_store.py     # 多租户配置存取（加密）+ 加密 ZIP 备份/恢复
│   ├── oci_client.py        # OCI API 封装
│   ├── scheduler.py         # 定时任务 + 容量重试（合规限速）
│   ├── formatting.py
│   ├── gui.py / dialogs.py  # 桌面 UI
│   └── ...
└── web/                    # 网页版
    ├── backend/            # FastAPI + worker
    ├── frontend/           # Vue 3
    ├── docker-compose.yml
    └── README.md
```

## 权限提示

完整使用实例、NSG、VNIC、Private/Public IP 管理时，用户至少需要对对应 Compartment 具备类似权限：

```
Allow group <group> to manage instance-family in compartment <compute-compartment>
Allow group <group> to manage virtual-network-family in compartment <network-compartment>
Allow group <group> to read volumes in compartment <compute-compartment>
Allow group <group> to inspect compartments in tenancy
```

如果 Compute 与 VCN/Subnet 位于不同 Compartment，需要分别授权。只读场景可改为 `inspect` / `read`；实际最小权限请结合组织策略验证。

## 安全、网络与计费说明

- ⭐ 免费 ARM/AMD 是 **Always Free 候选资格标记**，并不保证当前地区有容量、账号有剩余额度或所选 OCPU/内存/Boot Volume 完全免费。
- root SSH 与公网全开放风险很高。创建时会同时关闭 Ubuntu UFW/放行 iptables，并在实例专用 NSG 中开放 IPv4 入站、出站所有协议；选择 IPv6 时也开放 `::/0`。
- 后续防火墙界面按用户要求可修改实例的 **所有关联 NSG**。共享 NSG 的修改会影响其他实例；Subnet Security List 不会被修改，最终有效权限是各层规则的并集。
- IPv6 选项仅代表从 IPv6-enabled Subnet 分配地址；公网连通还需要正确的 Route Table、Internet Gateway 等配置。
- “更换公网 IPv4”只自动处理 EPHEMERAL 地址。Reserved IP 会被拒绝；更换不是原子操作，旧地址释放后新地址分配可能失败，并会中断 SSH、影响 DNS 与白名单。
- Boot Volume 10 为平衡、20 为较高性能、30–120 为超高性能；高性能档可能额外计费。
- 密码模式不支持容量重试，以保证 root 密码不进入未加密的 `jobs.json`。密码会在本机哈希后作为首次启动 cloud-init 发送给 OCI。

## 容量重试与 API 合规

容量不足时的自动重试会调用 OCI `LaunchInstance`。为遵守 [OCI API 请求限流与退避建议](https://docs.oracle.com/iaas/Content/API/Concepts/usingapi.htm)，本工具做了如下约束：

| 项 | 值 |
|----|----|
| 最小间隔 | **60 秒**（低于此值会被抬高） |
| 默认间隔 | **180 秒** |
| 最大间隔 | 3600 秒 |
| 默认最大次数 | **200**（**禁止无限重试**；硬顶 2000） |
| 429 / TooManyRequests | 指数退避冷却（约 60s 起，上限约 15 分钟，含抖动） |
| 同租户并发 | 同一时间 **最多 1 个** 容量重试在飞 |
| 非容量错误 | 立即停止，不继续打 API |
| 普通 API（列表/详情等） | OCI SDK `DEFAULT_RETRY_STRATEGY`（短退避，最多约 8 次） |
| `LaunchInstance` | **关闭** SDK 自动重试，由上方应用层间隔/冷却单独控制，避免双重连打 |

请勿通过改配置绕过上述下限；高频循环创建更容易触发 `TooManyRequests`，并可能被租户侧风控关注。Always Free 容量不保证；合规重试只能降低限流风险，不能保证一定抢到主机。

## 免责声明

电源、创建、终止、全开放防火墙、IP 更换与定时任务会影响线上资源。请确认选对租户、实例和 NSG 后再执行。本工具不会上传你的 API 私钥。定时任务与容量重试仅在 **本机程序运行期间** 生效。请遵守 Oracle Cloud 服务条款与可接受使用政策；滥用 API（含过度自动化）可能导致限流或账号处置。
