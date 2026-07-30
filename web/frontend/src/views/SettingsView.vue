<template>
  <div class="stack">
    <div>
      <h2 style="margin: 0">设置</h2>
      <p class="muted" style="margin: 0.2rem 0 0">通知推送与账号安全</p>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <!-- ============ 通知渠道 ============ -->
    <div class="card stack">
      <div class="row" style="justify-content: space-between">
        <div>
          <h3 style="margin: 0">通知推送</h3>
          <p class="muted" style="margin: 0.2rem 0 0; font-size: 12px">
            抢机成功/失败、定时任务失败、预算超额时推送。支持 Telegram / Bark / Server酱 / Webhook / 邮件。
          </p>
        </div>
        <button class="primary" @click="showAdd = !showAdd">{{ showAdd ? '收起' : '添加渠道' }}</button>
      </div>

      <div v-if="showAdd" class="stack" style="border-top: 1px solid var(--border); padding-top: 0.75rem">
        <div class="grid-2">
          <div class="field">
            <label>类型</label>
            <select v-model="addForm.kind">
              <option value="telegram">Telegram Bot</option>
              <option value="bark">Bark（iOS）</option>
              <option value="serverchan">Server酱</option>
              <option value="webhook">Webhook（POST JSON）</option>
              <option value="smtp">邮件（SMTP）</option>
            </select>
          </div>
          <div class="field">
            <label>名称（可选）</label>
            <input v-model="addForm.name" placeholder="如 我的 TG" />
          </div>
        </div>

        <div v-if="addForm.kind === 'telegram'" class="grid-2">
          <div class="field">
            <label>Bot Token</label>
            <input v-model="addConfig.bot_token" placeholder="123456:ABC-DEF..." />
          </div>
          <div class="field">
            <label>Chat ID</label>
            <input v-model="addConfig.chat_id" placeholder="通过 @userinfobot 获取" />
          </div>
        </div>
        <div v-else-if="addForm.kind === 'bark'" class="grid-2">
          <div class="field">
            <label>Device Key</label>
            <input v-model="addConfig.device_key" />
          </div>
          <div class="field">
            <label>服务器（可选，默认官方）</label>
            <input v-model="addConfig.server" placeholder="https://api.day.app" />
          </div>
        </div>
        <div v-else-if="addForm.kind === 'serverchan'" class="field">
          <label>SendKey</label>
          <input v-model="addConfig.send_key" placeholder="SCT..." />
        </div>
        <div v-else-if="addForm.kind === 'webhook'" class="grid-2">
          <div class="field">
            <label>URL</label>
            <input v-model="addConfig.url" placeholder="https://..." />
          </div>
          <div class="field">
            <label>Secret 头（可选，X-OCIBot-Secret）</label>
            <input v-model="addConfig.secret" />
          </div>
        </div>
        <div v-else-if="addForm.kind === 'smtp'" class="grid-2">
          <div class="field"><label>SMTP 服务器</label><input v-model="addConfig.host" placeholder="smtp.example.com" /></div>
          <div class="field"><label>端口</label><input v-model="addConfig.port" placeholder="465" /></div>
          <div class="field"><label>用户名</label><input v-model="addConfig.username" /></div>
          <div class="field"><label>密码 / 授权码</label><input v-model="addConfig.password" type="password" /></div>
          <div class="field"><label>收件邮箱</label><input v-model="addConfig.to_addr" /></div>
          <div class="field"><label>发件地址（可选，默认用户名）</label><input v-model="addConfig.from_addr" /></div>
        </div>

        <div class="field">
          <label>推送事件</label>
          <div class="choice-group">
            <label v-for="ev in EVENTS" :key="ev.key" class="choice muted">
              <input v-model="addEvents" type="checkbox" :value="ev.key" />
              <span>{{ ev.label }}</span>
            </label>
          </div>
        </div>
        <button class="primary" :disabled="saving" @click="createChannel">保存渠道</button>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>配置</th>
              <th>事件</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="channels.length === 0">
              <td colspan="6" class="muted">尚未配置任何通知渠道。强烈建议配置——抢机成功后可第一时间收到推送。</td>
            </tr>
            <tr v-for="c in channels" :key="c.id">
              <td>{{ c.name }}</td>
              <td>{{ kindLabel(c.kind) }}</td>
              <td class="muted" style="font-size: 12px; max-width: 240px; word-break: break-all">{{ c.config_hint }}</td>
              <td class="muted" style="font-size: 12px">{{ c.events.map(eventLabel).join(' / ') }}</td>
              <td><span class="badge" :class="c.enabled ? 'running' : ''">{{ c.enabled ? '启用' : '停用' }}</span></td>
              <td>
                <div class="row">
                  <button :disabled="testing === c.id" @click="testChannel(c)">
                    {{ testing === c.id ? '发送中…' : '测试' }}
                  </button>
                  <button @click="toggleChannel(c)">{{ c.enabled ? '停用' : '启用' }}</button>
                  <button class="danger" @click="deleteChannel(c)">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ============ 账号安全 ============ -->
    <div class="card stack">
      <h3 style="margin: 0">修改面板密码</h3>
      <div class="grid-2">
        <div class="field">
          <label>当前密码</label>
          <input v-model="pwdForm.old" type="password" autocomplete="current-password" />
        </div>
        <div class="field">
          <label>新密码（至少 8 位）</label>
          <input v-model="pwdForm.next" type="password" autocomplete="new-password" />
        </div>
      </div>
      <div class="row">
        <button class="primary" :disabled="saving || !pwdForm.old || pwdForm.next.length < 8" @click="changePassword">
          修改密码
        </button>
        <span class="muted" style="font-size: 12px">修改后其他设备会全部退出登录</span>
      </div>
    </div>

    <div class="card stack">
      <div class="row" style="justify-content: space-between">
        <div>
          <h3 style="margin: 0">两步验证（TOTP）</h3>
          <p class="muted" style="margin: 0.2rem 0 0; font-size: 12px">
            面板里保存着所有租户的 OCI API 私钥，强烈建议开启。支持 Google Authenticator / Microsoft Authenticator / 1Password 等。
          </p>
        </div>
        <span class="badge" :class="auth.totpEnabled ? 'running' : ''">
          {{ auth.totpEnabled ? '已开启' : '未开启' }}
        </span>
      </div>

      <template v-if="!auth.totpEnabled">
        <button v-if="!totpSetup" class="primary" style="align-self: flex-start" @click="startTotp">
          开始设置
        </button>
        <div v-else class="stack">
          <p style="margin: 0">
            1）在认证器 App 中「手动输入密钥」添加账号，密钥：
            <code class="totp-secret">{{ totpSetup.secret }}</code>
          </p>
          <p class="muted" style="margin: 0; font-size: 12px; word-break: break-all">
            或将此链接粘贴到支持 otpauth 的应用：{{ totpSetup.otpauth_url }}
          </p>
          <div class="row">
            <input v-model="totpCode" placeholder="输入 6 位验证码确认" style="width: 200px" />
            <button class="primary" :disabled="saving" @click="enableTotp">确认开启</button>
          </div>
        </div>
      </template>
      <template v-else>
        <div class="row">
          <input v-model="totpDisablePwd" type="password" placeholder="面板密码" style="width: 180px" />
          <input v-model="totpCode" placeholder="6 位验证码" style="width: 140px" />
          <button class="danger" :disabled="saving" @click="disableTotp">关闭两步验证</button>
        </div>
      </template>
    </div>

    <div class="card stack">
      <h3 style="margin: 0">会话管理</h3>
      <div class="row">
        <button class="danger" @click="logoutAll">在所有设备退出登录</button>
        <span class="muted" style="font-size: 12px">吊销所有已签发的登录令牌（包括当前设备）</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

type Channel = {
  id: string
  kind: string
  name: string
  enabled: boolean
  events: string[]
  config_hint: string
}

const auth = useAuthStore()
const router = useRouter()

const EVENTS = [
  { key: 'capacity', label: '抢机结果' },
]

const channels = ref<Channel[]>([])
const error = ref('')
const msg = ref('')
const saving = ref(false)
const testing = ref('')
const showAdd = ref(false)

const addForm = reactive({ kind: 'telegram', name: '' })
const addConfig = reactive<Record<string, string>>({})
const addEvents = ref<string[]>(EVENTS.map((e) => e.key))

const pwdForm = reactive({ old: '', next: '' })
const totpSetup = ref<{ secret: string; otpauth_url: string } | null>(null)
const totpCode = ref('')
const totpDisablePwd = ref('')

function kindLabel(kind: string) {
  const map: Record<string, string> = {
    telegram: 'Telegram',
    bark: 'Bark',
    serverchan: 'Server酱',
    webhook: 'Webhook',
    smtp: '邮件',
  }
  return map[kind] || kind
}

function eventLabel(key: string) {
  return EVENTS.find((e) => e.key === key)?.label || key
}

async function load() {
  const { data } = await api.get<Channel[]>('/notifications')
  channels.value = data
}

async function createChannel() {
  error.value = ''
  msg.value = ''
  saving.value = true
  try {
    const config: Record<string, unknown> = { ...addConfig }
    if (addForm.kind === 'smtp' && config.port) config.port = Number(config.port)
    await api.post('/notifications', {
      kind: addForm.kind,
      name: addForm.name,
      config,
      events: addEvents.value,
      enabled: true,
    })
    msg.value = '渠道已保存，建议点「测试」确认可以收到'
    showAdd.value = false
    Object.keys(addConfig).forEach((k) => delete addConfig[k])
    await load()
  } catch (e: any) {
    error.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

async function testChannel(c: Channel) {
  error.value = ''
  msg.value = ''
  testing.value = c.id
  try {
    const { data } = await api.post(`/notifications/${c.id}/test`)
    if (data.ok) msg.value = `测试消息已发送到「${c.name}」，请查收`
    else error.value = `发送失败：${data.detail}`
  } catch (e: any) {
    error.value = e?.message || '测试失败'
  } finally {
    testing.value = ''
  }
}

async function toggleChannel(c: Channel) {
  error.value = ''
  try {
    await api.patch(`/notifications/${c.id}`, { enabled: !c.enabled })
    await load()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  }
}

async function deleteChannel(c: Channel) {
  if (!confirm(`删除渠道「${c.name}」？`)) return
  error.value = ''
  try {
    await api.delete(`/notifications/${c.id}`)
    msg.value = '已删除'
    await load()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  }
}

async function changePassword() {
  error.value = ''
  msg.value = ''
  saving.value = true
  try {
    const { data } = await api.post('/auth/change-password', {
      old_password: pwdForm.old,
      new_password: pwdForm.next,
    })
    auth.setSession(data)
    pwdForm.old = ''
    pwdForm.next = ''
    msg.value = '密码已修改；其他设备已全部退出'
  } catch (e: any) {
    error.value = e?.message || '修改失败'
  } finally {
    saving.value = false
  }
}

async function startTotp() {
  error.value = ''
  try {
    const { data } = await api.post('/auth/totp/setup')
    totpSetup.value = data
  } catch (e: any) {
    error.value = e?.message || '生成失败'
  }
}

async function enableTotp() {
  error.value = ''
  saving.value = true
  try {
    await api.post('/auth/totp/enable', { code: totpCode.value.trim() })
    msg.value = '两步验证已开启，下次登录需要输入验证码'
    totpSetup.value = null
    totpCode.value = ''
    await auth.refreshMe()
  } catch (e: any) {
    error.value = e?.message || '开启失败'
  } finally {
    saving.value = false
  }
}

async function disableTotp() {
  error.value = ''
  saving.value = true
  try {
    await api.post('/auth/totp/disable', {
      password: totpDisablePwd.value,
      code: totpCode.value.trim(),
    })
    msg.value = '两步验证已关闭'
    totpCode.value = ''
    totpDisablePwd.value = ''
    await auth.refreshMe()
  } catch (e: any) {
    error.value = e?.message || '关闭失败'
  } finally {
    saving.value = false
  }
}

async function logoutAll() {
  if (!confirm('确认在所有设备退出登录？当前设备也需要重新登录。')) return
  try {
    await api.post('/auth/logout-all')
  } catch {
    // token already revoked server-side
  }
  auth.clearLocal()
  router.push({ name: 'login' })
}

onMounted(async () => {
  try {
    await load()
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
})
</script>

<style scoped>
.totp-secret {
  background: var(--panel-2);
  padding: 0.15rem 0.5rem;
  border-radius: 6px;
  font-size: 15px;
  letter-spacing: 1px;
  user-select: all;
}
</style>
