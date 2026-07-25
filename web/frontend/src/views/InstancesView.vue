<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>实例</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          搜索 / 导出 · 点名称进详情
        </p>
      </div>
      <div class="page-tools">
        <select v-model="tenantId">
          <option value="">全部租户（聚合）</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">{{ t.name }} · {{ t.region }}</option>
        </select>
        <input v-model="search" type="search" placeholder="搜索名称 / IP / OCID" />
        <label class="choice muted" style="flex: 0 0 auto">
          <input v-model="resolveIps" type="checkbox" />
          <span>解析 IP</span>
        </label>
        <button class="primary" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
        <button type="button" :disabled="!filtered.length" @click="exportCsv">导出 CSV</button>
        <router-link :to="{ path: '/launch', query: tenantId ? { tenant: tenantId } : {} }">
          <button type="button" class="primary">创建实例</button>
        </router-link>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div v-if="selected.size > 0" class="card row batch-bar">
      <strong>已选 {{ selected.size }} 台</strong>
      <button :disabled="batchBusy" @click="batchPower('START')">批量开机</button>
      <button :disabled="batchBusy" @click="batchPower('SOFTSTOP')">批量关机</button>
      <button :disabled="batchBusy" @click="batchPower('SOFTRESET')">批量重启</button>
      <button :disabled="batchBusy" @click="selected.clear()">取消选择</button>
      <span v-if="batchBusy" class="muted" style="font-size: 12px">{{ batchProgress }}</span>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width: 34px">
              <input
                type="checkbox"
                :checked="allSelected"
                title="全选当前列表"
                @change="toggleSelectAll"
              />
            </th>
            <th>名称</th>
            <th>状态</th>
            <th>租户</th>
            <th>Shape</th>
            <th>公网 IP</th>
            <th>私网 IP</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && filtered.length === 0">
            <td colspan="8" class="muted">
              {{ instances.length ? '没有匹配搜索的实例' : '暂无实例。请先在「租户」添加 API，再「创建实例」。' }}
            </td>
          </tr>
          <tr v-for="ins in filtered" :key="ins.id + ins.tenant_id">
            <td>
              <input
                type="checkbox"
                :checked="selected.has(selKey(ins))"
                @change="toggleSelect(ins)"
              />
            </td>
            <td>
              <router-link
                :to="`/instances/${ins.tenant_id || tenantId}/${ins.id}`"
                style="font-weight: 600; color: var(--text)"
              >
                {{ ins.display_name }}
              </router-link>
              <div class="muted" style="font-size: 11px; word-break: break-all">{{ ins.id }}</div>
            </td>
            <td>
              <span class="badge" :class="stateClass(ins.lifecycle_state)">{{ ins.lifecycle_state }}</span>
            </td>
            <td>{{ ins.tenant_name || '—' }}</td>
            <td>
              {{ ins.shape }}
              <span v-if="ins.free_tier_tag" class="badge">{{ ins.free_tier_tag }}</span>
              <div class="muted" style="font-size: 12px">
                <template v-if="ins.ocpus != null">{{ ins.ocpus }} OCPU</template>
                <template v-if="ins.memory_in_gbs != null"> · {{ ins.memory_in_gbs }} GB</template>
              </div>
            </td>
            <td>
              <span
                class="copyable"
                :class="{ empty: !ins.public_ip }"
                title="单击复制公网 IPv4"
                role="button"
                tabindex="0"
                @click="copyIp(ins.public_ip, $event)"
                @keydown.enter.prevent="copyIp(ins.public_ip)"
              >{{ ins.public_ip || '—' }}</span>
              <div v-if="ins.ipv6_addresses?.length" class="muted" style="font-size: 11px; margin-top: 2px">
                <span
                  v-for="ip6 in ins.ipv6_addresses"
                  :key="ip6"
                  class="copyable"
                  title="单击复制 IPv6"
                  style="display: inline-block; margin-right: 0.35rem"
                  role="button"
                  tabindex="0"
                  @click="copyIp(ip6, $event)"
                  @keydown.enter.prevent="copyIp(ip6)"
                >{{ ip6 }}</span>
              </div>
            </td>
            <td>
              <span
                class="copyable"
                :class="{ empty: !ins.private_ip }"
                title="单击复制私网 IP"
                role="button"
                tabindex="0"
                @click="copyIp(ins.private_ip, $event)"
                @keydown.enter.prevent="copyIp(ins.private_ip)"
              >{{ ins.private_ip || '—' }}</span>
            </td>
            <td>
              <div class="actions-row">
                <router-link :to="`/instances/${ins.tenant_id || tenantId}/${ins.id}`">
                  <button type="button">详情</button>
                </router-link>
                <button :disabled="acting === ins.id" @click="power(ins, 'START')">开机</button>
                <button :disabled="acting === ins.id" @click="power(ins, 'SOFTSTOP')">关机</button>
                <button :disabled="acting === ins.id" @click="power(ins, 'SOFTRESET')">重启</button>
                <button :disabled="acting === ins.id" @click="rename(ins)">重命名</button>
                <button :disabled="acting === ins.id" @click="replaceIp(ins)">换IP</button>
                <button class="danger" :disabled="acting === ins.id" @click="terminate(ins)">终止</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="muted" style="font-size: 12px; margin: 0">
      共 {{ filtered.length }} / {{ instances.length }} 台 · 单击公网 / 私网 / IPv6 可复制
    </p>
  </div>
</template>

<style scoped>
.batch-bar {
  position: sticky;
  top: 0.5rem;
  z-index: 5;
}
</style>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import api, { type Instance, type Tenant } from '@/api/client'
import { copyText, showToast } from '@/utils/toast'

const route = useRoute()
const tenants = ref<Tenant[]>([])
const instances = ref<Instance[]>([])
const tenantId = ref('')
const resolveIps = ref(true)
const search = ref('')
const loading = ref(false)
const acting = ref('')
const error = ref('')
const msg = ref('')
const selected = reactive(new Set<string>())
const batchBusy = ref(false)
const batchProgress = ref('')

function selKey(ins: Instance) {
  return `${ins.tenant_id || tenantId.value}|${ins.id}`
}

const allSelected = computed(
  () => filtered.value.length > 0 && filtered.value.every((i) => selected.has(selKey(i))),
)

function toggleSelect(ins: Instance) {
  const key = selKey(ins)
  if (selected.has(key)) selected.delete(key)
  else selected.add(key)
}

function toggleSelectAll() {
  if (allSelected.value) {
    selected.clear()
  } else {
    for (const ins of filtered.value) selected.add(selKey(ins))
  }
}

async function batchPower(action: string) {
  const targets = filtered.value.filter((i) => selected.has(selKey(i)))
  if (!targets.length) return
  const verb: Record<string, string> = { START: '开机', SOFTSTOP: '关机', SOFTRESET: '重启' }
  if (!confirm(`对已选 ${targets.length} 台实例执行「${verb[action] || action}」？`)) return
  error.value = ''
  msg.value = ''
  batchBusy.value = true
  let okCount = 0
  const failures: string[] = []
  try {
    // Sequential on purpose: keeps request rate gentle on the OCI API.
    for (let i = 0; i < targets.length; i++) {
      const ins = targets[i]
      batchProgress.value = `${i + 1}/${targets.length} · ${ins.display_name}`
      try {
        await api.post(`/tenants/${ins.tenant_id || tenantId.value}/instances/${ins.id}/power`, {
          action,
        })
        okCount++
      } catch (e: any) {
        failures.push(`${ins.display_name}: ${e?.message || '失败'}`)
      }
    }
    msg.value = `批量${verb[action] || action}完成：${okCount}/${targets.length} 成功`
    if (failures.length) error.value = failures.join('；')
    selected.clear()
    await load()
  } finally {
    batchBusy.value = false
    batchProgress.value = ''
  }
}

const filtered = computed(() => {
  // Backend already drops TERMINATED; keep a client-side guard for older API caches.
  let list = instances.value.filter(
    (ins) => String(ins.lifecycle_state || '').toUpperCase() !== 'TERMINATED',
  )
  const q = search.value.trim().toLowerCase()
  if (!q) return list
  return list.filter((ins) => {
    const blob = [
      ins.display_name,
      ins.id,
      ins.shape,
      ins.public_ip,
      ins.private_ip,
      ins.tenant_name,
      ins.lifecycle_state,
      ...(ins.ipv6_addresses || []),
    ]
      .join(' ')
      .toLowerCase()
    return blob.includes(q)
  })
})

function stateClass(state: string) {
  const s = (state || '').toUpperCase()
  if (s === 'RUNNING') return 'running'
  if (s === 'STOPPED') return 'stopped'
  if (s.includes('STOP') || s.includes('START') || s === 'PROVISIONING') return 'warn'
  if (s.includes('TERMINAT')) return 'err'
  return ''
}

function exportCsv() {
  const rows = filtered.value
  const header = [
    'display_name',
    'lifecycle_state',
    'tenant_name',
    'shape',
    'ocpus',
    'memory_in_gbs',
    'public_ip',
    'private_ip',
    'region',
    'id',
  ]
  const lines = [header.join(',')]
  for (const ins of rows) {
    const vals = [
      ins.display_name,
      ins.lifecycle_state,
      ins.tenant_name,
      ins.shape,
      ins.ocpus ?? '',
      ins.memory_in_gbs ?? '',
      ins.public_ip,
      ins.private_ip,
      ins.region,
      ins.id,
    ].map((v) => `"${String(v ?? '').replace(/"/g, '""')}"`)
    lines.push(vals.join(','))
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `ocibot-instances-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
  showToast(`已导出 ${rows.length} 行`, 'ok')
}

async function copy(text: string) {
  await copyText(text)
}

function copyIp(text?: string | null, ev?: Event) {
  if (ev) {
    ev.preventDefault()
    ev.stopPropagation()
  }
  if (!text) return
  void copyText(text, '已复制 IP')
}

async function loadTenants() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data
}

let loadSeq = 0
async function load() {
  error.value = ''
  msg.value = ''
  loading.value = true
  const seq = ++loadSeq
  try {
    let data: Instance[]
    if (tenantId.value) {
      const res = await api.get<Instance[]>(`/tenants/${tenantId.value}/instances`, {
        params: { resolve_ips: resolveIps.value, include_subcompartments: true },
      })
      data = res.data
    } else {
      const res = await api.get<Instance[]>('/instances', {
        params: { resolve_ips: resolveIps.value },
      })
      data = res.data
    }
    // Discard out-of-order responses from a superseded tenant switch.
    if (seq !== loadSeq) return
    instances.value = data
  } catch (e: any) {
    if (seq !== loadSeq) return
    error.value = e?.message || '加载失败'
    instances.value = []
  } finally {
    if (seq === loadSeq) {
      loading.value = false
      // Drop selections that no longer exist in the refreshed list.
      const live = new Set(instances.value.map((i) => selKey(i)))
      for (const key of [...selected]) {
        if (!live.has(key)) selected.delete(key)
      }
    }
  }
}

async function power(ins: Instance, action: string) {
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/power`, { action })
    msg.value = data.message || `${action} 已提交`
    await load()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  } finally {
    acting.value = ''
  }
}

async function rename(ins: Instance) {
  const name = prompt('新名称', ins.display_name)
  if (!name || !name.trim()) return
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/rename`, {
      display_name: name.trim(),
    })
    msg.value = data.message || '已重命名'
    await load()
  } catch (e: any) {
    error.value = e?.message || '重命名失败'
  } finally {
    acting.value = ''
  }
}

async function replaceIp(ins: Instance) {
  if (!confirm(`更换「${ins.display_name}」的临时公网 IPv4？旧地址会释放。`)) return
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/public-ip/replace`)
    if (data.ok) msg.value = data.message
    else error.value = data.message
    await load()
  } catch (e: any) {
    error.value = e?.message || '更换失败'
  } finally {
    acting.value = ''
  }
}

async function terminate(ins: Instance) {
  if (!confirm(`确认终止实例「${ins.display_name}」？此操作危险且可能不可恢复。`)) return
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/terminate`, {
      preserve_boot_volume: false,
    })
    msg.value = data.message || '终止已提交'
    await load()
  } catch (e: any) {
    error.value = e?.message || '终止失败'
  } finally {
    acting.value = ''
  }
}

watch(tenantId, () => {
  load()
})

onMounted(async () => {
  try {
    await loadTenants()
    const q = String(route.query.tenant || '')
    if (q && q !== tenantId.value) {
      // Changing tenantId triggers the watcher, which loads — avoid a double fetch.
      tenantId.value = q
    } else {
      await load()
    }
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>
