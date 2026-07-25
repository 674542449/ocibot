<template>
  <div class="stack">
    <div>
      <h2 style="margin: 0">用户管理</h2>
      <p class="muted" style="margin: 0.2rem 0 0">仅管理员可见。第一位注册的用户自动成为管理员。</p>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box" style="white-space: pre-wrap">{{ msg }}</div>

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
import { onMounted, ref } from 'vue'
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

function fmt(v: string) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
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
}

async function saveSettings() {
  error.value = ''
  try {
    const { data } = await api.put('/admin/settings', {
      allow_open_registration: allowOpenRegistration.value,
    })
    settingsSource.value = data.source
    msg.value = data.allow_open_registration ? '已允许开放注册' : '已关闭开放注册'
  } catch (e: any) {
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
</script>
