# 换 IP API 开发文档

给脚本调用的接口：用 **API 密钥**更换实例「多出口 IP」的公网 IP。可以只换某一个私网 IP 的公网 IP，
也可以一键换全部。私网 IP、主公网 IP 都不变。

适用版本：0.4.176 及以后。

不想写脚本的话，仓库里有一个现成的本地图形界面工具：[tools/ip-api-gui](../tools/ip-api-gui/README.md)
（Windows 双击 `启动.bat` 即可）。

---

## 目录

- [能做什么、不能做什么](#能做什么不能做什么)
- [准备工作](#准备工作)
- [鉴权](#鉴权)
- [接口一览](#接口一览)
- [接口详情](#接口详情)
  - [列出多出口 IP](#1-列出多出口-ip)
  - [换一个私网 IP 的公网 IP](#2-换一个私网-ip-的公网-ip)
  - [一键换全部](#3-一键换全部)
  - [查进度 / 结果](#4-查进度--结果)
- [任务对象（job）](#任务对象job)
- [错误码](#错误码)
- [更换是怎么进行的](#更换是怎么进行的)
- [示例](#示例)
- [安全建议](#安全建议)
- [常见问题](#常见问题)

---

## 能做什么、不能做什么

| 能 | 不能 |
| --- | --- |
| 列出一台实例的辅助私网 IP 和各自绑的公网 IP | 开关机、重装、删除实例 |
| 换某一个辅助私网 IP 的公网 IP | 换主私网 IP 的公网 IP |
| 一键换这台实例全部辅助私网 IP 的公网 IP | 新建 / 删除辅助私网 IP |
| 查最近一次更换的进度和结果 | 调用面板的其他任何接口 |

- 密钥**只能**调用本文列出的 4 个接口，拿它调面板其他接口一律返回 401。
- 反过来，这 4 个接口**只认**密钥，不认网页登录的 cookie。
- 只能操作**自己账号下**的租户。
- 和网页上的「多出口 IP」一样，只对**升级（按量付费）账号**开放。

---

## 准备工作

### 1. 新建 API 密钥

面板 →「设置」→「API 密钥」→ 填名称和面板登录密码 →「新建密钥」。

- 完整密钥（`ak_` 开头）**只显示这一次**，请马上保存。面板数据库只存它的哈希，之后查不回来。
- 列表里只显示开头几位、创建时间、最近使用时间和 IP。
- 每个账号最多 10 个；不用的随时删除，删除后立即失效。
- 下面几种操作会让**所有**旧密钥失效（列表里标「已失效」），需要重新生成：
  - 修改面板密码
  - 开启或关闭两步验证
  - 「在所有设备退出登录」

### 2. 找到租户 ID 和实例 OCID

打开实例详情页，浏览器地址栏是这样的：

```
https://你的面板地址/instances/<租户ID>/<实例OCID>
```

更省事的办法：实例详情 →「保留 IP」标签 →「多出口 IP」卡片 → 展开「用 API 换 IP（脚本调用）」，
里面的 curl 示例已经填好了这台实例的地址、租户 ID 和实例 OCID，复制后把 `KEY=` 换成你的密钥即可。

### 3. 用 HTTPS 访问面板

密钥放在请求头里，**IP 直连模式（`http://IP:8000`）下是明文传输的**，链路上的任何人都能看到并拿去用。
请改用域名 + HTTPS 模式（`bash scripts/install.sh domain 你的域名`，见 [ACCESS-MODES.md](ACCESS-MODES.md)），
或者只在内网 / 本机调用。

---

## 鉴权

每个请求带上密钥，二选一：

```
Authorization: Bearer ak_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

```
X-API-Key: ak_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**请设置一个自定义的 User-Agent**，例如 curl 加 `-A "ip-api/1"`，Python 里设置 `User-Agent` 请求头。
不要用 HTTP 客户端的默认值。

---

## 接口一览

公共前缀：

```
{面板地址}/api/v1/tenants/{租户ID}/instances/{实例OCID}/multi-ips
```

| 方法 | 路径（接在前缀后面） | 作用 |
| --- | --- | --- |
| GET | （空） | 列出辅助私网 IP、公网 IP、名字 |
| POST | `/{私网IP}/rotate` | 只换这一个私网 IP 的公网 IP |
| POST | `/rotate-all` | 一键换全部 |
| GET | `/rotate` | 最近一次更换的进度 / 结果 |

- 所有响应都是 JSON（`Content-Type: application/json`）。
- POST 接口不需要请求体。
- 出错时返回非 2xx 状态码，响应体是 `{"detail": "说明"}`，见[错误码](#错误码)。

---

## 接口详情

### 1. 列出多出口 IP

```
GET {前缀}
```

列出实例主网卡上的全部辅助私网 IP，以及各自绑定的公网 IP。顺序和面板「多出口 IP」表一致：
按公网 IP 的名字自然排序（proxy-01、proxy-02 … proxy-10），没有名字的排在最后。

这个接口会实时查询 Oracle。

**响应示例**

```json
{
  "ok": true,
  "primary_private_ip": "10.0.0.180",
  "items": [
    { "private_ip": "10.0.0.136", "public_ip": "152.70.1.11", "name": "proxy-01", "managed": true },
    { "private_ip": "10.0.0.250", "public_ip": "152.70.1.12", "name": "proxy-02", "managed": true },
    { "private_ip": "10.0.0.73",  "public_ip": "",            "name": "",         "managed": false }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `primary_private_ip` | 主私网 IP（不能换，也不在 `items` 里） |
| `items[].private_ip` | 辅助私网 IP |
| `items[].public_ip` | 当前绑定的公网 IP；空字符串 = 没有公网 IP |
| `items[].name` | 公网 IP（保留 IP）的名字 |
| `items[].managed` | 是否面板创建的辅助 IP；`false` 是在 Oracle 控制台手工建的（也能换公网 IP） |

### 2. 换一个私网 IP 的公网 IP

```
POST {前缀}/{私网IP}/rotate
POST {前缀}/{私网IP}/rotate?wait=90
```

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `私网IP` | 路径 | 辅助私网 IP 地址，如 `10.0.0.136`。不能是主私网 IP |
| `wait` | 查询，可选 | 等更换结束再返回，最多等这么多秒，`0`–`600`，默认 `0`（立即返回） |

- 删掉这个私网 IP 现在绑的公网 IP，再申请一个新的绑回去，名字沿用旧的。
- 这个私网 IP 现在没有公网 IP 时，直接分配一个新的。
- 接口不核对「现在绑的是哪个公网 IP」：调用本身就表示「把它现在的换掉」。

**响应示例**（`wait=90`，已经换完）

```json
{
  "ok": true,
  "message": "已更换 1 个公网 IP（私网 IP 不变）",
  "job": {
    "state": "done",
    "phase": "done",
    "message": "已更换 1 个公网 IP（私网 IP 不变）",
    "started_at": "2026-11-02T13:20:01.512+00:00",
    "finished_at": "2026-11-02T13:20:19.044+00:00",
    "items": [
      {
        "private_ip": "10.0.0.136",
        "old_ip": "152.70.1.11",
        "new_ip": "152.70.9.87",
        "name": "proxy-01",
        "status": "done",
        "message": "",
        "same_address": false
      }
    ]
  }
}
```

**`ok` 的含义**

| 情况 | `ok` | `job.state` |
| --- | --- | --- |
| 不带 `wait`，或等到时限还没换完 | `true`，只表示「已开始」 | `running` |
| 等到了，全部换好 | `true` | `done` |
| 等到了，有失败 | `false` | `failed` |
| 任务中断（面板中途重启） | `false` | `interrupted` |

`ok` 为 `false` 时 HTTP 状态码仍是 200（请求本身被接受了），每个 IP 的情况看 `job.items`。

### 3. 一键换全部

```
POST {前缀}/rotate-all
POST {前缀}/rotate-all?wait=90
```

换这台实例**全部**辅助私网 IP 的公网 IP，包括面板创建的、手工创建的，以及当前没有公网 IP 的（直接分配）。
参数和响应同上，`job.items` 里每个私网 IP 一项。

### 4. 查进度 / 结果

```
GET {前缀}/rotate
```

返回这台实例**最近一次**更换的进度或结果，无论它是网页上点的还是 API 发起的。
从没换过时 `job` 为 `null`。

这个接口只读面板自己的数据库，**不请求 Oracle**，轮询它不会消耗 Oracle 的 API 额度。
建议每 2–5 秒查一次。

```json
{
  "ok": true,
  "job": {
    "state": "running",
    "phase": "wait_release",
    "message": "",
    "started_at": "2026-11-02T13:20:01.512+00:00",
    "finished_at": "",
    "items": [
      { "private_ip": "10.0.0.136", "old_ip": "152.70.1.11", "new_ip": "", "name": "proxy-01",
        "status": "releasing", "message": "", "same_address": false }
    ]
  }
}
```

---

## 任务对象（job）

| 字段 | 说明 |
| --- | --- |
| `state` | `running` 进行中 · `done` 全部成功 · `failed` 有失败 · `interrupted` 中断 |
| `phase` | 进行到哪一步，见下表 |
| `message` | 结束时的总结，如「已更换 18 个公网 IP（私网 IP 不变）；2 个失败，见下表」 |
| `started_at` / `finished_at` | 开始 / 结束时间（ISO 8601，UTC）；进行中时 `finished_at` 为空 |
| `items` | 每个私网 IP 一项，见下表 |

**`phase`**

| 值 | 含义 |
| --- | --- |
| `plan` | 准备 |
| `release` | 正在发出删除旧 IP 的请求 |
| `wait_release` | 等 Oracle 把旧 IP 删完（名额腾出来） |
| `create` | 正在申请新 IP |
| `wait_assign` | 等新 IP 绑定完成 |
| `done` | 结束 |

**`items[]`**

| 字段 | 说明 |
| --- | --- |
| `private_ip` | 私网 IP（不变） |
| `old_ip` | 旧公网 IP；原来没有时为空 |
| `new_ip` | 新公网 IP；还没申请到时为空 |
| `name` | 公网 IP 的名字（沿用旧的） |
| `status` | 见下表 |
| `message` | 这一项的说明（失败原因等） |
| `same_address` | `true` = Oracle 分回了原来的地址，等于没换，可以稍后再换一次 |

**`items[].status`**

| 值 | 含义 |
| --- | --- |
| `pending` | 待处理 |
| `releasing` | 旧 IP 删除中 |
| `released` | 旧 IP 已删完，待申请 |
| `assigning` | 新 IP 绑定中 |
| `create_failed` | 申请报错，正在核对 Oracle 是否其实已经建好（过渡状态） |
| `done` | 完成 |
| `failed` | 失败，原因见 `message` |
| `skipped` | 跳过（公网 IP 当时正在变更中，没动它） |

---

## 错误码

出错时响应体为：

```json
{ "detail": "说明文字" }
```

| 状态码 | 什么时候 | `detail` 举例 |
| --- | --- | --- |
| 400 | 私网 IP 格式不对 | `不是有效的 IPv4 地址：abc` |
| 400 | 实例没有辅助私网 IP | `这台实例没有辅助私网 IP` |
| 400 | 没有能换的（都在变更中等） | `公网 IP 正在变更中（ASSIGNING），这次跳过` |
| 401 | 没带密钥 | `缺少 API 密钥：请求头加上 Authorization: Bearer <密钥>` |
| 401 | 密钥不对或已删除 | `API 密钥无效或已删除` |
| 401 | 密钥已失效 | `API 密钥已失效（改过密码、开关过两步验证或退出了所有会话），请到「设置」里重新生成` |
| 403 | 免费账号，或还没识别账号等级 | `多出口 IP 与批量保留 IP 仅限升级（按量付费）账号使用` |
| 404 | 租户不存在或不是你的 | `租户不存在` |
| 404 | 私网 IP 不在这台实例的主网卡上，或是主私网 IP | `这台实例主网卡上没有辅助私网 IP 10.0.0.5（主私网 IP 不能换）` |
| 409 | 这台实例已经有一个更换在进行（网页或 API 发起的都算） | `这台实例已经有一个更换任务在进行，请等它结束` |
| 422 | 参数不合法，如 `wait` 超出 0–600 | FastAPI 的校验信息 |
| 502 | 请求 Oracle 失败、租户配置有问题（如私钥无法解密） | Oracle 返回的错误说明 |

遇到 403 但账号确实是升级账号：先在面板「租户」页对这个租户点一次「等级查询」。
如果所有请求都返回 403，而且说明里不是账号等级的问题，可能是调用方的 IP 被面板封禁了，
请管理员到「用户管理 / 更新」页的「IP 封禁」里查看并解封。

---

## 更换是怎么进行的

和网页上「更换公网 IP」是同一套流程：

1. **先把这一批旧公网 IP 的删除全部发出去**（同时发 5 个）。
2. 哪个旧 IP 被 Oracle **彻底删完**（不再占保留 IP 名额），就马上给它的私网 IP **申请新的保留 IP 并直接绑上**，
   名字、区间、地址池沿用旧的。不等最慢的那个。
3. 看到新 IP 状态变成「已绑定」才算完成。

要点：

- **名额不会超**：每个新 IP 都是在它自己的旧 IP 不再占名额之后才申请，保留 IP 总数不会超过更换前。
  每区域 50 个用满了也能换。
- **会断网**：被换的出口从删除旧 IP 到新 IP 绑好之间不通。只换一个时影响这一个，一键换全部时这一批同时断开。
  主公网 IP 和 SSH 不受影响。
- **旧地址找不回来**：删除后归还 Oracle，以后也不一定能再分到。
- **服务器里不用改配置**：私网 IP、网卡、实例元数据都不变，同步服务也不用重装。
- **同一台实例同时只跑一个更换**：正在换时再调用返回 409。不同实例可以同时换。
- **自动处理的情况**：
  - Oracle 限流时按 2 → 4 → 8 → 16 秒退避重试；SDK 熔断器打开时等它恢复再试。
  - 新 IP 悄悄绑定失败时会再绑一次；还不行就留在保留 IP 列表里，`message` 会说明，可以到面板手动绑定。
  - 申请报错但 Oracle 其实已经建好的，会按私网 IP 认回来，不会重复申请。
- **失败了怎么补**：
  - 「申请新 IP 失败，这个私网 IP 现在没有公网 IP」：再对这个私网 IP 调一次 `/{私网IP}/rotate`，会直接分配一个，名字照旧。
  - 「删除旧 IP 失败，这个没有更换」：它还是原来的地址，稍后重试即可。
- **用时**：主要取决于 Oracle 释放、绑定的速度，一般几十秒到几分钟，IP 越多越久。
- **审计**：每次 API 更换都会记进面板「审计日志」（操作「API · 更换多出口公网 IP」），包括用的是哪把密钥（开头几位）、
  换的是哪个私网 IP 或全部。

### 关于 `wait` 和超时

- 带 `wait` 时请求会一直挂着，直到换完或到时限。
- 面板前面有 **Cloudflare**（约 100 秒超时）或其他反向代理时，等待超过它的超时会被它提前断开，
  客户端看到 524 / 504 之类的错误，**但更换照常在后台进行**。之后用 `GET {前缀}/rotate` 查结果即可。
- 稳妥的做法：`wait` 设 90 以内；如果返回时 `job.state` 还是 `running`，再轮询查进度接口。

---

## 示例

下面的例子里：

```bash
KEY="ak_你的密钥"
BASE="https://panel.example.com/api/v1/tenants/<租户ID>/instances/<实例OCID>/multi-ips"
UA="ip-api/1"
```

### curl

```bash
# 列出
curl -s -A "$UA" -H "Authorization: Bearer $KEY" "$BASE"

# 只换 10.0.0.136 的公网 IP，最多等 90 秒
curl -s -A "$UA" -X POST -H "Authorization: Bearer $KEY" "$BASE/10.0.0.136/rotate?wait=90"

# 一键换全部，立即返回
curl -s -A "$UA" -X POST -H "Authorization: Bearer $KEY" "$BASE/rotate-all"

# 查进度 / 结果
curl -s -A "$UA" -H "Authorization: Bearer $KEY" "$BASE/rotate"
```

### Bash：换一个 IP，打印新地址（需要 jq）

```bash
#!/usr/bin/env bash
set -euo pipefail
KEY="ak_你的密钥"
BASE="https://panel.example.com/api/v1/tenants/<租户ID>/instances/<实例OCID>/multi-ips"
UA="ip-api/1"
PRIVATE_IP="${1:?用法：$0 <私网IP>}"

api() { curl -sS -A "$UA" -H "Authorization: Bearer $KEY" -w '\n%{http_code}' "$@"; }

# 发起更换并等最多 90 秒
resp="$(api -X POST "$BASE/$PRIVATE_IP/rotate?wait=90")"
code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
if [ "$code" != "200" ]; then
  echo "发起失败（HTTP $code）：$(echo "$body" | jq -r '.detail')" >&2
  exit 1
fi

# 90 秒内没换完就接着查进度
state="$(echo "$body" | jq -r '.job.state')"
while [ "$state" = "running" ]; do
  sleep 3
  body="$(curl -sS -A "$UA" -H "Authorization: Bearer $KEY" "$BASE/rotate")"
  state="$(echo "$body" | jq -r '.job.state')"
done

echo "$body" | jq -r '.job.items[] | "\(.private_ip)  \(.old_ip) -> \(.new_ip)  [\(.status)] \(.message)"'
[ "$state" = "done" ]
```

### Python（只用标准库，Python 3.6+）

```python
import json
import time
import urllib.error
import urllib.request

KEY = "ak_你的密钥"
BASE = "https://panel.example.com/api/v1/tenants/<租户ID>/instances/<实例OCID>/multi-ips"
HEADERS = {"Authorization": f"Bearer {KEY}", "User-Agent": "ip-api/1"}


def call(method: str, path: str = "", timeout: float = 120) -> dict:
    req = urllib.request.Request(BASE + path, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = json.load(exc).get("detail", "")
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None


def rotate(private_ip=None) -> dict:
    """换一个（给出私网 IP）或全部（不给），等换完返回任务对象。"""
    path = f"/{private_ip}/rotate?wait=90" if private_ip else "/rotate-all?wait=90"
    job = call("POST", path)["job"]
    while job and job["state"] == "running":
        time.sleep(3)
        job = call("GET", "/rotate")["job"]
    return job


if __name__ == "__main__":
    for ip in call("GET")["items"]:
        print(ip["name"] or "-", ip["private_ip"], ip["public_ip"] or "（无公网 IP）")

    job = rotate("10.0.0.136")          # 只换一个；rotate() 不带参数 = 一键换全部
    for it in job["items"]:
        print(it["private_ip"], it["old_ip"], "->", it["new_ip"], it["status"], it["message"])
    print("成功" if job["state"] == "done" else f"未全部成功：{job['message']}")
```

---

## 安全建议

- **只走 HTTPS**：IP 直连模式下密钥是明文传输的。
- **一个脚本一把密钥**，名字写清楚用在哪；不用了就删掉。出了问题删掉那一把即可，不影响别的。
- **不要把密钥写进公开的仓库**或发到聊天群里；放在环境变量或权限为 600 的配置文件里。
- 怀疑泄漏：直接在「设置 → API 密钥」删掉；或者「在所有设备退出登录」，一次让所有密钥失效。
- 密钥的权限只有换多出口 IP，泄漏了也删不了机器、看不到私钥，但对方可以反复换你的出口 IP 造成断网。
- 定期看「审计日志」里「API · 更换多出口公网 IP」的记录，以及密钥列表里的「最近使用」时间和 IP。

---

## 常见问题

**换完了，服务器里要做什么吗？**
不用。私网 IP 没变，流量照常从对应的新公网 IP 出去。

**能不能按公网 IP 或名字指定要换哪个？**
目前只能按私网 IP 指定。先调「列出多出口 IP」，按 `name` 或 `public_ip` 找到对应的 `private_ip`。

**返回 409 怎么办？**
这台实例正在换（可能是网页上点的）。用 `GET {前缀}/rotate` 看进度，等 `state` 不是 `running` 了再调用。

**`same_address` 是 `true`？**
Oracle 把原来的地址又分回来了，等于没换。过一会儿再换一次这个私网 IP 即可。

**任务显示 `interrupted`？**
面板在更换过程中重启了。用「列出多出口 IP」看哪些私网 IP 的 `public_ip` 是空的，对它们再调一次
`/{私网IP}/rotate` 补上，名字会沿用。

**能同时换好几台实例吗？**
可以，不同实例互不影响；同一台实例同时只能有一个更换。

**调用太频繁会怎样？**
每次更换都会删除、申请保留 IP，消耗 Oracle 的 API 额度，和面板的抢机重试等功能共用同一份额度。
请只在需要时换，查进度用 `GET {前缀}/rotate`（不请求 Oracle）。
