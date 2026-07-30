<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>任务中心</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          容量重试由后台 Worker 执行
        </p>
      </div>
      <div class="page-tools">
        <label class="choice muted" style="flex: 0 0 auto">
          <input v-model="autoRefresh" type="checkbox" />
          <span>自动刷新</span>
        </label>
        <button @click="load">刷新</button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <!-- The jobs on this page are exactly what the switch stops. Saying it here
         means a task sitting at "idle" forever is explainable. -->
    <div v-if="backgroundOff" class="card bg-off-box">
      <strong>⚠ 后台 OCI 请求已关闭，本页任务不会执行</strong>
      <p class="muted" style="margin: 0.3rem 0 0; font-size: 12px">
        任务可以照常创建和保存，但 Worker 不会去调用 Oracle，抢机不会触发。
        要恢复：在 <code>web/.env</code> 里设 <code>OCIBOT_WORKER_BACKGROUND_OCI=1</code>
        后执行 <code>docker compose up -d</code>。
      </p>
    </div>

    <div class="card stack">
      <h3 style="margin: 0">容量重试</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>状态</th>
              <th>进度</th>
              <th>配置轮询</th>
              <th>下次运行</th>
              <th>最近结果</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="capacityJobs.length === 0">
              <td colspan="7" class="muted">
                暂无容量重试。可在「创建实例」勾选容量不足自动重试，成功后会显示在这里。
              </td>
            </tr>
            <template v-for="j in capacityJobs" :key="j.id">
              <tr>
                <td>{{ j.name }}</td>
                <td>
                  <span class="badge" :class="jobBadge(j.status)">{{ statusLabel(j.status) }}</span>
                </td>
                <td>{{ j.attempts }} / {{ j.max_attempts }} · {{ j.interval_sec }}s</td>
                <td class="muted" style="font-size: 12px">
                  {{ configSummary(j) }}
                </td>
                <td class="muted" style="font-size: 12px">{{ fmt(j.next_run_at) }}</td>
                <td class="muted" style="font-size: 12px; max-width: 220px">
                  {{ j.last_error || (j.success_instance_id ? 'OK ' + j.success_instance_id.slice(-12) : '—') }}
                </td>
                <td>
                  <div class="row">
                    <button @click="toggleLog(j)">{{ openLog === j.id ? '收起日志' : '日志' }}</button>
                    <button v-if="j.enabled" @click="stopJob(j)">停止</button>
                    <button v-else-if="j.status !== 'success'" @click="resumeJob(j)">继续</button>
                    <button class="danger" @click="deleteJob(j)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="openLog === j.id">
                <td colspan="7" style="background: var(--input-bg)">
                  <div class="attempt-log">
                    <div v-if="!(attempts[j.id] || []).length" class="muted">
                      暂无尝试记录（Worker 每 {{ j.interval_sec }}s 尝试一次）
                    </div>
                    <div v-for="a in attempts[j.id] || []" :key="a.id" class="attempt-line">
                      <span class="muted">#{{ a.n }} {{ fmt(a.created_at) }}</span>
                      <span v-if="a.config_label" class="badge">{{ a.config_label }}</span>
                      <span v-if="a.availability_domain" class="muted">AD…{{ a.availability_domain.slice(-6) }}</span>
                      <span
                        class="badge"
                        :class="a.ok ? 'running' : a.rate_limited ? 'warn' : a.capacity ? '' : 'err'"
                      >
                        {{ a.ok ? '成功' : a.rate_limited ? '429 限流' : a.capacity ? '无容量' : '失败' }}
                      </span>
                      <span style="word-break: break-all">{{ a.message }}</span>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import api, { type CapacityJob, type Tenant } from '@/api/client'

type Attempt = {
  id: string
  n: number
  seq: number
  ok: boolean
  capacity: boolean
  rate_limited: boolean
  message: string
  availability_domain: string
  config_label: string
  created_at: string
}


const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

const capacityJobs = ref<CapacityJob[]>([])
const tenants = ref<Tenant[]>([])
const attempts = ref<Record<string, Attempt[]>>({})
const openLog = ref('')
/** 后台 OCI 是否被关掉；关掉时本页的任务永远不会跑，必须说清楚。 */
const backgroundOff = ref(false)

async function checkBackground() {
  try {
    const { data } = await api.get('/system/status')
    backgroundOff.value = data.background_oci === false
  } catch {
    // 状态读不到不是本页的重点，静默即可
  }
}

const autoRefresh = ref(false)
const error = ref('')
const msg = ref('')
const saving = ref(false)
let timer: number | undefined


function wd(i: number) {
  return WEEKDAYS[i] ?? String(i)
}

function jobBadge(status: string) {
  if (status === 'success') return 'running'
  if (status === 'failed' || status === 'stopped') return 'err'
  if (status === 'running') return 'warn'
  return ''
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    idle: '等待中',
    running: '尝试中',
    success: '已成功',
    stopped: '已停止',
    failed: '已失败',
  }
  return map[status] || status
}

function configSummary(j: CapacityJob) {
  const p = j.launch_payload || {}
  const parts: string[] = []
  if (p.ocpus != null && p.memory_in_gbs != null) parts.push(`${p.ocpus}C/${p.memory_in_gbs}G`)
  for (const fb of j.fallback_configs || []) {
    parts.push(`${(fb as any).ocpus}C/${(fb as any).memory_in_gbs}G`)
  }
  const label = parts.length ? parts.join(' → ') : '固定配置'
  return label + (j.has_user_data ? ' · 含启动脚本' : '')
}

function fmt(v: string | null | undefined) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

async function load() {
  error.value = ''
  const [jobs, tenantList] = await Promise.all([
    api.get<CapacityJob[]>('/jobs/capacity'),
    api.get<Tenant[]>('/tenants'),
  ])
  capacityJobs.value = jobs.data
  tenants.value = tenantList.data
  if (openLog.value) await loadAttempts(openLog.value)
}

async function loadAttempts(jobId: string) {
  try {
    const { data } = await api.get<Attempt[]>(`/jobs/capacity/${jobId}/attempts`, {
      params: { limit: 100 },
    })
    attempts.value = { ...attempts.value, [jobId]: [...data].reverse() }
  } catch {
    // job may have been deleted
  }
}

async function toggleLog(j: CapacityJob) {
  if (openLog.value === j.id) {
    openLog.value = ''
    return
  }
  openLog.value = j.id
  await loadAttempts(j.id)
}

async function refreshCapacity() {
  // Lightweight tick for the auto-refresh: only capacity jobs (and the open log)
  // change during retries — no need to re-pull tenants every 5s.
  const { data } = await api.get<CapacityJob[]>('/jobs/capacity')
  capacityJobs.value = data
  if (openLog.value) await loadAttempts(openLog.value)
}

async function stopJob(j: CapacityJob) {
  error.value = ''
  try {
    await api.post(`/jobs/capacity/${j.id}/stop`)
    msg.value = '已停止'
    await load()
  } catch (e: any) {
    error.value = e?.message || '停止失败'
  }
}

async function resumeJob(j: CapacityJob) {
  error.value = ''
  try {
    await api.post(`/jobs/capacity/${j.id}/resume`)
    msg.value = '已继续'
    await load()
  } catch (e: any) {
    error.value = e?.message || '继续失败'
  }
}

async function deleteJob(j: CapacityJob) {
  if (!confirm(`删除任务「${j.name}」？`)) return
  error.value = ''
  try {
    await api.delete(`/jobs/capacity/${j.id}`)
    msg.value = '已删除'
    if (openLog.value === j.id) openLog.value = ''
    await load()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  }
}

onMounted(async () => {
  try {
    await load()
    void checkBackground()
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
  timer = window.setInterval(async () => {
    if (!autoRefresh.value) return
    const active = capacityJobs.value.some((j) => j.enabled)
    if (active || openLog.value) {
      try {
        await refreshCapacity()
      } catch {
        // transient errors during background refresh are non-fatal
      }
    }
  }, 5_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.bg-off-box {
  padding: 0.65rem 0.8rem;
  border-color: var(--warn);
}
.bg-off-box code {
  font-size: 11px;
}
.attempt-log {
  max-height: 320px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 12.5px;
  font-family: Consolas, monospace;
}
.attempt-line {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  flex-wrap: wrap;
}
</style>
