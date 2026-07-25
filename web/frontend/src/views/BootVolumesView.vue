<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>引导卷</h2>
        <p class="muted" style="margin: 0.2rem 0 0">容量与挂载实例</p>
      </div>
      <div class="page-tools">
        <select v-model="tenantId" @change="load">
          <option disabled value="">选择租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">
            {{ t.name }} · {{ t.region }}
          </option>
        </select>
        <label class="row muted" style="font-size: 12px; flex: 0 0 auto">
          <input v-model="includeSub" type="checkbox" style="width: auto" @change="load" />
          含子 Compartment
        </label>
        <button class="primary" :disabled="loading || !tenantId" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div class="row stats" v-if="summary">
      <div class="stat card">
        <div class="muted" style="font-size: 12px">引导卷数量</div>
        <div class="stat-num">{{ summary.count }}</div>
      </div>
      <div class="stat card">
        <div class="muted" style="font-size: 12px">合计容量</div>
        <div class="stat-num">{{ summary.total_gb }} GB</div>
      </div>
      <div class="stat card">
        <div class="muted" style="font-size: 12px">已挂载</div>
        <div class="stat-num ok">{{ summary.attached }}</div>
      </div>
      <div class="stat card">
        <div class="muted" style="font-size: 12px">未挂载</div>
        <div class="stat-num warn">{{ summary.orphaned }}</div>
      </div>
    </div>

    <div class="row">
      <input
        v-model="search"
        placeholder="搜索名称 / OCID / 实例 / AD"
        style="max-width: 320px"
      />
      <select v-model="filterAttach" style="width: auto">
        <option value="all">全部</option>
        <option value="attached">仅已挂载</option>
        <option value="orphaned">仅未挂载</option>
      </select>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>容量</th>
            <th>性能</th>
            <th>状态</th>
            <th>Hydration</th>
            <th>挂载实例</th>
            <th>可用域</th>
            <th>OCID</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && filtered.length === 0">
            <td colspan="8" class="muted">
              {{ volumes.length ? '没有匹配的引导卷' : '暂无引导卷，请选择租户后刷新。' }}
            </td>
          </tr>
          <tr v-for="v in filtered" :key="v.id">
            <td>
              <div style="font-weight: 600">{{ v.display_name }}</div>
              <div class="muted" style="font-size: 11px">{{ formatTime(v.time_created) }}</div>
            </td>
            <td>{{ v.size_in_gbs }} GB</td>
            <td>
              {{ v.vpus_per_gb }}
              <span class="badge">{{ v.performance_label || '' }}</span>
            </td>
            <td>
              <span class="badge" :class="stateClass(v.lifecycle_state)">{{
                v.lifecycle_state || '—'
              }}</span>
            </td>
            <td>
              <span
                class="badge"
                :class="
                  v.is_hydrated === true ? 'running' : v.is_hydrated === false ? 'warn' : ''
                "
              >
                {{
                  v.is_hydrated === true ? '完成' : v.is_hydrated === false ? '同步中' : '—'
                }}
              </span>
            </td>
            <td>
              <template v-if="v.instance_id">
                <router-link
                  :to="`/instances/${tenantId}/${v.instance_id}`"
                  style="color: var(--text); font-weight: 600"
                >
                  {{ v.instance_name || v.instance_id.slice(-12) }}
                </router-link>
                <div
                  class="muted copyable"
                  style="font-size: 11px; word-break: break-all"
                  title="单击复制实例 OCID"
                  role="button"
                  tabindex="0"
                  @click="copy(v.instance_id)"
                  @keydown.enter.prevent="copy(v.instance_id)"
                >
                  {{ v.instance_id }}
                </div>
                <div v-if="v.attachment_state" class="muted" style="font-size: 11px">
                  附件：{{ v.attachment_state }}
                </div>
              </template>
              <span v-else class="badge warn">未挂载</span>
            </td>
            <td style="font-size: 12px">{{ v.availability_domain || '—' }}</td>
            <td>
              <span
                class="copyable"
                style="font-size: 11px; word-break: break-all"
                title="单击复制引导卷 OCID"
                role="button"
                tabindex="0"
                @click="copy(v.id)"
                @keydown.enter.prevent="copy(v.id)"
              >{{ shortId(v.id) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="muted" style="font-size: 12px; margin: 0">
      显示 {{ filtered.length }} / {{ volumes.length }} · OCI 仅提供配置容量，不含系统内已用空间
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api, { type Tenant } from '@/api/client'
import { copyText } from '@/utils/toast'

type BootVolume = {
  id: string
  display_name: string
  size_in_gbs: number
  vpus_per_gb: number
  performance_label?: string
  lifecycle_state: string
  availability_domain: string
  compartment_id: string
  is_hydrated: boolean | null
  time_created: string
  instance_id: string
  instance_name: string
  attachment_state: string
}

const route = useRoute()
const tenants = ref<Tenant[]>([])
const tenantId = ref('')
const includeSub = ref(true)
const loading = ref(false)
const error = ref('')
const msg = ref('')
const volumes = ref<BootVolume[]>([])
const summary = ref<{ count: number; total_gb: number; attached: number; orphaned: number } | null>(
  null,
)
const search = ref('')
const filterAttach = ref<'all' | 'attached' | 'orphaned'>('all')

const filtered = computed(() => {
  let list = volumes.value
  if (filterAttach.value === 'attached') list = list.filter((v) => !!v.instance_id)
  if (filterAttach.value === 'orphaned') list = list.filter((v) => !v.instance_id)
  const q = search.value.trim().toLowerCase()
  if (!q) return list
  return list.filter((v) => {
    const blob = [
      v.display_name,
      v.id,
      v.instance_id,
      v.instance_name,
      v.availability_domain,
      v.lifecycle_state,
      String(v.size_in_gbs),
    ]
      .join(' ')
      .toLowerCase()
    return blob.includes(q)
  })
})

function shortId(id: string) {
  if (!id) return '—'
  if (id.length <= 22) return id
  return `${id.slice(0, 10)}…${id.slice(-8)}`
}

function stateClass(state: string) {
  const s = (state || '').toUpperCase()
  if (s === 'AVAILABLE') return 'running'
  if (s === 'PROVISIONING' || s === 'RESTORING') return 'warn'
  if (s.includes('FAULT') || s.includes('TERMINAT')) return 'err'
  return ''
}

function formatTime(v: string) {
  if (!v) return ''
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

async function copy(text: string) {
  await copyText(text || '')
}

async function loadTenants() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data.filter((t) => t.enabled)
  const q = String(route.query.tenant || '')
  if (q && tenants.value.some((t) => t.id === q)) tenantId.value = q
  else if (tenants.value[0]) tenantId.value = tenants.value[0].id
}

async function load() {
  if (!tenantId.value) return
  loading.value = true
  error.value = ''
  msg.value = ''
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/boot-volumes`, {
      params: { include_subcompartments: includeSub.value },
    })
    volumes.value = data.data?.volumes || []
    summary.value = data.data?.summary || null
    msg.value = data.message || ''
    if (!data.ok && data.message) error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '加载失败'
    volumes.value = []
    summary.value = null
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    await loadTenants()
    if (tenantId.value) await load()
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.75rem;
}
.stat {
  padding: 0.75rem 1rem;
}
.stat-num {
  font-size: 1.5rem;
  font-weight: 700;
}
.stat-num.ok {
  color: #86efac;
}
.stat-num.warn {
  color: #fde68a;
}
.copyable {
  cursor: copy;
  border-bottom: 1px dashed #64748b;
  user-select: none;
}
.copyable:hover {
  color: #93c5fd;
}
@media (max-width: 900px) {
  .stats {
    grid-template-columns: 1fr 1fr;
  }
}
@media (max-width: 420px) {
  .stats {
    grid-template-columns: 1fr;
  }
}
</style>
