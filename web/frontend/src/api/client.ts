import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 120_000,
  withCredentials: true, // auth is the HttpOnly cookie; always send/receive it
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    const detail = err?.response?.data?.detail
    if (typeof detail === 'string') {
      err.message = detail
    } else if (Array.isArray(detail)) {
      err.message = detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ')
    }
    if (err?.response?.status === 401) {
      localStorage.removeItem('ocibot_username')
      const url: string = err?.config?.url || ''
      // Let the router guard handle the initial /auth/me probe; only hard-redirect
      // on a genuine mid-session expiry, preserving where the user was.
      if (!url.endsWith('/auth/me') && !location.pathname.startsWith('/login')) {
        const redirect = encodeURIComponent(location.pathname + location.search)
        location.href = `/login?redirect=${redirect}`
      }
    }
    return Promise.reject(err)
  },
)

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
  budget_monthly_usd: number
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

export type ScheduleJob = {
  id: string
  tenant_id: string
  name: string
  enabled: boolean
  kind: string
  time_of_day: string
  weekdays: number[]
  run_at: string | null
  action: string
  instance_ids: string[]
  last_run_date: string
  created_at: string
}

export type AuditItem = {
  id: string
  action: string
  target: string
  detail: string
  created_at: string
}
