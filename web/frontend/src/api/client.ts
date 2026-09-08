/**
 * 面板的 HTTP 客户端。
 *
 * 以前是 axios。换成 fetch 的理由只有一个：axios 占入口包的 24%
 * （187,806 → 142,312 字节，gzip 70,639 → 53,838），而入口包是**唯一阻塞首屏**的
 * 那个；而这里真正用到的 axios 能力只有下面这几十行 —— baseURL、JSON 序列化、
 * 查询参数、超时、一个响应拦截器。
 *
 * 下面四件事是**照着调用方的现有写法**复刻的，不是设计选择。改动它们等于悄悄
 * 弄坏散在各个视图里的分支：
 *
 *   1. 泛型方法 `get<T>()` —— 24 处这么写，去掉泛型会炸出 30 个 TS2558。
 *   2. `headers` 是**普通对象、键全小写**，不是 Headers 实例 ——
 *      InstanceDetailView 和 InstancesView 直接下标取
 *      `res.headers['x-ocibot-reread']`；给 Headers 实例的话取到的是 undefined，
 *      而且是**静默**的（那两处是「这次读是不是瞬时故障/部分失败」的提示）。
 *   3. 抛出的错误上要挂 `err.response = { status, data }` —— BackupView 靠
 *      `e.response.data instanceof Blob` 解析导出失败的正文，LaunchView 靠
 *      `e.response.status` 区分「请求被拒」和「响应丢了」。
 *   4. 超时的错误消息里必须有 "timeout" —— LaunchView:1403 用
 *      `/timeout/i.test(e.message)` 判断「客户端超时」，那条分支决定要不要提示
 *      用户「机器可能已经开出来了，去列表看一眼」。
 */

const BASE = '/api'
const DEFAULT_TIMEOUT = 120_000

export interface ApiResponse<T = any> {
  data: T
  status: number
  /** 普通对象，键全小写 —— 调用方是下标取用的。 */
  headers: Record<string, string>
}

export interface ApiError extends Error {
  response?: { status: number; data: any; headers: Record<string, string> }
}

interface RequestConfig {
  params?: Record<string, any>
  timeout?: number
  responseType?: 'blob'
  headers?: Record<string, string>
}

function buildUrl(path: string, params?: Record<string, any>): string {
  const p = path.startsWith('/') ? path : `/${path}`
  const url = `${BASE}${p}`
  if (!params) return url
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    // undefined / null 不进查询串。`false` 和 `0` 要进 —— 后端有布尔开关，
    // 用真值判断会把 `force=false` 悄悄丢掉，变成「没传」而不是「传了 false」。
    if (v === undefined || v === null) continue
    qs.append(k, String(v))
  }
  const s = qs.toString()
  return s ? `${url}?${s}` : url
}

function plainHeaders(h: Headers): Record<string, string> {
  const out: Record<string, string> = {}
  h.forEach((value, key) => {
    out[key.toLowerCase()] = value
  })
  return out
}

async function parseBody(res: Response, responseType?: 'blob'): Promise<any> {
  if (responseType === 'blob') return await res.blob()
  const type = res.headers.get('content-type') || ''
  if (res.status === 204 || res.status === 205) return null
  if (type.includes('application/json')) {
    // 声明了 JSON 但正文是空的（有些代理会这么干）——不要让 JSON.parse 抛，
    // 那会把一次成功的请求变成一个看不懂的错误。
    const text = await res.text()
    if (!text) return null
    try {
      return JSON.parse(text)
    } catch {
      return text
    }
  }
  return await res.text()
}

function messageFrom(data: any, fallback: string): string {
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((d: any) => d?.msg || JSON.stringify(d)).join('; ')
  }
  return fallback
}

function on401(path: string) {
  localStorage.removeItem('ocibot_username')
  // 初次的 /auth/me 探测交给路由守卫处理；只有会话中途过期才硬跳转，
  // 并把当前位置带上，登录完能回到原地。
  if (!path.endsWith('/auth/me') && !location.pathname.startsWith('/login')) {
    const redirect = encodeURIComponent(location.pathname + location.search)
    location.href = `/login?redirect=${redirect}`
  }
}

async function request<T = any>(
  method: string,
  path: string,
  body?: any,
  config: RequestConfig = {},
): Promise<ApiResponse<T>> {
  const url = buildUrl(path, config.params)
  const headers: Record<string, string> = { ...(config.headers || {}) }
  let payload: BodyInit | undefined

  if (body instanceof FormData) {
    // **不要**设 Content-Type：浏览器要自己加上 multipart 的 boundary，
    // 手写一个没有 boundary 的头会让后端解析不出任何字段。
    payload = body
  } else if (body !== undefined && body !== null) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const controller = new AbortController()
  const ms = config.timeout ?? DEFAULT_TIMEOUT
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, ms)

  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers,
      body: payload,
      // 认证是 HttpOnly cookie，每个请求都要带上。
      credentials: 'same-origin',
      signal: controller.signal,
    })
  } catch (err: any) {
    clearTimeout(timer)
    // 消息里必须留下 "timeout" 这个词：LaunchView 用它区分「客户端超时」
    // 和「服务端拒绝」，那条分支决定要不要提示用户去列表确认机器有没有开出来。
    const e: ApiError = new Error(
      timedOut ? `timeout of ${ms}ms exceeded` : err?.message || '网络请求失败',
    )
    throw e
  }
  clearTimeout(timer)

  const data = await parseBody(res, config.responseType)
  const hdrs = plainHeaders(res.headers)

  if (!res.ok) {
    if (res.status === 401) on401(path)
    const e: ApiError = new Error(
      messageFrom(config.responseType === 'blob' ? null : data, `请求失败（${res.status}）`),
    )
    // 调用方读的就是这个形状（BackupView 的 Blob 分支、LaunchView 的 status 分支）。
    e.response = { status: res.status, data, headers: hdrs }
    throw e
  }

  return { data: data as T, status: res.status, headers: hdrs }
}

const api = {
  get: <T = any>(path: string, config?: RequestConfig) =>
    request<T>('GET', path, undefined, config),
  delete: <T = any>(path: string, config?: RequestConfig) =>
    request<T>('DELETE', path, undefined, config),
  post: <T = any>(path: string, body?: any, config?: RequestConfig) =>
    request<T>('POST', path, body, config),
  put: <T = any>(path: string, body?: any, config?: RequestConfig) =>
    request<T>('PUT', path, body, config),
  patch: <T = any>(path: string, body?: any, config?: RequestConfig) =>
    request<T>('PATCH', path, body, config),
}

export default api

/** Build a same-origin WebSocket URL for an /api/... path. */
export function wsUrl(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`
  const base = p.startsWith('/api') ? p : `/api${p}`
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${base}`
}

export type TokenResponse = {
  access_token: string
  token_type: string
  username: string
}

export type Tenant = {
  id: string
  name: string
  user_ocid: string
  tenancy_ocid: string
  fingerprint: string
  region: string
  compartment_ocid: string
  description: string
  enabled: boolean
  color: string
  has_private_key: boolean
  account_tier: string
  free_only_mode: boolean
  /** '' on a primary tenant; the primary's id on a 副区 (secondary region) row. */
  parent_tenant_id: string
  /** Localized region name, e.g. 大阪. */
  region_label: string
  created_at: string
  updated_at: string
}

/** One region in the 副区 picker. */
export type TenantRegion = {
  region_name: string
  region_key: string
  region_label: string
  is_home_region: boolean
  status: string
  subscribed: boolean
  /** Panel tenant row managing this region ('' = not added yet). */
  tenant_id: string
}

export type TenantRegions = {
  ok: boolean
  message: string
  home_region: string
  subscribed: TenantRegion[]
  available: TenantRegion[]
}

/** Oracle Identity Domain password-policy mutation result. */
export type OciPasswordPolicyResult = {
  ok: boolean
  message: string
  data?: Record<string, unknown>
}

export type Instance = {
  id: string
  display_name: string
  lifecycle_state: string
  shape: string
  region: string
  availability_domain: string
  compartment_id: string
  time_created: string
  ocpus: number | null
  memory_in_gbs: number | null
  public_ip: string
  private_ip: string
  ipv6_addresses: string[]
  boot_volume_size_in_gbs: number | null
  free_tier_tag: string
  /** 创建时若选了 root 密码模式，密码会写进实例标签，这里带回来。密钥模式为空。 */
  root_password: string
  /** 终止保护。存在 OCI 的 freeform tag `ocibot_protected` 上，所以面板重装后
   *  依然有效，在 Oracle 控制台里也看得见。开启时后端的 terminate 直接返回 409。 */
  protected: boolean
  /** Oracle Cloud Agent 的监控插件是否被禁用 —— 也就是「监控页为什么一片空白」
   *  的答案。null = 该实例没有返回 agent_config（老实例/老镜像），此时 UI 不做
   *  任何断言，而不是把「不知道」显示成「已启用」。 */
  monitoring_disabled: boolean | null
  tenant_id: string
  tenant_name: string
}

export type CapacityJob = {
  id: string
  tenant_id: string
  name: string
  enabled: boolean
  status: string
  interval_sec: number
  max_attempts: number
  attempts: number
  last_error: string
  last_attempt_at: string | null
  next_run_at: string | null
  cooldown_until: string | null
  consecutive_rate_limits: number
  success_instance_id: string
  created_at: string
  updated_at: string
  launch_payload: Record<string, unknown>
  fallback_configs: Record<string, unknown>[]
  has_user_data: boolean
}

export type AuditItem = {
  id: string
  action: string
  target: string
  detail: string
  created_at: string
}
