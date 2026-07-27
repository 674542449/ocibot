# Changelog

## 0.4.17 — 2026-07-28

### 修复
- **开通副区报 `[404] EntityNotFound`**：区域代码被转成了小写再提交。
  `CreateRegionSubscription` 是**按这个 key 定位区域**的，Oracle 返回的 key 是大写
  （`NRT` / `KIX` / `FRA`），小写的 `kix` 不是任何实体，于是报 404 ——
  而 404 看起来又很像权限问题，很容易查错方向。
  现在区域代码原样回传给 Oracle，输入时仍可用区域名或 key、不区分大小写
- **开通失败时看不出是哪一步失败**：开通流程要依次调用「读已开通区域 → 读区域清单 →
  提交开通」三个接口，三者都可能返回 404，但之前的错误消息完全一样。
  现在每种失败都会标明所处步骤
- **404 / 401 补充权限提示**：该接口需要 API 用户具备租户级权限
  （Administrators 组或 `manage tenancies` 策略），且账号必须已升级为 PAYG ——
  纯 Always Free 账号无法订阅新区域。OCI 对无权限也返回 404，因此这条提示直接附在消息里

### 维护
- `tests/test_secondary_region.py` 增加 8 项：区域代码原样提交（回归）、
  区域名 / key 大小写混用均可、已开通时不再重复提交、三个步骤各自的错误归属、
  404 权限提示

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 应为 0.4.17
```

---

## 0.4.16 — 2026-07-28

### 功能
- **租户开通副区（其他国家 / 地区）**：租户页新增「副区管理」，可查看该 Oracle 账号
  已开通的区域与主区，并从完整区域清单中开通新的副区
  （`GET /tenants/{id}/regions`、`POST /tenants/{id}/regions/subscribe`）。
  已在 Oracle 控制台开通过的区域也可以一键「添加到面板」，两种情况走同一个接口
- **创建实例时可选择副区**：创建页新增「区域（主区 / 副区）」下拉，
  镜像 / Shape / 网络元数据、额度预检、抢机重试都会按所选区域走
- **副区以「同凭据的子租户」建模**：开通后自动添加一个与主租户同 API 凭据、
  区域不同的租户行（`parent_tenant_id` 指向主租户）。OCI 会话本身只绑定一个区域，
  这样实例列表、存储、WebSSH、定时任务、抢机任务等每个按租户组织的页面
  都能直接管理副区，无需为每个接口再加一个 region 参数。
  删除主租户会一并删除它的副区行；备份 / 恢复会重新映射父子关系（ID 会重新生成）

### 修复 / 防呆
- **副区不适用 Always Free，守卫按区域区分**（重要）：免费额度只存在于主区，
  而用量快照是**按区域**统计的 —— 在一个全新的副区里读到的用量是 0，
  看起来像「还有 4 OCPU / 24 GB 免费额度」，据此放行会直接产生账单。
  现在创建 / 抢机 / 额度预检在副区一律改走区域闸门（`enforce_secondary_region`）：
  - 租户勾选着「仅使用免费额度」→ 直接拒绝，并说明去哪里取消勾选
  - 取消勾选（明确接受计费）→ 放行，并在结果消息里标注「按量计费」
  - 副区租户创建时默认就是「允许计费」，创建页与额度面板也不再显示会被误读为
    「有免费余量」的进度条
- 区域探测**故意保守**：只有当前区域与主区都能解析成合法的 OCI 区域 ID 时才判定为副区，
  读取失败一律按主区处理，避免探测异常把所有创建都卡死

### 维护
- 新增 `tests/test_secondary_region.py`（12 项）与 `tests/test_backup_region_link.py`
- 全接口冒烟测试覆盖新增的两个副区接口

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 应为 0.4.16
```

---

## 0.4.15 — 2026-07-27

### 功能
- **创建实例页显示账号已用免费额度**：A1 OCPU、A1 内存、E2.1.Micro 台数、块存储（含引导卷）
  各自的「已用 / 上限 / 剩余」与进度条。选择租户即读取，可手动刷新；用量读取不完整时明确警告
- **提交前拦截超额配置**：新增 `POST /tenants/{id}/launch-quota-check` 对当前表单预演。
  它调用的是服务端真正拦截时用的同一个 `check_launch_quota`，而不是在前端重写额度算法，
  因此预检结论不会与服务端判断漂移。超额时不打开确认框并给出具体原因
  （如「A1 额度不足：需要 3 OCPU / 12 GB，剩余 2 OCPU / 12 GB」）。
  预检请求本身失败时**不阻止**创建，服务端仍会再校验一次

### 修复
- **额度校验对已升级账号形同虚设**（重要）：Oracle 账号只要升级过就会被识别为
  `account_tier="paid"`，而守卫据此把超额从「拦截」降级为「警告」。实测：已用 50GB
  引导卷的账号再创建 A1 4C/24G + **200GB** 引导卷（合计 250GB > 200GB）竟然通过校验。
  改为**按租户显式开关** `free_only_mode`（默认开启），不再从账号等级推测意图：
  - 默认：无论账号是否付费，超出 Always Free 一律拦截
  - 确实要用付费资源时，在「租户」页取消勾选「仅使用免费额度」
  - 升级已有部署时该列默认为开启，即既有租户自动受保护
  - 创建页会显示当前租户处于「仅免费额度」还是「允许超额计费」
- **实例防火墙不显示规则**：`get_instance_firewall` 只读 VNIC 的 `nsg_ids`，但多数实例
  没有 NSG —— OCI 默认网络把规则放在**子网安全列表**里。现在同时读取并以只读方式展示
  （面板的添加规则 / 一键全开放只作用于 NSG），并补上了缺失的空状态提示
- **引导卷扩容 502**：上一轮改动漏了 `quota_guard` 导入，每次请求都失败，
  而宽泛的 `except → 502` 把它伪装成 OCI 报错。新增全接口冒烟测试防止此类问题再隐形
- **块存储永远显示"未挂载"、卸载按钮永不出现**：`list_volume_attachments` 的 SDK 签名
  只接受一个位置参数，代码多传了一个导致 `TypeError` 被吞掉；同时 `delete_block_volume`
  的"仍挂载中"保护也一直静默失效
- **管理员可能把自己永久锁死**：「重置密码」对自己可点，会吊销会话、清除 2FA，
  然后立即跳转登录页销毁唯一一份新密码。现已对自己隐藏，服务端也拒绝
- **实例列表刷新按钮永久失效**：请求在途时切换租户会让 `loading` 永远为 true
- **创建实例可能提交错的镜像**：切换操作系统后镜像被"仅 Ubuntu"的列表覆盖，
  规格回落到 x86 固定型号，可能把 ARM 镜像提交到 x86 shape 上
- 拒绝的开关机 / 重命名 / 终止被显示为成功（终止还会跳转）；取消保留 IP 名称输入仍会创建；
  NSG 端口留空报英文 422；清空月度预算导致整个租户编辑失败；存储页把租户级失败显示成"暂无卷"；
  WebSSH 远端退出后终端看似仍连接；删除控制台连接失败也报成功；
  已达上限的抢机任务点「继续」是空操作；定时任务执行失败既无历史也无告警；
  `24:00` 这类时间可保存但永不触发；备份漏了月度预算；取消全部推送事件反而恢复全选
- 修复 zip 炸弹绕过（伪造 `file_size` 可使 398KB 上传产生 830MB 分配）、
  额度 fail-closed 在 Worker 侧被第二次读取绕过、自更新误报成功等 32 项核查结论

### 维护
- 删除孤儿组件 `views/BootVolumesView.vue`（无路由、无引用）
- 新增 `tests/test_version_bump.py`：`app_version` 必须与 CHANGELOG 顶部版本一致，
  忘记递增会导致测试失败
- 测试总数 153 → 302

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 应为 0.4.15
```

---

## 0.4.14 — 2026-07-26

### 安全（重要）
- **修复登录限流可被绕过**：`run.py` 之前让 uvicorn 无条件信任所有来源的
  `X-Forwarded-For`（`forwarded_allow_ips="*"`），它会直接改写 `request.client.host`，
  而这正是登录限流的计数键 —— 攻击者每次请求伪造一个 IP 即可无限暴力破解密码。
  现在只有 `OCIBOT_TRUST_PROXY=1` 时才信任代理头，且仅信任
  `OCIBOT_FORWARDED_ALLOW_IPS`（默认回环地址）
- **WebSSH 增加 Origin 校验**：WebSocket 不受 CORS 保护，`SameSite=none` 配置下
  任意网站都可借受害者 Cookie 打开终端（CSWSH）；跨站握手现在在 accept 前直接拒绝
- **忽略 `OCIBOT_CORS_ORIGINS=*`**：通配符 + Cookie 凭据会让 Starlette 回显来源域，
  等于任意站点可以以登录用户身份读取 API；启动时会明确告警
- **上传不再整体读入内存**：备份导入 / 对象上传改为分块限量读取，两个接口从
  `async def` 改为同步（线程池）执行，避免阻塞事件循环；新增 32MB 请求体上限
- **补齐 SSRF 地址过滤**：新增 `0.0.0.0/8`、`100.64.0.0/10`、`192.0.0.0/24`、
  `240.0.0.0/4` 等段，并解开 NAT64 `64:ff9b::/96` 与 6to4 `2002::/16` 中内嵌的 IPv4
  （此前 `64:ff9b::a9fe:a9fe` 可绕过限制访问云元数据）
- 新增 HSTS / COOP / CORP 响应头（HSTS 仅在 `OCIBOT_COOKIE_SECURE=1` 时下发）
- **面板内一键更新改为默认关闭**：它会驱动挂载的 `docker.sock` 并可能进入宿主机
  命名空间，管理员失陷≈宿主机 root。`scripts/install.sh` 仍显式置 1，推荐安装方式不受影响
- 弱 / 短 `OCIBOT_MASTER_KEY` 启动时给出具体告警（密钥派生方式未改动，改了会导致
  已存私钥无法解密）
- compose 增加 `no-new-privileges`，新增 `OCIBOT_BIND` 可只监听 127.0.0.1

### 修复
- **容量重试的降级配置会被丢弃**：`POST /jobs/capacity` 校验了 `fallback_configs`
  却没写入任务行，Worker 因此只会尝试主配置；现已持久化并与创建向导共用校验
- **定时任务可能重复执行**：「今天已跑」标记只 `flush()` 未提交，两个 Worker 在同一
  分钟会各发一次开关机；改为提交后再执行（SQLite 上旧代码则会因锁冲突静默跳过）
- 创建实例时额度校验失败会抛未处理的 500，现返回 502 并带原因
- `prepare_launch_network()` 用 `auth_mode` 推断重试模式，导致普通创建被按重试规则校验
- 备份导入遇到超范围 `password_expiry_days` 会 500，现做截断
- 登录后跳转只接受站内路径
- `AdminView.vue` 日志翻译表类型错误（vue-tsc 报错，vite 不做类型检查所以此前未暴露）

### 第二轮（并行审计复查，13 个 agent）
上一轮的修复被独立复查，发现三处真实缺陷并已修正：

- **请求体上限可被 `chunked` 绕过**：只检查 `Content-Length` 是不够的，40MB 会被完整落盘；
  而且 FastAPI 在依赖注入**之前**解析 multipart，所以未认证即可触发，同一手法还能让单个
  请求驻留约 1GB 表单字段。改为按**实际收到的字节**计数的纯 ASGI 中间件
- **`_client_ip` 取 XFF 最左项**：nginx 标准的 `$proxy_add_x_forwarded_for` 是追加而非替换，
  最左项仍由客户端控制，`TRUST_PROXY=1` 时限流照样能绕；现统一使用 `request.client.host`
- **IDNA 编码器不一致导致 SSRF**：`getaddrinfo` 用 IDNA2003、httpx 用 IDNA2008，
  `evilß.example.com` 校验的是 `evilss.example.com` 却连向 `xn--evil-yna.example.com`

同时修复：
- **`POSTGRES_PASSWORD` 一直是默认值**：compose 的 `${VAR}` 插值只读项目根目录的 `.env`，
  不读 `env_file`，所以 `install.sh` 生成的随机密码从未生效。现在会自动建立
  `.env → web/.env` 链接，并在更新时幂等地 `ALTER USER` 对齐（先起 db、改密码、再起 api）
- **`docker-compose.yml` 的 `environment:` 覆盖 `env_file:`**：`OCIBOT_REQUIRE_SECURE_SECRETS` 等
  5 个键重复声明，导致 `web/.env` 里的值被静默丢弃，生产密钥守卫从未生效
- 粘贴 OCI 配置的正则回溯（ReDoS）：108KB 占用 2.3s GIL，现 0.26ms（约 8700 倍）
- `account_tier` 只要不是已知值就**关闭免费额度硬上限**，已反转为仅 `paid` 放开
- 恢复已停止的容量重试任务会绕过"每租户仅一个"限制，导致两个任务竞争 LaunchInstance
- 解密后的 OCI 私钥曾写入系统临时目录且不清理，改为内存内 `key_content`
- 创建实例后 800ms 自动跳转会**销毁一次性 root 密码**，改为需手动确认已保存
- `list_images` 算出 Ubuntu 过滤结果却丢弃、`list_objects` 缺 `fields` 导致用量恒为 0、
  换公网 IP 在解绑超时后误报成功并返回旧地址
- SSH 公钥与启动脚本只挡 `\n`，`\r`/`\x85`/U+2028/U+2029 也是 YAML 换行，可注入 cloud-init
- 限流字典无上界、`LoginRequest.username` 无长度限制
- `install.sh update` 的 `git clean -fd` 会删除未跟踪的运维文件（需 `OCIBOT_CLEAN_UNTRACKED=1` 才执行）
- 自更新用 `docker run -d` 的退出码判断成功（它只代表容器启动了）

### 第三轮（补齐此前搁置的三项）

- **WebSSH / 引导卷扩容现在校验 SSH 主机密钥**（此前 `known_hosts=None`，完全不校验，
  任何能在该地址应答的一方都能冒充实例并拿到你输入的 SSH 凭据）
  - 首次连接记录指纹（TOFU），之后不符即**在发送任何凭据之前**中止
  - 用 `asyncssh.get_server_host_key()` 只做密钥交换，不认证 —— 这是安全性的关键：
    若在 `connect()` 之后才检查，凭据已经交给冒充方了
  - 指纹按**实例 OCID** 记录而非 IP，所以换公网 IP / 停开机不会误报
    （这正是当初跳过校验的理由）
  - 重装系统后指纹会合法变化：实例详情页提供「重置主机密钥」，并有
    `GET/DELETE /api/tenants/{tid}/instances/{iid}/host-key`
- **额度读取不完整时不再 fail-open**：此前每个子查询各自 try/except，限流或报错会得到
  一份"用量为 0"的快照，校验器读成"额度全空"从而放行，可能产生真实费用。
  现在 `get_free_quota_usage` 会标记 `read_incomplete`（含 `list_instances_tree` 的
  分区间部分失败、以及卷列表的 `errors`），API 侧返回 503 拒绝，
  Worker 侧**推迟本次尝试且不消耗次数**（不会因为一次抖动就杀掉长跑的抢机任务）。
  付费账号不受影响 —— 它本就不受硬上限约束
  - 仅根据"是否有 notes"判断是不行的：对象存储的近似统计在**成功时**也会写 notes
- **自更新并发锁不再只在单进程内有效**：`threading.Lock` 挡不住 `OCIBOT_API_WORKERS=2`
  的另一个进程。改为在写入 `running` 的同一事务里用 `SELECT ... FOR UPDATE` 复查状态行
  （SQLite 上是空操作，但那种部署本就是单进程），并把网络请求移出临界区

### 测试
- 新增 `tests/test_web_hardening.py`、`test_upload_limits.py`、
  `test_schedule_single_fire.py`、`test_capacity_job_create.py`、`test_audit_pass2.py`、
  `test_ssh_hostkey.py`、`test_quota_fail_closed.py`，并扩充 `test_url_safety.py`
- 共 258 passed / 1 skipped（原 153）

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.14
```

---

## 0.4.13 — 2026-07-26

### 行为 · 减少 Oracle API 调用
- **进入面板不再自动打 OCI**：实例 / 创建 / 账号用量 / 存储 / 引导卷等页仅在用户点击「刷新 / 加载」时请求
- 实例列表默认不解析 IP；切换租户或解析 IP 开关不自动重拉
- 租户导入默认关闭「保存后测试连接」
- 禁用 `GET /instances` 全租户聚合接口，强制单租户查询
- Worker 每日预算检查按租户串行并间隔，避免多账号同时打 Usage API
- 实例详情进入时不再预取引导卷 / 默认强拉监控（按当前页签按需加载）

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.13
```

---

## 0.4.12 — 2026-07-26

### 体验
- 系统更新「更新日志」以中文摘要展示（git/docker/compose 常见输出翻译）
- 新增站点 Logo / Favicon（侧栏、登录页、浏览器标签）

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.12
```

---

## 0.4.11 — 2026-07-26

### 修复
- 实例列表 / 详情不再展示冗长 OCID（仍可通过路由与复制接口使用）
- **网络监控数据异常**：`NetworksBytesIn/Out` 改为 MQL `.rate()`（B/s），多 VNIC 同秒求和；前端按 B/s·KB/s·MB/s 显示

### 升级
```bash
cd ~/ocibot && bash scripts/install.sh update
curl -s http://127.0.0.1:8000/api/health   # 0.4.11
```

---

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
