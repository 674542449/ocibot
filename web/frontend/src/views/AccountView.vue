<template>
  <div class="stack">
    <div class="row" style="justify-content: space-between">
      <div>
        <h2 style="margin: 0">账号用量</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          订阅等级 / 配额 / 费用曲线（Usage API，需权限）
        </p>
      </div>
      <div class="row">
        <select v-model.number="days" style="width: auto" @change="loadUsage">
          <option :value="7">7 天</option>
          <option :value="30">30 天</option>
          <option :value="90">90 天</option>
        </select>
        <button class="primary" :disabled="loading || !tenantId" @click="loadAll">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div class="card stack">
      <div class="field">
        <label>租户</label>
        <select v-model="tenantId" @change="loadAll">
          <option disabled value="">选择租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">
            {{ t.name }} · {{ t.region }} · {{ tierLabel(t.account_tier) }}
          </option>
        </select>
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

      <h3 style="margin: 0.5rem 0 0">计算配额（参考）</h3>
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
              <td colspan="2" class="muted">无配额数据或无权限</td>
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
              <td colspan="4" class="muted">无运行中相关实例</td>
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
              <td colspan="4" class="muted">无卷数据</td>
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
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">费用（最近 {{ days }} 天）</h3>
        <div class="muted" style="font-size: 13px">
          合计：
          <strong>{{ usage?.total ?? '—' }}</strong>
          {{ usage?.currency || '' }}
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
              <td colspan="2" class="muted">无服务拆分</td>
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

const route = useRoute()
const tenants = ref<Tenant[]>([])
const tenantId = ref('')
const loading = ref(false)
const error = ref('')
const msg = ref('')
const data = ref<any>(null)
const usage = ref<any>(null)
const usageMsg = ref('')
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
    warn: '偏高',
    ok: '正常',
  }
  return m[s] || s || '正常'
}

async function loadTenants() {
  const { data: rows } = await api.get<Tenant[]>('/tenants')
  tenants.value = rows
  const q = String(route.query.tenant || '')
  // Preserve the user's current selection; only pick a default when it is unset
  // or no longer exists (otherwise a refresh would snap the dropdown back).
  if (tenantId.value && rows.some((t) => t.id === tenantId.value)) return
  if (q && rows.some((t) => t.id === q)) tenantId.value = q
  else if (rows[0]) tenantId.value = rows[0].id
}

async function loadAccount() {
  if (!tenantId.value) return
  const res = await api.get(`/tenants/${tenantId.value}/account`)
  data.value = res.data.data || {}
  msg.value = res.data.message || ''
}

async function loadUsage() {
  if (!tenantId.value) return
  try {
    const res = await api.get(`/tenants/${tenantId.value}/usage`, { params: { days: days.value } })
    usage.value = res.data.data || {}
    usageMsg.value = res.data.message || ''
    if (!res.data.ok && res.data.message) {
      // soft fail — still show empty chart area
      usageMsg.value = res.data.message
    }
  } catch (e: any) {
    usage.value = { daily: [], by_service: [], total: 0 }
    usageMsg.value = e?.message || '读取账单失败'
  }
}

async function loadQuota() {
  if (!tenantId.value) return
  try {
    const res = await api.get(`/tenants/${tenantId.value}/free-quota`)
    quota.value = res.data.data || null
  } catch {
    quota.value = null
  }
}

async function loadAll() {
  if (!tenantId.value) return
  error.value = ''
  loading.value = true
  try {
    await Promise.all([loadAccount(), loadUsage(), loadQuota()])
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    await loadTenants()
    if (tenantId.value) await loadAll()
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>

<style scoped>
.chart-wrap {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.5rem;
  background: #0b1220;
}
.bar-chart {
  width: 100%;
  height: 160px;
  display: block;
}
.gauge-track {
  height: 10px;
  background: #1e293b;
  border-radius: 6px;
  overflow: hidden;
  margin-top: 4px;
}
.gauge-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 0.75rem;
}
.gauge-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.75rem;
  background: var(--panel-2);
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
  font-weight: 700;
  color: #0b1220;
}
.st-ok {
  background: #22c55e;
}
.st-warn {
  background: #f59e0b;
}
.st-critical {
  background: #f97316;
}
.st-over {
  background: #ef4444;
  color: #fff;
}
.badge.st-ok {
  background: #14532d;
  color: #86efac;
}
.badge.st-warn {
  background: #78350f;
  color: #fde68a;
}
.badge.st-critical,
.badge.st-over {
  background: #7f1d1d;
  color: #fecaca;
}
</style>
