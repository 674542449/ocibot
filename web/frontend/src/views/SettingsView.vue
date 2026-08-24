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
            <!-- 只写抢机。0.4.36 删掉了定时开关机和预算告警，但这句话还在承诺
                 「定时任务失败、预算超额时推送」—— 后端 notify.EVENT_KEYS 只有
                 ("capacity",)，那两类通知永远不会来。承诺一个不存在的告警比没有
                 告警更糟：操作员会以为没收到就是没出事。 -->
            抢机成功 / 失败时推送。支持 Telegram / Bark / Server酱 / Webhook / 邮件。
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
          <!-- placeholder 写 465 会被读成「不填就是 465」，但后端 validate_channel_config
               的 smtp 必填清单里有 port，留空直接 400「smtp 渠道缺少字段: port」——
               提示语承诺了一个默认值，保存却报缺字段，操作员只会以为是别的地方填错了。
               notify._smtp_port 自己的兜底就是 465，所以这里补 465 而不是标必填：两边
               取同一个默认值，谁也不会先于谁做出不同的解释。
               选 465 还有 TLS 上的含义 —— _send_smtp 的 use_ssl 默认 (port == 465)，
               所以「留空」落在全程 SSL 的那一档，而不是落在需要 STARTTLS 升级、
               失败还要靠下面那个开关兜底的 587/25。 -->
          <div class="field">
            <label>端口</label>
            <input v-model="addConfig.port" inputmode="numeric" placeholder="留空按 465 处理" />
            <p class="field-hint">465 = 连上就是 SSL；587 / 25 / 2525 = 先明文连接再 STARTTLS 升级。只接受这四个端口。</p>
          </div>
          <div class="field"><label>用户名</label><input v-model="addConfig.username" /></div>
          <div class="field"><label>密码 / 授权码</label><input v-model="addConfig.password" type="password" /></div>
          <div class="field"><label>收件邮箱</label><input v-model="addConfig.to_addr" /></div>
          <div class="field"><label>发件地址（可选，默认用户名）</label><input v-model="addConfig.from_addr" /></div>

          <!-- require_tls 以前只能手发 PATCH 才能改。_send_smtp 在服务器不宣告
               STARTTLS 时直接中止发送，错误里让操作员「在渠道配置中显式设置
               require_tls=false」—— 而面板上根本没有这一项，于是这条提示指向一个
               不存在的开关：邮件通知彻底发不出去，抢机成功也没人知道。
               默认必须是开（true）。关掉等于同意把邮箱密码明文丢上网，这种选择要
               当场看见后果，所以不做成一个混在其它字段里的小方块。 -->
          <div class="field span-2">
            <label>传输加密</label>
            <div class="tls-box" :class="{ 'tls-risky': !addRequireTls }">
              <label class="choice">
                <input v-model="addRequireTls" type="checkbox" />
                <span>要求 TLS 后再发送账号密码（推荐保持开启）</span>
              </label>
              <span v-if="!addRequireTls" class="badge err">高风险</span>
              <p class="field-hint tls-note">
                <template v-if="addRequireTls">
                  服务器不支持 STARTTLS 时<strong>中止发送</strong>，不会降级成明文。
                  <template v-if="smtpPort === 465">当前端口 465 本身全程 SSL，走不到这一步。</template>
                </template>
                <template v-else>
                  已关闭：服务器不支持 STARTTLS 时仍会继续登录发信，
                  <strong>SMTP 用户名和密码 / 授权码会以明文经过网络</strong>，
                  链路上任何人（同一 WiFi、机房交换机、上游网络）都能读到并接管这个邮箱。
                  只有自建内网邮件服务器、且这个密码不在别处复用时才考虑关闭。
                  <template v-if="smtpPort === 465">当前端口是 465（全程 SSL），并不需要关掉它。</template>
                </template>
              </p>
            </div>
          </div>
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
              <td colspan="6" class="muted empty">尚未配置任何通知渠道。强烈建议配置——抢机成功后可第一时间收到推送。</td>
            </tr>
            <tr v-for="c in channels" :key="c.id">
              <td>{{ c.name }}</td>
              <td>{{ kindLabel(c.kind) }}</td>
              <td class="muted" style="font-size: 12px; max-width: 240px; word-break: break-all">{{ c.config_hint }}</td>
              <!-- events 为空数组时 join('') 出来是**空字符串**，这一格就是一片空白，
                   跟「还没加载出来」长得一模一样 —— 而它其实是「一个事件都不订阅，
                   这个渠道永远不会收到推送」。后端读回时已经把 NULL（老库里的
                   「订阅全部」）展开成完整事件键，所以现在能走到空数组的只剩下真正
                   取消订阅的渠道，必须显式说出来，否则它和一个正常渠道在列表里毫无区别。 -->
              <td class="muted" style="font-size: 12px">
                <span v-if="c.events.length">{{ c.events.map(eventLabel).join(' / ') }}</span>
                <span v-else class="badge warn">未订阅 · 不会推送</span>
              </td>
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
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
// 单独一个 ref 而不是塞进 addConfig，两个原因：
//
// 1. addConfig 的类型是 Record<string, string>，往里放布尔值 vue-tsc 直接报错。
//    （不是「会被写成字符串 'false'」—— checkbox 的 v-model 在没有
//    true-value/false-value 时赋的是真布尔，见 @vue/runtime-dom 的
//    getCheckboxValue：`key in el ? el[key] : checked`。）
// 2. 更要紧的是 clearAddConfig 会 `Object.keys(addConfig).forEach(delete)` 无差别
//    清空。require_tls 一旦跟着被删掉，checkbox 会渲染成**未勾选**，而后端在
//    缺这个键时默认是 true —— 界面显示「已关闭 TLS 要求」，实际行为却是要求 TLS，
//    正好反着。这个开关是安全开关，显示和实际反过来比没有这个开关更糟。
const addRequireTls = ref(true)

// 端口留空按 465 算 —— 和 createChannel 里真正发出去的值、和 notify._smtp_port
// 的兜底保持同一套规则，免得提示语说的是一回事、发出去的是另一回事。
const smtpPort = computed(() => {
  const raw = String(addConfig.port ?? '').trim()
  return raw === '' ? 465 : Number(raw)
})

function clearAddConfig() {
  Object.keys(addConfig).forEach((k) => delete addConfig[k])
  // 事件勾选也要复位。EVENTS 目前只有一项，取消勾选就 POST events: []，
  // 后端会照收（空列表 = 谁也不订阅），而这里以前不复位，于是**之后新建的每一个
  // 渠道**都继承了这个空列表：界面上状态显示「启用」、点测试也报绿，实际一条都不发。
  // 列表页又没有事件编辑入口，只能改数据库或手发 PATCH 才能救回来。
  addEvents.value = EVENTS.map((e) => e.key)
  // 同理，而且后果更重：为某台内网自建服务器关掉过一次 TLS 要求，如果不复位，
  // 之后新建的每个 SMTP 渠道都带着 require_tls=false —— 下一个渠道很可能是
  // 公网邮箱，密码就这样明文出去了，而界面上开关默认长得像是开着的。
  addRequireTls.value = true
}

// 换渠道类型必须清空 addConfig。它是所有类型共用的**一个** dict，切类型只是换了
// 渲染哪几个输入框，之前填的键一个都没走。Telegram 填完 bot_token / chat_id 再切到
// Webhook 保存，就会把这两个键连同 url 一起 POST 上去；后端整包收下（加密存库），
// 于是一个 webhook 渠道里躺着一个 bot token —— 界面上既看不见也删不掉。
watch(() => addForm.kind, clearAddConfig)

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
    if (addForm.kind === 'smtp') {
      const rawPort = String(addConfig.port ?? '').trim()
      // 非数字原样送上去，让后端回「SMTP 端口必须是数字」。以前是无条件
      // Number()，"abc" 变成 NaN，JSON.stringify 又把 NaN 写成 null，后端看到的是
      // 「没填」，于是报「缺少字段: port」—— 明明填了，提示却说没填。
      config.port = rawPort === '' ? 465 : /^\d+$/.test(rawPort) ? Number(rawPort) : rawPort
      config.require_tls = addRequireTls.value
    }
    await api.post('/notifications', {
      kind: addForm.kind,
      name: addForm.name,
      config,
      events: addEvents.value,
      enabled: true,
    })
    msg.value = '渠道已保存，建议点「测试」确认可以收到'
    showAdd.value = false
    clearAddConfig()
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
/* .grid-2 是两列 grid，传输加密这一块要横跨整行 —— 挤在半列里就又变成一个
   「不起眼的复选框」，而这是本页唯一一个会把密码明文送上网的选项。 */
.span-2 {
  grid-column: 1 / -1;
}

.tls-box {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
  background: var(--panel-2);
}

/* 关掉之后整块变红：勾选框本身太小，光靠一行说明文字扫一眼是看不见的。 */
.tls-box.tls-risky {
  border-color: var(--danger);
  background: var(--danger-soft);
}

.tls-box .badge {
  margin-left: 0.4rem;
  vertical-align: middle;
}

.tls-note strong {
  color: var(--text);
  font-weight: 600;
}

.tls-box.tls-risky .tls-note,
.tls-box.tls-risky .tls-note strong {
  color: var(--danger);
}

.totp-secret {
  background: var(--panel-2);
  padding: 0.15rem 0.5rem;
  border-radius: 6px;
  font-size: 15px;
  letter-spacing: 1px;
  user-select: all;
}
</style>
