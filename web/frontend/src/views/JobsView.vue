<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>任务中心</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          容量重试与定时开关机由后台 Worker 执行
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

    <div class="card stack">
      <div class="row" style="justify-content: space-between">
        <h3 style="margin: 0">定时开关机</h3>
        <button class="primary" @click="showSchedule = !showSchedule">
          {{ showSchedule ? '收起' : '新建' }}
        </button>
      </div>

      <div v-if="showSchedule" class="stack" style="border-top: 1px solid var(--border); padding-top: 0.75rem">
        <div class="grid-2">
          <div class="field">
            <label>名称</label>
            <input v-model="schedForm.name" />
          </div>
          <div class="field">
            <label>租户</label>
            <select v-model="schedForm.tenant_id">
              <option disabled value="">选择租户</option>
              <option v-for="t in tenants" :key="t.id" :value="t.id">{{ t.name }}</option>
            </select>
          </div>
          <div class="field">
            <label>类型</label>
            <select v-model="schedForm.kind">
              <option value="weekly">每周循环</option>
              <option value="once">一次性（指定时间）</option>
            </select>
          </div>
          <div class="field">
            <label>动作</label>
            <select v-model="schedForm.action">
              <option value="SOFTSTOP">正常关机</option>
              <option value="STOP">强制关机</option>
              <option value="START">开机</option>
              <option value="SOFTRESET">正常重启</option>
              <option value="RESET">强制重启</option>
            </select>
          </div>
          <div v-if="schedForm.kind === 'weekly'" class="field">
            <label>时间（本地 HH:MM）</label>
            <input v-model="schedForm.time_of_day" placeholder="22:00" />
          </div>
          <div v-else class="field">
            <label>执行时间</label>
            <input v-model="schedForm.run_at_local" type="datetime-local" />
          </div>
        </div>
        <div v-if="schedForm.kind === 'weekly'" class="field">
          <label>星期</label>
          <div class="choice-group">
            <label v-for="(w, i) in WEEKDAYS" :key="i" class="choice muted">
              <input v-model="schedForm.weekdays" type="checkbox" :value="i" />
              <span>{{ w }}</span>
            </label>
          </div>
        </div>
        <p class="muted" style="font-size: 12px; margin: 0">
          实例列表为空表示该租户下全部非终止实例。一次性任务执行后自动停用。
        </p>
        <button class="primary" :disabled="saving" @click="createSchedule">创建定时任务</button>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>时间</th>
              <th>动作</th>
              <th>上次执行</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="schedules.length === 0">
              <td colspan="6" class="muted">暂无定时任务</td>
            </tr>
            <tr v-for="s in schedules" :key="s.id">
              <td>{{ s.name }}</td>
              <td>{{ s.kind === 'once' ? '一次性' : '每周' }}</td>
              <td class="muted" style="font-size: 12px">
                <template v-if="s.kind === 'once'">{{ fmt(s.run_at) }}</template>
                <template v-else>{{ s.time_of_day }} · 周{{ (s.weekdays || []).map(wd).join('/') }}</template>
              </td>
              <td>{{ s.action }}</td>
              <td>{{ s.last_run_date || '—' }}</td>
              <td>
                <button class="danger" @click="deleteSchedule(s)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <details>
        <summary class="muted" style="cursor: pointer">执行历史（最近 {{ runs.length }} 条）</summary>
        <div class="table-wrap" style="margin-top: 0.5rem">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>任务</th>
                <th>动作</th>
                <th>结果</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="runs.length === 0">
                <td colspan="5" class="muted">暂无执行记录</td>
              </tr>
              <tr v-for="r in runs" :key="r.id">
                <td class="muted" style="font-size: 12px">{{ fmt(r.created_at) }}</td>
                <td>{{ r.job_name }}</td>
                <td>{{ r.action }}</td>
                <td>
                  <span class="badge" :class="r.ok ? 'running' : 'err'">
                    {{ r.success_count }}/{{ r.instance_count }}
                  </span>
                </td>
                <td class="muted" style="font-size: 12px; max-width: 320px; word-break: break-all">
                  {{ r.message }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import api, { type CapacityJob, type ScheduleJob, type Tenant } from '@/api/client'

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

type Run = {
  id: string
  job_name: string
  action: string
  ok: boolean
  instance_count: number
  success_count: number
  message: string
  created_at: string
}

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

const capacityJobs = ref<CapacityJob[]>([])
const schedules = ref<ScheduleJob[]>([])
const runs = ref<Run[]>([])
const tenants = ref<Tenant[]>([])
const attempts = ref<Record<string, Attempt[]>>({})
const openLog = ref('')
const autoRefresh = ref(true)
const error = ref('')
const msg = ref('')
const showSchedule = ref(false)
const saving = ref(false)
let timer: number | undefined

const schedForm = reactive({
  name: '工作日关机',
  tenant_id: '',
  kind: 'weekly',
  time_of_day: '22:00',
  run_at_local: '',
  action: 'SOFTSTOP',
  weekdays: [0, 1, 2, 3, 4] as number[],
})

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
  const [c, s, t, r] = await Promise.all([
    api.get<CapacityJob[]>('/jobs/capacity'),
    api.get<ScheduleJob[]>('/jobs/schedules'),
    api.get<Tenant[]>('/tenants'),
    api.get<Run[]>('/jobs/schedules/runs', { params: { limit: 50 } }),
  ])
  capacityJobs.value = c.data
  schedules.value = s.data
  tenants.value = t.data
  runs.value = r.data
  if (!schedForm.tenant_id && t.data[0]) schedForm.tenant_id = t.data[0].id
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
  // change during retries — no need to re-pull tenants/schedules/runs every 5s.
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

async function createSchedule() {
  error.value = ''
  saving.value = true
  try {
    const body: Record<string, unknown> = {
      name: schedForm.name,
      tenant_id: schedForm.tenant_id,
      kind: schedForm.kind,
      action: schedForm.action,
      enabled: true,
      instance_ids: [],
    }
    if (schedForm.kind === 'weekly') {
      body.time_of_day = schedForm.time_of_day
      body.weekdays = schedForm.weekdays
    } else {
      if (!schedForm.run_at_local) throw new Error('请选择执行时间')
      body.run_at = new Date(schedForm.run_at_local).toISOString()
    }
    await api.post('/jobs/schedules', body)
    msg.value = '定时任务已创建'
    showSchedule.value = false
    await load()
  } catch (e: any) {
    error.value = e?.message || '创建失败'
  } finally {
    saving.value = false
  }
}

async function deleteSchedule(s: ScheduleJob) {
  if (!confirm(`删除「${s.name}」？`)) return
  error.value = ''
  try {
    await api.delete(`/jobs/schedules/${s.id}`)
    msg.value = '已删除'
    await load()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  }
}

onMounted(async () => {
  try {
    await load()
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
