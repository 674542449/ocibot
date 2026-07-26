<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>存储</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          引导卷 / 块卷 / 对象存储
        </p>
      </div>
      <div class="page-tools">
        <select v-model="tenantId" @change="onTenantChange">
          <option disabled value="">选择租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">
            {{ t.name }} · {{ t.region }}
          </option>
        </select>
        <button class="primary" :disabled="loading || !tenantId" @click="refreshAll">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div v-if="quota" class="row stats">
      <div class="stat card">
        <div class="muted" style="font-size: 12px">块存储剩余</div>
        <div class="stat-num">
          {{ fmt(quota.buckets?.block_storage_gb?.remaining) }} /
          {{ fmt(quota.buckets?.block_storage_gb?.limit) }} GB
        </div>
      </div>
      <div class="stat card">
        <div class="muted" style="font-size: 12px">对象存储剩余</div>
        <div class="stat-num">
          {{ fmt(quota.buckets?.object_storage_gb?.remaining) }} /
          {{ fmt(quota.buckets?.object_storage_gb?.limit) }} GB
        </div>
      </div>
      <div class="stat card">
        <div class="muted" style="font-size: 12px">整体</div>
        <div class="stat-num">{{ quotaStatusText(quota.overall_status) }}</div>
      </div>
    </div>

    <div class="tab-row" role="tablist">
      <button
        v-for="t in tabDefs"
        :key="t.id"
        type="button"
        role="tab"
        :class="{ primary: tab === t.id }"
        @click="tab = t.id"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- Boot -->
    <div v-if="tab === 'boot'" class="stack">
      <div class="page-tools">
        <label class="choice muted" style="flex: 0 0 auto">
          <input v-model="includeSub" type="checkbox" @change="loadBoot" />
          <span>含子 Compartment</span>
        </label>
        <input v-model="bootSearch" type="search" placeholder="搜索引导卷" />
      </div>
      <div class="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>容量</th>
              <th>性能</th>
              <th>状态</th>
              <th>挂载实例</th>
              <th>可用域</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!bootVolumes.length">
              <td colspan="6" class="muted">暂无引导卷</td>
            </tr>
            <tr v-for="v in filteredBoot" :key="v.id">
              <td>
                <div style="font-weight: 600">{{ v.display_name }}</div>
                <div class="muted" style="font-size: 11px">{{ shortId(v.id) }}</div>
              </td>
              <td>{{ v.size_in_gbs }} GB</td>
              <td>{{ v.vpus_per_gb }} <span class="badge">{{ v.performance_label || '' }}</span></td>
              <td><span class="badge">{{ v.lifecycle_state }}</span></td>
              <td>
                <router-link
                  v-if="v.instance_id"
                  :to="`/instances/${tenantId}/${v.instance_id}`"
                  style="color: var(--text); font-weight: 600"
                >
                  {{ v.instance_name || shortId(v.instance_id) }}
                </router-link>
                <span v-else class="badge warn">未挂载</span>
              </td>
              <td style="font-size: 12px">{{ v.availability_domain || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Block -->
    <div v-if="tab === 'block'" class="stack">
      <div class="card stack" style="padding: 0.85rem">
        <h3 style="margin: 0">创建块卷</h3>
        <div class="grid-2">
          <div class="field">
            <label>名称</label>
            <input v-model="createForm.display_name" placeholder="可选" />
          </div>
          <div class="field">
            <label>可用域</label>
            <input v-model="createForm.availability_domain" placeholder="如：xxxx:US-ASHBURN-AD-1" />
          </div>
          <div class="field">
            <label>大小 GB（≥50）</label>
            <input v-model.number="createForm.size_in_gbs" type="number" min="50" />
          </div>
          <div class="field">
            <label>性能 VPUs/GB</label>
            <select v-model.number="createForm.vpus_per_gb">
              <option :value="10">10 平衡</option>
              <option :value="20">20</option>
              <option :value="30">30</option>
              <option :value="60">60</option>
              <option :value="120">120</option>
            </select>
          </div>
        </div>
        <button class="primary" :disabled="busy" @click="createBlock">创建</button>
      </div>

      <div class="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>容量</th>
              <th>状态</th>
              <th>挂载</th>
              <th>可用域</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!blockVolumes.length">
              <td colspan="6" class="muted">暂无块卷</td>
            </tr>
            <tr v-for="v in blockVolumes" :key="v.id">
              <td>
                <div style="font-weight: 600">{{ v.display_name }}</div>
                <div class="muted" style="font-size: 11px">{{ shortId(v.id) }}</div>
              </td>
              <td>{{ v.size_in_gbs }} GB</td>
              <td><span class="badge">{{ v.lifecycle_state }}</span></td>
              <td>
                <template v-if="v.instance_id">
                  {{ v.instance_name || shortId(v.instance_id) }}
                  <div class="muted" style="font-size: 11px">{{ v.attachment_state }}</div>
                </template>
                <span v-else class="badge warn">未挂载</span>
              </td>
              <td style="font-size: 12px">{{ v.availability_domain || '—' }}</td>
              <td>
                <div class="row" style="flex-wrap: wrap">
                  <button
                    v-if="!v.instance_id"
                    :disabled="busy"
                    @click="attachBlock(v)"
                  >
                    挂载
                  </button>
                  <button
                    v-if="v.attachment_id"
                    :disabled="busy"
                    @click="detachBlock(v)"
                  >
                    卸载
                  </button>
                  <button :disabled="busy" @click="resizeBlock(v)">扩容</button>
                  <button class="danger" :disabled="busy || !!v.instance_id" @click="deleteBlock(v)">
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="muted" style="font-size: 12px; margin: 0">
        挂载默认 PARAVIRTUALIZED。删除前须卸载。Always Free 块存储（含引导卷）合计 200GB。
      </p>
    </div>

    <!-- Object -->
    <div v-if="tab === 'object'" class="stack">
      <div class="card stack" style="padding: 0.85rem">
        <div class="row" style="justify-content: space-between">
          <h3 style="margin: 0">存储桶</h3>
          <span class="muted" style="font-size: 12px">namespace: {{ objectNs || '—' }}</span>
        </div>
        <div class="row">
          <input v-model="newBucket" placeholder="新桶名称" style="max-width: 240px" />
          <button class="primary" :disabled="busy || !newBucket" @click="createBucket">创建桶</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>创建时间</th>
                <th>访问</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!buckets.length">
                <td colspan="4" class="muted">暂无存储桶</td>
              </tr>
              <tr v-for="b in buckets" :key="b.name">
                <td>
                  <button type="button" style="background: none; border: none; color: var(--accent); padding: 0" @click="openBucket(b.name)">
                    {{ b.name }}
                  </button>
                </td>
                <td class="muted" style="font-size: 12px">{{ formatTime(b.time_created) }}</td>
                <td>{{ b.public_access_type || 'NoPublicAccess' }}</td>
                <td>
                  <button class="danger" :disabled="busy" @click="deleteBucket(b.name)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="activeBucket" class="card stack" style="padding: 0.85rem">
        <div class="row" style="justify-content: space-between">
          <h3 style="margin: 0">对象 · {{ activeBucket }}</h3>
          <button @click="activeBucket = ''">关闭</button>
        </div>
        <div class="row">
          <input type="file" @change="onFile" />
          <button class="primary" :disabled="busy || !uploadFile" @click="uploadObject">上传（≤10MB）</button>
          <button :disabled="busy" @click="loadObjects">刷新</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>大小</th>
                <th>修改时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!objects.length">
                <td colspan="4" class="muted">空桶或未加载</td>
              </tr>
              <tr v-for="o in objects" :key="o.name">
                <td>{{ o.name }}</td>
                <td>{{ formatBytes(o.size) }}</td>
                <td class="muted" style="font-size: 12px">{{ formatTime(o.time_modified || o.time_created) }}</td>
                <td>
                  <button class="danger" :disabled="busy" @click="deleteObject(o.name)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { type Tenant } from '@/api/client'

type TabId = 'boot' | 'block' | 'object'
const tabDefs = [
  { id: 'boot' as const, label: '引导卷' },
  { id: 'block' as const, label: '块卷' },
  { id: 'object' as const, label: '对象存储' },
]

const route = useRoute()
const router = useRouter()
const tenants = ref<Tenant[]>([])
const tenantId = ref('')
const tab = ref<TabId>('boot')
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const msg = ref('')
const quota = ref<any>(null)
const includeSub = ref(true)

const bootVolumes = ref<any[]>([])
const bootSearch = ref('')
const blockVolumes = ref<any[]>([])
const buckets = ref<any[]>([])
const objectNs = ref('')
const activeBucket = ref('')
const objects = ref<any[]>([])
const newBucket = ref('')
const uploadFile = ref<File | null>(null)

const createForm = reactive({
  display_name: '',
  availability_domain: '',
  size_in_gbs: 50,
  vpus_per_gb: 10,
})

const filteredBoot = computed(() => {
  const q = bootSearch.value.trim().toLowerCase()
  if (!q) return bootVolumes.value
  return bootVolumes.value.filter((v) =>
    [v.display_name, v.id, v.instance_id, v.instance_name, v.availability_domain]
      .join(' ')
      .toLowerCase()
      .includes(q),
  )
})

function shortId(id: string) {
  if (!id) return '—'
  if (id.length <= 22) return id
  return `${id.slice(0, 10)}…${id.slice(-8)}`
}
function fmt(n: any) {
  const v = Number(n || 0)
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, '')
}
function quotaStatusText(s: string) {
  return ({ over: '已超', critical: '接近上限', warn: '偏高', ok: '正常' } as any)[s] || s || '—'
}
function formatTime(v: string) {
  if (!v) return ''
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}
function formatBytes(n: number) {
  const v = Number(n || 0)
  if (v > 1e9) return (v / 1e9).toFixed(2) + ' GB'
  if (v > 1e6) return (v / 1e6).toFixed(2) + ' MB'
  if (v > 1e3) return (v / 1e3).toFixed(1) + ' KB'
  return v + ' B'
}

async function loadTenants() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data.filter((t) => t.enabled)
  const q = String(route.query.tenant || '')
  if (q && tenants.value.some((t) => t.id === q)) tenantId.value = q
  else if (tenants.value[0]) tenantId.value = tenants.value[0].id
  const t = String(route.query.tab || '')
  if (t === 'boot' || t === 'block' || t === 'object') tab.value = t
}

// Request-sequence guard. Without it a slow response for tenant A could land
// after the user switched to tenant B and repopulate the page with A's
// volumes/buckets — and because bucket and object actions are addressed by NAME,
// the next click would then be sent to tenant B for a resource that only exists
// in A.
//
// The counter is PER LOADER. A single shared counter is wrong here: refreshAll()
// starts all four loaders at once, so each one bumped the same counter and every
// response except the last-started loader's was discarded as stale.
const loadSeq: Record<string, number> = {}

function beginLoad(key: string): { stale: () => boolean } {
  const seq = (loadSeq[key] = (loadSeq[key] || 0) + 1)
  const wanted = tenantId.value
  return { stale: () => seq !== loadSeq[key] || tenantId.value !== wanted }
}

async function loadQuota() {
  if (!tenantId.value) return
  const guard = beginLoad('quota')
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/free-quota`)
    if (guard.stale()) return
    quota.value = data.data || null
  } catch {
    if (guard.stale()) return
    quota.value = null
  }
}

async function loadBoot() {
  if (!tenantId.value) return
  const guard = beginLoad('boot')
  const { data } = await api.get(`/tenants/${tenantId.value}/boot-volumes`, {
    params: { include_subcompartments: includeSub.value },
  })
  if (guard.stale()) return
  bootVolumes.value = data.data?.volumes || []
}

async function loadBlock() {
  if (!tenantId.value) return
  const guard = beginLoad('block')
  const { data } = await api.get(`/tenants/${tenantId.value}/block-volumes`, {
    params: { include_subcompartments: true },
  })
  if (guard.stale()) return
  blockVolumes.value = data.data?.volumes || []
  // Pre-fill AD from first volume or boot
  if (!createForm.availability_domain) {
    const ad =
      blockVolumes.value[0]?.availability_domain ||
      bootVolumes.value[0]?.availability_domain ||
      ''
    if (ad) createForm.availability_domain = ad
  }
}

async function loadBuckets() {
  if (!tenantId.value) return
  const guard = beginLoad('buckets')
  const { data } = await api.get(`/tenants/${tenantId.value}/object-storage/buckets`)
  if (guard.stale()) return
  buckets.value = data.data?.buckets || []
  objectNs.value = data.data?.namespace || ''
}

async function refreshAll() {
  if (!tenantId.value) return
  loading.value = true
  error.value = ''
  msg.value = ''
  try {
    await Promise.all([loadQuota(), loadBoot(), loadBlock(), loadBuckets()])
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onTenantChange() {
  activeBucket.value = ''
  objects.value = []
  // Clear previous tenant data; user must click 刷新 to hit OCI.
  bootVolumes.value = []
  blockVolumes.value = []
  buckets.value = []
  quota.value = null
  router.replace({ query: { ...route.query, tenant: tenantId.value, tab: tab.value } })
}

async function createBlock() {
  if (!createForm.availability_domain) {
    error.value = '请填写可用域'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/block-volumes`, { ...createForm })
    if (data.ok) {
      msg.value = data.message
      await loadBlock()
      await loadQuota()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '创建失败'
  } finally {
    busy.value = false
  }
}

async function deleteBlock(v: any) {
  if (!confirm(`删除块卷 ${v.display_name || v.id}？`)) return
  busy.value = true
  try {
    const { data } = await api.delete(`/tenants/${tenantId.value}/block-volumes/${v.id}`)
    if (data.ok) {
      msg.value = data.message
      await loadBlock()
      await loadQuota()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    busy.value = false
  }
}

async function attachBlock(v: any) {
  const instanceId = prompt('输入要挂载的实例 OCID（须 RUNNING）')
  if (!instanceId) return
  const type = prompt('挂载类型：PARAVIRTUALIZED 或 ISCSI', 'PARAVIRTUALIZED') || 'PARAVIRTUALIZED'
  busy.value = true
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/block-volumes/${v.id}/attach`, {
      instance_id: instanceId.trim(),
      type,
    })
    if (data.ok) {
      msg.value = data.message
      await loadBlock()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '挂载失败'
  } finally {
    busy.value = false
  }
}

async function detachBlock(v: any) {
  if (!v.attachment_id) return
  if (!confirm('卸载该块卷？')) return
  busy.value = true
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/block-volumes/detach`, {
      attachment_id: v.attachment_id,
    })
    if (data.ok) {
      msg.value = data.message
      await loadBlock()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '卸载失败'
  } finally {
    busy.value = false
  }
}

async function resizeBlock(v: any) {
  const raw = prompt(`新大小 GB（当前 ${v.size_in_gbs}，只能扩大）`, String(v.size_in_gbs + 50))
  if (!raw) return
  const size = Number(raw)
  if (!Number.isFinite(size) || size <= v.size_in_gbs) {
    error.value = '新大小必须大于当前'
    return
  }
  busy.value = true
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/block-volumes/${v.id}/update`, {
      size_in_gbs: size,
    })
    if (data.ok) {
      msg.value = data.message
      await loadBlock()
      await loadQuota()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '扩容失败'
  } finally {
    busy.value = false
  }
}

async function createBucket() {
  busy.value = true
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/object-storage/buckets`, {
      name: newBucket.value.trim(),
    })
    if (data.ok) {
      msg.value = data.message
      newBucket.value = ''
      await loadBuckets()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '创建失败'
  } finally {
    busy.value = false
  }
}

async function deleteBucket(name: string) {
  if (!confirm(`删除存储桶 ${name}？（须为空）`)) return
  busy.value = true
  try {
    const { data } = await api.delete(`/tenants/${tenantId.value}/object-storage/buckets/${encodeURIComponent(name)}`)
    if (data.ok) {
      msg.value = data.message
      if (activeBucket.value === name) {
        activeBucket.value = ''
        objects.value = []
      }
      await loadBuckets()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    busy.value = false
  }
}

async function openBucket(name: string) {
  activeBucket.value = name
  await loadObjects()
}

async function loadObjects() {
  if (!activeBucket.value) return
  busy.value = true
  try {
    const { data } = await api.get(
      `/tenants/${tenantId.value}/object-storage/buckets/${encodeURIComponent(activeBucket.value)}/objects`,
    )
    objects.value = data.data?.objects || []
  } catch (e: any) {
    error.value = e?.message || '列举对象失败'
  } finally {
    busy.value = false
  }
}

function onFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  uploadFile.value = input.files?.[0] || null
}

async function uploadObject() {
  if (!uploadFile.value || !activeBucket.value) return
  const fd = new FormData()
  fd.append('file', uploadFile.value)
  fd.append('object_name', uploadFile.value.name)
  busy.value = true
  try {
    const { data } = await api.post(
      `/tenants/${tenantId.value}/object-storage/buckets/${encodeURIComponent(activeBucket.value)}/objects`,
      fd,
    )
    if (data.ok) {
      msg.value = data.message
      uploadFile.value = null
      await loadObjects()
      await loadQuota()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '上传失败'
  } finally {
    busy.value = false
  }
}

async function deleteObject(name: string) {
  if (!confirm(`删除对象 ${name}？`)) return
  busy.value = true
  try {
    const { data } = await api.delete(
      `/tenants/${tenantId.value}/object-storage/buckets/${encodeURIComponent(activeBucket.value)}/objects/${encodeURIComponent(name)}`,
    )
    if (data.ok) {
      msg.value = data.message
      await loadObjects()
    } else error.value = data.message
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    busy.value = false
  }
}

watch(tab, (t) => {
  router.replace({ query: { ...route.query, tenant: tenantId.value, tab: t } })
})

onMounted(async () => {
  try {
    await loadTenants()
    // No automatic multi-API fan-out on enter; user clicks 刷新.
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.75rem;
}
.stat {
  padding: 0.75rem 1rem;
}
.stat-num {
  font-size: 1.15rem;
  font-weight: 700;
}
.tabs button {
  min-width: 88px;
}
@media (max-width: 900px) {
  .stats {
    grid-template-columns: 1fr;
  }
}
</style>
