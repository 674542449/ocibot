<template>
  <div class="stack">
    <div>
      <h2 style="margin: 0">用户管理 · 系统更新</h2>
      <p class="muted" style="margin: 0.2rem 0 0">仅管理员可见。第一位注册的用户自动成为管理员。</p>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box" style="white-space: pre-wrap">{{ msg }}</div>

    <!-- System update first — most asked-for admin action -->
    <div class="card stack update-card">
      <div class="row" style="justify-content: space-between; align-items: flex-start; gap: 0.75rem">
        <div style="min-width: 0; flex: 1">
          <h3 style="margin: 0">🔄 系统更新</h3>
          <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
            从 GitHub 拉取最新代码并重建 Docker 容器（保留数据库与密钥）。
          </p>
        </div>
        <div class="row update-actions">
          <button
            type="button"
            class="primary"
            style="min-width: 7rem; font-weight: 700"
            :disabled="updateBusy || updateRunning"
            @click="checkUpdate"
          >
            {{ updateBusy && !updateRunning ? '检查中…' : '检查更新' }}
          </button>
          <button
            type="button"
            class="primary"
            style="min-width: 7rem"
            :disabled="updateBusy || updateRunning || !canApplyUpdate"
            @click="applyUpdate"
          >
            {{ updateRunning ? '更新中…' : '一键更新' }}
          </button>
        </div>
      </div>

      <div v-if="!updateInfo" class="muted" style="font-size: 13px">点击「检查更新」获取版本信息。</div>
      <template v-else>
        <div class="grid-2" style="font-size: 13px">
          <div>
            <div class="muted" style="font-size: 12px">当前版本</div>
            <div>
              <code>{{ updateInfo.local?.git_sha || 'unknown' }}</code>
              <span class="muted"> · app {{ updateInfo.local?.app_version || '—' }}</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size: 12px">
              远程
              <a
                v-if="updateInfo.remote?.html_url"
                :href="updateInfo.remote.html_url"
                target="_blank"
                rel="noopener"
                >GitHub</a
              >
            </div>
            <div>
              <code>{{ updateInfo.remote?.short_sha || updateInfo.remote?.sha?.slice?.(0, 7) || '—' }}</code>
              <span v-if="updateInfo.update_available" class="badge warn" style="margin-left: 0.35rem"
                >有新版本</span
              >
              <span
                v-else-if="updateInfo.remote?.sha"
                class="badge running"
                style="margin-left: 0.35rem"
                >已是最新</span
              >
            </div>
            <div class="muted" style="font-size: 12px; margin-top: 0.2rem">
              {{ updateInfo.remote?.message || '' }}
            </div>
          </div>
        </div>

        <div class="row" style="font-size: 12px">
          <span class="badge" :class="updateStateClass">状态：{{ updateStateLabel }}</span>
          <span v-if="updateInfo.message" class="muted">{{ updateInfo.message }}</span>
        </div>

        <div v-if="!updateInfo.capabilities?.can_apply" class="error-box" style="font-size: 13px">
          当前环境无法在线更新（需要 Docker 部署并挂载宿主机仓库与 docker.sock）。
          请 SSH 执行：
          <code>cd ~/ocibot && bash scripts/install.sh update</code>
          <div class="muted" style="margin-top: 0.35rem; font-size: 12px">
            enabled={{ updateInfo.capabilities?.enabled }} · host_dir={{
              updateInfo.capabilities?.host_dir_exists
            }}
            · compose={{ updateInfo.capabilities?.compose_file_exists }} · sock={{
              updateInfo.capabilities?.docker_sock
            }}
            · docker={{ updateInfo.capabilities?.docker_bin }} · git={{
              updateInfo.capabilities?.git_bin
            }}
          </div>
        </div>

        <details v-if="updateInfo.log_tail" style="font-size: 12px">
          <summary class="muted">更新日志</summary>
          <pre
            class="muted"
            style="
              max-height: 240px;
              overflow: auto;
              white-space: pre-wrap;
              background: var(--panel-2);
              padding: 0.6rem;
              border-radius: 8px;
            "
            >{{ updateInfo.log_tail }}</pre
          >
        </details>
      </template>
    </div>

    <div class="card stack">
      <h3 style="margin: 0">面板设置</h3>
      <div class="row">
        <label class="row" style="gap: 0.4rem">
          <input
            v-model="allowOpenRegistration"
            type="checkbox"
            style="width: auto"
            @change="saveSettings"
          />
          允许开放注册
        </label>
        <span class="muted" style="font-size: 12px">
          （来源：{{ settingsSource === 'db' ? '面板设置' : '环境变量默认值' }}）关闭后仅现有用户可登录
        </span>
      </div>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>用户名</th>
            <th>角色</th>
            <th>两步验证</th>
            <th>租户数</th>
            <th>状态</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td style="font-weight: 600">{{ u.username }}</td>
            <td>
              <span class="badge" :class="u.is_admin ? 'warn' : ''">{{ u.is_admin ? '管理员' : '用户' }}</span>
            </td>
            <td>{{ u.totp_enabled ? '✅' : '—' }}</td>
            <td>{{ u.tenant_count }}</td>
            <td>
              <span class="badge" :class="u.is_active ? 'running' : 'err'">
                {{ u.is_active ? '正常' : '已禁用' }}
              </span>
            </td>
            <td class="muted" style="font-size: 12px">{{ fmt(u.created_at) }}</td>
            <td>
              <div class="row">
                <button v-if="u.id !== meId" @click="toggleActive(u)">
                  {{ u.is_active ? '禁用' : '启用' }}
                </button>
                <button @click="resetPassword(u)">重置密码</button>
                <button v-if="u.id !== meId" @click="toggleAdmin(u)">
                  {{ u.is_admin ? '取消管理员' : '设为管理员' }}
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

type AdminUser = {
  id: string
  username: string
  is_active: boolean
  is_admin: boolean
  totp_enabled: boolean
  tenant_count: number
  created_at: string
}

const auth = useAuthStore()
const users = ref<AdminUser[]>([])
const error = ref('')
const msg = ref('')
const allowOpenRegistration = ref(true)
const settingsSource = ref('env')
const meId = ref('')

const updateInfo = ref<any>(null)
const updateBusy = ref(false)
let updatePollTimer: number | undefined

const updateRunning = computed(() => updateInfo.value?.state === 'running')
const canApplyUpdate = computed(() => !!updateInfo.value?.capabilities?.can_apply)

const updateStateLabel = computed(() => {
  const s = updateInfo.value?.state || 'idle'
  return (
    (
      {
        idle: '空闲',
        checking: '检查中',
        running: '更新中',
        success: '成功',
        error: '失败',
      } as Record<string, string>
    )[s] || s
  )
})

const updateStateClass = computed(() => {
  const s = updateInfo.value?.state || 'idle'
  if (s === 'success') return 'running'
  if (s === 'error') return 'err'
  if (s === 'running') return 'warn'
  return ''
})

function fmt(v: string) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

function stopUpdatePoll() {
  if (updatePollTimer) {
    window.clearInterval(updatePollTimer)
    updatePollTimer = undefined
  }
}

function startUpdatePoll() {
  stopUpdatePoll()
  updatePollTimer = window.setInterval(async () => {
    try {
      const { data } = await api.get('/admin/update')
      updateInfo.value = data
      if (data.state !== 'running') {
        stopUpdatePoll()
        updateBusy.value = false
        if (data.state === 'success') {
          msg.value = data.message || '更新完成，请强制刷新页面（Ctrl+F5）'
        }
        if (data.state === 'error') {
          error.value = data.message || data.last_error || '更新失败'
        }
      }
    } catch {
      /* ignore transient errors while containers restart */
    }
  }, 3000)
}

async function loadUpdate() {
  try {
    const { data } = await api.get('/admin/update')
    updateInfo.value = data
    if (data.state === 'running') {
      updateBusy.value = true
      startUpdatePoll()
    }
  } catch {
    updateInfo.value = null
  }
}

async function checkUpdate() {
  error.value = ''
  msg.value = ''
  updateBusy.value = true
  try {
    const { data } = await api.post('/admin/update/check')
    updateInfo.value = data
    if (data.update_available) {
      msg.value = `发现新版本 ${data.remote?.short_sha || ''}：${data.remote?.message || ''}`
    } else {
      msg.value = '已是最新版本（或无法精确比对本地 commit）'
    }
  } catch (e: any) {
    error.value = e?.message || '检查更新失败'
    await loadUpdate()
  } finally {
    updateBusy.value = false
  }
}

async function applyUpdate() {
  if (
    !confirm(
      '确认从 GitHub 拉取最新代码并重建容器？\n\n会短暂中断面板访问；数据库与 web/.env 密钥会保留。',
    )
  ) {
    return
  }
  error.value = ''
  msg.value = ''
  updateBusy.value = true
  try {
    const { data } = await api.post('/admin/update/apply')
    updateInfo.value = data
    msg.value = '更新已开始，请稍候…页面可能短暂无法访问。'
    startUpdatePoll()
  } catch (e: any) {
    updateBusy.value = false
    error.value = e?.message || '无法启动更新'
  }
}

async function load() {
  const [u, s, me] = await Promise.all([
    api.get<AdminUser[]>('/admin/users'),
    api.get('/admin/settings'),
    api.get('/auth/me'),
  ])
  users.value = u.data
  allowOpenRegistration.value = !!s.data.allow_open_registration
  settingsSource.value = s.data.source
  meId.value = me.data.id
  await loadUpdate()
}

async function saveSettings() {
  error.value = ''
  const attempted = allowOpenRegistration.value
  try {
    const { data } = await api.put('/admin/settings', {
      allow_open_registration: attempted,
    })
    settingsSource.value = data.source
    allowOpenRegistration.value = !!data.allow_open_registration
    msg.value = data.allow_open_registration ? '已允许开放注册' : '已关闭开放注册'
  } catch (e: any) {
    allowOpenRegistration.value = !attempted // revert the optimistic v-model toggle
    error.value = e?.message || '保存失败'
  }
}

async function toggleActive(u: AdminUser) {
  const verb = u.is_active ? '禁用' : '启用'
  if (!confirm(`${verb}用户「${u.username}」？禁用后其所有会话立即失效。`)) return
  error.value = ''
  try {
    await api.patch(`/admin/users/${u.id}`, { is_active: !u.is_active })
    msg.value = `已${verb}「${u.username}」`
    await load()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  }
}

async function toggleAdmin(u: AdminUser) {
  const verb = u.is_admin ? '取消管理员' : '设为管理员'
  if (!confirm(`确认将「${u.username}」${verb}？`)) return
  error.value = ''
  try {
    await api.patch(`/admin/users/${u.id}`, { is_admin: !u.is_admin })
    msg.value = `已${verb}「${u.username}」`
    await load()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  }
}

async function resetPassword(u: AdminUser) {
  if (!confirm(`为「${u.username}」生成新的随机密码？其所有会话将失效，已开启的两步验证也会被清除。`)) return
  error.value = ''
  try {
    const { data } = await api.post(`/admin/users/${u.id}/reset-password`)
    msg.value = `「${data.username}」的新密码：${data.new_password}\n${data.message}`
    if (u.id === meId.value) {
      // Own password reset revokes our token; back to login.
      auth.clearLocal()
      location.href = '/login'
      return
    }
    await load()
  } catch (e: any) {
    error.value = e?.message || '重置失败'
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e: any) {
    error.value = e?.message || '加载失败（需要管理员权限）'
  }
})

onBeforeUnmount(() => {
  stopUpdatePoll()
})
</script>

<style scoped>
.update-card {
  border: 1px solid var(--accent-soft-2);
  box-shadow: var(--shadow-sm);
  background: linear-gradient(180deg, var(--accent-soft) 0%, var(--panel) 48%);
}
.update-actions {
  flex-shrink: 0;
}
@media (max-width: 700px) {
  .update-actions {
    width: 100%;
  }
  .update-actions button {
    flex: 1;
  }
}
</style>
