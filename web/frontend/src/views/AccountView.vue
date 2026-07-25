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

async function loadTenants() {
  const { data: rows } = await api.get<Tenant[]>('/tenants')
  tenants.value = rows
  const q = String(route.query.tenant || '')
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

async function loadAll() {
  if (!tenantId.value) return
  error.value = ''
  loading.value = true
  try {
    await Promise.all([loadAccount(), loadUsage()])
    await loadTenants()
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
</style>
