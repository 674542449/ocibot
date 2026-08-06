<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>账号用量</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          订阅等级 / 配额 / 每月账单与支付状态 · 点击「刷新」才请求 Oracle
        </p>
      </div>
      <div class="page-tools">
        <select v-model.number="days">
          <option :value="7">7 天</option>
          <option :value="30">30 天</option>
          <option :value="90">90 天</option>
        </select>
        <button class="primary" :disabled="loading || !tenantId" @click="loadAll">
          {{ loading ? '加载中…' : '刷新用量' }}
        </button>
      </div>
    </div>

    <div class="card stack">
      <div class="field">
        <label>租户</label>
        <select v-model="tenantId">
          <option disabled value="">选择租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">
            {{ t.name }} · {{ t.region }} · {{ tierLabel(t.account_tier) }}
          </option>
        </select>
        <p class="muted" style="margin: 0.35rem 0 0; font-size: 12px">
          切换租户不会自动请求 API；选定后请点右上角「刷新用量」。
        </p>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div v-if="data" class="card stack">
      <div class="grid-2">
        <div>
          <div class="muted" style="font-size: 12px">租户名</div>
          <div style="font-weight: 700">{{ data.tenancy_name || '—' }}</div>
        </div>
        <div>
          <div class="muted" style="font-size: 12px">Home Region</div>
          <div>{{ data.home_region || '—' }}</div>
        </div>
        <div>
          <div class="muted" style="font-size: 12px">账号等级</div>
          <div>
            <span
              class="badge"
              :class="
                data.tier_code === 'paid' ? 'running' : data.tier_code === 'free' ? 'warn' : ''
              "
            >
              {{ data.tier || '未知' }}
            </span>
          </div>
        </div>
        <div>
          <div class="muted" style="font-size: 12px">说明</div>
          <div class="muted" style="font-size: 12px">{{ data.description || '—' }}</div>
        </div>
      </div>
      <p style="margin: 0; font-size: 13px">{{ data.tier_reason }}</p>
      <p class="muted" style="margin: 0; font-size: 12px">{{ data.tier_note }}</p>

      <!-- Filtered to the Always Free shapes server-side (FREE_TIER_LIMIT_TAGS):
           Oracle reports non-zero limits for paid families on free accounts too,
           so listing them was quota the operator has no use for. -->
      <h3 style="margin: 0.5rem 0 0">计算配额（参考 · 仅免费套餐 A1 / E2.1.Micro）</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!(data.limits || []).length">
              <td colspan="2" class="muted empty">无配额数据或无权限</td>
            </tr>
            <tr v-for="l in data.limits || []" :key="l.name">
              <td>{{ l.name }}</td>
              <td>{{ l.value }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="quota" class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">免费额度用量（Always Free）</h3>
        <span class="q-pill" :class="'st-' + (quota.overall_status || 'ok')">
          {{ quotaStatusText(quota.overall_status) }}
        </span>
      </div>
      <div class="gauge-grid">
        <div v-for="g in visibleGauges" :key="g.key" class="gauge-card">
          <div class="row" style="justify-content: space-between; font-size: 13px">
            <span style="font-weight: 600">{{ g.label }}</span>
            <span class="badge" :class="'st-' + bucket(g.key).status">{{
              quotaStatusText(bucket(g.key).status)
            }}</span>
          </div>
          <div style="font-size: 1.25rem; font-weight: 700; margin: 0.25rem 0">
            {{ fmt(bucket(g.key).used) }}
            <span class="muted" style="font-size: 12px; font-weight: 500">
              / {{ fmt(bucket(g.key).limit) }} {{ g.unit }}
            </span>
          </div>
          <div class="gauge-track">
            <div
              class="gauge-fill"
              :class="'st-' + bucket(g.key).status"
              :style="{ width: pct(bucket(g.key)) }"
            ></div>
          </div>
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            剩余 {{ fmt(bucket(g.key).remaining) }} {{ g.unit }}
            <span v-if="bucket(g.key).soft"> · 软追踪</span>
          </div>
        </div>
      </div>
      <p v-if="(quota.notes || []).length" class="muted" style="font-size: 12px; margin: 0">
        {{ (quota.notes || []).join('；') }}
      </p>
      <p class="muted" style="font-size: 11px; margin: 0">
        额度为 Oracle Always Free 参考上限，实际以账单为准；块存储含引导卷 + 块卷合计；对象存储为近似统计；公网
        IP 为软追踪。
        <router-link v-if="tenantId" :to="`/storage?tenant=${tenantId}`">管理存储 →</router-link>
      </p>

      <h4 style="margin: 0.5rem 0 0">实例占用</h4>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>Shape</th>
              <th>状态</th>
              <th>占用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!(quota.instances || []).length">
              <td colspan="4" class="muted empty">无运行中相关实例</td>
            </tr>
            <tr v-for="inst in quota.instances || []" :key="inst.id">
              <td>
                <router-link
                  v-if="inst.id"
                  :to="`/instances/${tenantId}/${inst.id}`"
                  style="color: var(--text); font-weight: 600"
                >
                  {{ inst.display_name || shortId(inst.id) }}
                </router-link>
                <span v-else>{{ inst.display_name || '—' }}</span>
              </td>
              <td style="font-size: 12px">{{ inst.shape }}</td>
              <td><span class="badge">{{ inst.lifecycle_state }}</span></td>
              <td class="muted" style="font-size: 12px">
                <template v-if="inst.units">
                  <span v-if="inst.units.a1_ocpu">A1 {{ inst.units.a1_ocpu }}C/{{ inst.units.a1_memory_gb }}G</span>
                  <span v-if="inst.units.e2_micro_count">E2.Micro ×{{ inst.units.e2_micro_count }}</span>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4 style="margin: 0.5rem 0 0">卷明细</h4>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>大小</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!(quota.volumes || []).length">
              <td colspan="4" class="muted empty">无卷数据</td>
            </tr>
            <tr v-for="v in quota.volumes || []" :key="v.id || v.display_name">
              <td>{{ v.display_name || shortId(v.id) }}</td>
              <td>{{ v.kind === 'block' ? '块卷' : '引导卷' }}</td>
              <td>{{ v.size_in_gbs }} GB</td>
              <td><span class="badge">{{ v.lifecycle_state || '—' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4 v-if="(quota.object_buckets || []).length" style="margin: 0.5rem 0 0">对象存储桶</h4>
      <div v-if="(quota.object_buckets || []).length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>桶</th>
              <th>约占用</th>
              <th>对象数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in quota.object_buckets || []" :key="b.name">
              <td>{{ b.name }}</td>
              <td>{{ fmt(b.approximate_size_gb) }} GB</td>
              <td>{{ b.object_count ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card stack">
      <div class="row" style="justify-content: space-between; align-items: baseline">
        <h3 style="margin: 0">每月账单</h3>
        <span v-if="invoiceError" class="badge stopped">读取失败</span>
        <span v-else-if="unpaidCount > 0" class="badge warn">{{ unpaidCount }} 张未付清</span>
        <span v-else-if="invoices.length" class="badge running">已全部付清</span>
      </div>
      <p v-if="invoiceMsg && !invoiceError" class="muted" style="margin: 0; font-size: 12px">
        {{ invoiceMsg }}
      </p>
      <div v-if="invoices.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>账期</th>
              <th>账单号</th>
              <th>金额</th>
              <th>未付金额</th>
              <th>到期日</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="inv in invoices" :key="inv.invoice_id || inv.invoice_number">
              <td>{{ billingMonth(inv.time_invoice) }}</td>
              <td class="mono">{{ inv.invoice_number || '—' }}</td>
              <td class="mono">{{ money(inv.amount, inv.currency) }}</td>
              <td class="mono">{{ money(inv.amount_due, inv.currency) }}</td>
              <td>{{ dateOnly(inv.time_due) }}</td>
              <td>
                <span class="badge" :class="invoiceClass(inv)">{{ invoiceLabel(inv) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else-if="invoiceError" class="error-box">读取账单失败：{{ invoiceError }}</div>
      <div v-else class="empty">该账号暂无账单记录。</div>
    </div>

    <div class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">费用（最近 {{ days }} 天）</h3>
        <div class="muted" style="font-size: 13px">
          合计：
          <strong>{{ usage?.total ?? '—' }}</strong>
          {{ usage?.currency || '' }}
        </div>
      </div>

      <!-- 本月费用. Deliberately its own block rather than another number in the
           header row: it answers a different question from "最近 N 天" (calendar
           month vs rolling window) and the two are easy to mistake for each other
           when they sit side by side. The period is spelled out for the same
           reason. -->
      <div class="mtd">
        <div class="mtd-label">
          本月费用
          <span class="mtd-period" v-if="usage?.month_start">
            {{ usage.month_start }} 起 · UTC
          </span>
        </div>
        <div class="mtd-value">
          <template v-if="monthToDate !== null">
            <strong>{{ monthToDate }}</strong>
            <span class="mtd-cur">{{ usage?.currency || '' }}</span>
          </template>
          <!-- Never 0.00 for a read that failed: for a cost figure those two
               readings mean opposite things. -->
          <span v-else class="mtd-none">未能读取</span>
        </div>
      </div>

      <p class="muted" style="margin: 0; font-size: 12px">{{ usageMsg }}</p>
      <div v-if="(usage?.daily || []).length" class="chart-wrap">
        <svg :viewBox="`0 0 ${svgW} ${svgH}`" class="bar-chart">
          <g v-for="(pt, i) in usage.daily" :key="pt.date">
            <rect
              :x="barX(i)"
              :y="barY(pt.amount)"
              :width="barW"
              :height="Math.max(1, svgH - 24 - barY(pt.amount))"
              fill="#3b82f6"
              opacity="0.85"
            />
          </g>
          <line x1="0" :y1="svgH - 20" :x2="svgW" :y2="svgH - 20" stroke="#334155" />
        </svg>
        <div class="row muted" style="font-size: 11px; justify-content: space-between">
          <span>{{ usage.daily[0]?.date }}</span>
          <span>{{ usage.daily[usage.daily.length - 1]?.date }}</span>
        </div>
      </div>
      <div v-else class="muted" style="font-size: 13px">暂无每日费用数据</div>

      <h4 style="margin: 0.5rem 0 0">按服务</h4>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>服务</th>
              <th>金额</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!(usage?.by_service || []).length">
              <td colspan="2" class="muted empty">无服务拆分</td>
            </tr>
            <tr v-for="s in usage?.by_service || []" :key="s.service">
              <td>{{ s.service }}</td>
              <td>{{ s.amount }} {{ usage?.currency || '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api, { type Tenant } from '@/api/client'
import { pickTenantId } from '@/stores/tenantLock'

const route = useRoute()
const tenants = ref<Tenant[]>([])
const tenantId = ref('')
const loading = ref(false)
const error = ref('')
const msg = ref('')
const data = ref<any>(null)
const usage = ref<any>(null)
const usageMsg = ref('')

/** Month-to-date spend, or null when the read did not succeed.
 *
 *  The server sends null (not 0) in that case on purpose, so the distinction has
 *  to survive here too: `?? 0` or a bare falsy check would turn "Oracle refused
 *  the usage read" into a confident "本月 0.00", which is the one wrong answer
 *  nobody would question. Formatted to 2 decimals because this is money. */
const monthToDate = computed<string | null>(() => {
  const raw = usage.value?.month_to_date
  if (raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n.toFixed(2) : null
})
const days = ref(30)

const svgW = 640
const svgH = 160
const barW = computed(() => {
  const n = Math.max(1, (usage.value?.daily || []).length)
  return Math.max(2, (svgW - 20) / n - 2)
})
const maxAmount = computed(() => {
  const vals = (usage.value?.daily || []).map((d: any) => Number(d.amount || 0))
  return Math.max(1, ...vals, 0.01)
})

function barX(i: number) {
  const n = Math.max(1, (usage.value?.daily || []).length)
  return 10 + i * ((svgW - 20) / n)
}
function barY(amount: number) {
  const h = svgH - 24
  return 8 + h - (Number(amount || 0) / maxAmount.value) * h
}

function tierLabel(t: string) {
  return { paid: '已升级', free: '免费' }[t] || '未知'
}

const quota = ref<any>(null)
const gauges = [
  { key: 'a1_ocpu', label: 'ARM A1 OCPU', unit: '' },
  { key: 'a1_memory_gb', label: 'ARM A1 内存', unit: 'GB' },
  { key: 'e2_micro_count', label: 'AMD E2.Micro 实例', unit: '台' },
  { key: 'block_storage_gb', label: '块存储（引导+块卷）', unit: 'GB' },
  { key: 'object_storage_gb', label: '对象存储', unit: 'GB' },
  { key: 'public_ip_soft', label: '公网 IP（软）', unit: '个' },
  // Absent unless the server was asked for it (include_egress); visibleGauges
  // drops it rather than rendering an empty bar that reads as "10TB free".
  { key: 'egress_gb', label: '出网流量（本月·估算）', unit: 'GB' },
]
const visibleGauges = computed(() => gauges.filter((g) => (quota.value?.buckets || {})[g.key]))
function bucket(key: string) {
  return (
    (quota.value?.buckets || {})[key] || {
      used: 0,
      limit: 0,
      remaining: 0,
      ratio: 0,
      status: 'ok',
      soft: false,
    }
  )
}
function pct(b: any) {
  const r = Math.max(0, Math.min(1, Number(b?.ratio || 0)))
  return (r * 100).toFixed(1) + '%'
}
function fmt(n: any) {
  const v = Number(n || 0)
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, '')
}
function shortId(id: string) {
  if (!id) return '—'
  if (id.length <= 18) return id
  return `${id.slice(0, 8)}…${id.slice(-6)}`
}
function quotaStatusText(s: string) {
  const m: Record<string, string> = {
    over: '已超免费额度',
    critical: '接近上限',
    // Exactly at the cap. For a free account that is the intended end state —
    // two of two free micros running is the whole point — so it is not an alarm.
    full: '额度已用满',
    warn: '偏高',
    ok: '正常',
  }
  return m[s] || s || '正常'
}

async function loadTenants() {
  const { data: rows } = await api.get<Tenant[]>('/tenants')
  tenants.value = rows
  // Preserve the user's current selection; only pick a default when it is unset
  // or no longer exists (otherwise a refresh would snap the dropdown back).
  if (tenantId.value && rows.some((t) => t.id === tenantId.value)) return
  tenantId.value = pickTenantId(rows, route.query.tenant)
}

// Request-sequence guard, same as StorageView/InstancesView. Without it a slow
// response for tenant A lands after the user switched to tenant B and fills the
// page with A's spend, invoices and remaining free quota under B's name — every
// number on the page is wrong and nothing says so.
//
// The counter is PER LOADER: loadAll() starts four at once, so one shared
// counter would have each of them bump it and discard all but the last-started.
const loadSeq: Record<string, number> = {}

function beginLoad(key: string): { stale: () => boolean } {
  const seq = (loadSeq[key] = (loadSeq[key] || 0) + 1)
  const wanted = tenantId.value
  return { stale: () => seq !== loadSeq[key] || tenantId.value !== wanted }
}

async function loadAccount() {
  if (!tenantId.value) return
  const guard = beginLoad('account')
  const res = await api.get(`/tenants/${tenantId.value}/account`)
  if (guard.stale()) return
  data.value = res.data.data || {}
  msg.value = res.data.message || ''
}

async function loadUsage() {
  if (!tenantId.value) return
  const guard = beginLoad('usage')
  try {
    const res = await api.get(`/tenants/${tenantId.value}/usage`, { params: { days: days.value } })
    if (guard.stale()) return
    usage.value = res.data.data || {}
    usageMsg.value = res.data.message || ''
    if (!res.data.ok && res.data.message) {
      // soft fail — still show empty chart area
      usageMsg.value = res.data.message
    }
  } catch (e: any) {
    if (guard.stale()) return
    usage.value = { daily: [], by_service: [], total: 0 }
    usageMsg.value = e?.message || '读取账单失败'
  }
}

/** Invoices for this tenancy, newest first. Empty on a free account — such a
 *  tenancy has no subscription, so it is never billed. */
const invoices = ref<any[]>([])
const invoiceMsg = ref('')
/** Set only when the read itself failed. Kept apart from invoiceMsg so the page
 *  never reports "no bills" for an account it could not read. */
const invoiceError = ref('')
const unpaidCount = computed(
  () => invoices.value.filter((i) => !i.is_paid).length,
)

function billingMonth(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function dateOnly(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 10)
}

function money(v: number | null, currency: string): string {
  if (v === null || v === undefined) return '—'
  return `${Number(v).toFixed(2)}${currency ? ' ' + currency : ''}`
}

/** Paid is the answer being asked for, so it is stated plainly; a failed
 *  payment is called out separately because it needs action. */
function invoiceLabel(inv: any): string {
  if (inv.is_paid) return '已支付'
  if (inv.is_payment_failed) return '支付失败'
  return { OPEN: '未支付', PAST_DUE: '已逾期', PAYMENT_SUBMITTED: '支付处理中' }[
    String(inv.status || '').toUpperCase() as string
  ] || '未支付'
}

function invoiceClass(inv: any): string {
  if (inv.is_paid) return 'running'
  if (inv.is_payment_failed || String(inv.status).toUpperCase() === 'PAST_DUE') return 'stopped'
  return 'warn'
}

async function loadInvoices() {
  if (!tenantId.value) return
  const guard = beginLoad('invoices')
  invoiceError.value = ''
  try {
    const res = await api.get(`/tenants/${tenantId.value}/invoices`)
    if (guard.stale()) return
    invoices.value = res.data.data?.invoices || []
    invoiceMsg.value = res.data.message || ''
    // ok=false means Oracle refused the read; an empty list then proves nothing.
    if (!res.data.ok) {
      invoiceError.value = res.data.message || '未知原因'
      invoices.value = []
    }
  } catch (e: any) {
    if (guard.stale()) return
    invoices.value = []
    invoiceMsg.value = ''
    invoiceError.value = e?.message || '请求失败'
  }
}

async function loadQuota() {
  if (!tenantId.value) return
  const guard = beginLoad('quota')
  try {
    const res = await api.get(`/tenants/${tenantId.value}/free-quota`)
    if (guard.stale()) return
    quota.value = res.data.data || null
  } catch {
    if (guard.stale()) return
    quota.value = null
  }
}

async function loadAll() {
  if (!tenantId.value) return
  const guard = beginLoad('all')
  error.value = ''
  loading.value = true
  try {
    await Promise.all([loadAccount(), loadUsage(), loadQuota(), loadInvoices()])
  } catch (e: any) {
    if (guard.stale()) return
    error.value = e?.message || '加载失败'
  } finally {
    // Only the newest run may clear the spinner. Otherwise an earlier, slower
    // run finishing second turns it off while the current one is still loading.
    if (!guard.stale()) loading.value = false
  }
}

onMounted(async () => {
  try {
    await loadTenants()
    // Do not auto-hit Oracle on enter; user clicks 刷新 / 加载.
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>

<style scoped>
/* 本月费用. Given its own surface so it reads as a distinct figure rather than a
   second interpretation of the chart below it — the chart is a rolling window,
   this is the calendar month. */
.mtd {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.4rem 1rem;
  padding: 0.6rem 0.75rem;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  background: var(--panel-2);
}

.mtd-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

/* The period is part of the number's meaning, not decoration: without it "本月"
   is ambiguous about the timezone, and Oracle bills on UTC. */
.mtd-period {
  margin-left: 0.5rem;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--muted);
}

.mtd-value {
  font-family: var(--font-mono);
  font-size: 20px;
  line-height: 1.2;
  color: var(--text);
}

.mtd-cur {
  margin-left: 0.3rem;
  font-size: 12px;
  color: var(--muted);
}

.mtd-none {
  font-size: 13px;
  font-family: inherit;
  color: var(--warn);
}

.chart-wrap {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.5rem;
  background: var(--panel-2);
}
.bar-chart {
  width: 100%;
  height: 160px;
  display: block;
}
/* An allowance is a bounded quantity, not a task in progress — the quarter ticks
   let you read "about three quarters gone" without doing the division, which is
   the question actually being asked of this bar. */
.gauge-track {
  position: relative;
  height: 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 3px;
  overflow: hidden;
  margin-top: 4px;
}
.gauge-track::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: repeating-linear-gradient(
    to right,
    transparent 0 calc(25% - 1px),
    color-mix(in srgb, var(--text) 22%, transparent) calc(25% - 1px) 25%
  );
}
.gauge-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 220px), 1fr));
  gap: 0.75rem;
}
.gauge-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.75rem;
  background: var(--panel);
  box-shadow: var(--shadow-sm);
}
.gauge-fill {
  height: 100%;
  border-radius: 6px;
  transition: width 0.3s ease;
}
.q-pill {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 650;
  color: #fff;
}
.st-ok {
  background: var(--ok);
}
/* Fully used, not over. Same colour family as ok — a free account that has used
   its whole allowance is in the expected state, not a fault to act on. */
.st-full {
  background: var(--ok);
  opacity: 0.72;
}
.st-warn {
  background: var(--warn);
}
.st-critical {
  background: #f5319d;
}
.st-over {
  background: var(--danger);
  color: #fff;
}
.badge.st-ok,
.badge.st-full {
  background: var(--ok-soft);
  color: var(--ok);
  border-color: transparent;
}
.badge.st-warn {
  background: var(--warn-soft);
  color: var(--warn);
  border-color: transparent;
}
.badge.st-critical,
.badge.st-over {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: transparent;
}
:global(html[data-theme='dark']) .badge.st-ok,
:global(html[data-theme='dark']) .badge.st-full {
  color: #7dffa8;
  background: rgba(28, 78, 52, 0.85);
}
:global(html[data-theme='dark']) .badge.st-warn {
  color: #ffd27a;
  background: rgba(90, 64, 20, 0.85);
}
:global(html[data-theme='dark']) .badge.st-critical,
:global(html[data-theme='dark']) .badge.st-over {
  color: #ffb0ad;
  background: rgba(96, 40, 38, 0.85);
}
</style>
