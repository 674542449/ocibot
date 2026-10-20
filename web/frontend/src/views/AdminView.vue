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
          <h3 style="margin: 0; display: flex; align-items: center; gap: 0.45rem">
            <Icon name="refresh" :size="17" /> 系统更新
          </h3>
          <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
            定位宿主机仓库目录，执行与 SSH 相同的
            <code>bash scripts/install.sh update</code>
            （拉代码 + 构建 + 重启，保留数据库与密钥）。
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

      </template>
    </div>

    <div class="card stack">
      <h3 style="margin: 0">面板设置</h3>
      <div class="choice-group">
        <label class="choice">
          <input
            v-model="allowOpenRegistration"
            type="checkbox"
            @change="saveSettings"
          />
          <span>允许开放注册</span>
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
            <td>
              <Icon v-if="u.totp_enabled" name="check" :size="16" class="ok-mark" />
              <span v-else class="muted">—</span>
            </td>
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
                <!-- Never offered for yourself: the reset revokes your session and
                     clears your 2FA, and the redirect that followed destroyed the
                     one-time password before it could be read — locking out a lone
                     admin with no recovery path. Use 设置 → 修改面板密码 instead. -->
                <button v-if="u.id !== meId" @click="resetPassword(u)">重置密码</button>
                <button v-if="u.id !== meId" @click="toggleAdmin(u)">
                  {{ u.is_admin ? '取消管理员' : '设为管理员' }}
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card stack">
      <div class="row" style="justify-content: space-between; align-items: center">
        <h3 style="margin: 0">IP 封禁</h3>
        <button :disabled="banBusy" @click="loadBans">刷新</button>
      </div>
      <p class="muted" style="margin: 0; font-size: 12px">
        User-Agent 命中下面某一行、不带 User-Agent（开关打开时）、或用诱饵用户名登录（面板里没有这个账号时）的请求，来源 IP
        会被立即封禁，之后它的所有请求都返回 403；IPv6 地址封它所在的整个 /64（否则换个地址就能接着试）。内网 / 本机 / Cloudflare
        节点 / 白名单（OCIBOT_IP_BAN_ALLOWLIST）地址永远不封，已经被封的 IP 写进白名单后也会放行。
        被封在外面时，在服务器上执行 <code>bash scripts/install.sh unban 你的IP</code>。
      </p>
      <div v-if="banViewer" style="font-size: 13px">
        面板看到的你的 IP：<b>{{ banViewer.your_ip }}</b>
        <span v-if="banViewer.via_cloudflare" class="muted">（经 Cloudflare 节点 {{ banViewer.peer_ip }}，已取真实 IP）</span>
        <div v-if="banViewer.your_ip_exempt" class="badge warn" style="margin-top: 0.35rem; white-space: normal">
          面板没有识别出公网 IP（{{ banViewer.your_ip_exempt }}）。如果你是从外网访问的，说明真实 IP 没有传到面板，自动封禁不会生效 ——
          检查反代 / Cloudflare 配置（见 README 故障排查表）。
        </div>
      </div>

      <div class="grid-2">
        <div class="field">
          <label>封禁的 User-Agent（每行一个）</label>
          <textarea v-model="banUaText" rows="6" spellcheck="false"></textarea>
          <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
            普通一行：完全等于才封（区分大小写），如 <code>curl/8.14.1</code>。<br />
            <code>~</code> 开头：UA 里包含这段文字就封（不分大小写、不管版本号），如 <code>~python-requests</code>。<br />
            会误伤正常浏览器的规则（如 <code>~mozilla</code>）保存时会被拒绝。
          </p>
        </div>
        <div class="field">
          <label>诱饵用户名（每行一个，不区分大小写）</label>
          <textarea v-model="banNameText" rows="4" spellcheck="false"></textarea>
        </div>
      </div>
      <label class="choice">
        <input v-model="banEmptyUa" type="checkbox" />
        <span>不带 User-Agent 的请求也封（浏览器一定会带 UA，空的只有脚本和扫描器）</span>
      </label>
      <div class="row">
        <button class="primary" :disabled="banBusy" @click="saveBanRules">保存规则</button>
        <button :disabled="banBusy || !banRecommended.length" @click="fillRecommended">
          填入常见爬虫 UA
        </button>
        <input v-model="manualBanIp" placeholder="手动封禁 IP，如 1.2.3.4" style="max-width: 260px" @keyup.enter="manualBan" />
        <button :disabled="banBusy || !manualBanIp.trim()" @click="manualBan">封禁</button>
      </div>

      <div class="row" style="justify-content: space-between; align-items: center">
        <span class="muted" style="font-size: 12px">
          共 {{ banTotal }} 个被封禁的 IP<span v-if="banTotal > bans.length">（显示最近 {{ bans.length }} 个）</span>
        </span>
        <button :disabled="banBusy || banSelected.size === 0" @click="unbanSelected">
          解封所选（{{ banSelected.size }}）
        </button>
      </div>
      <div v-if="bans.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width: 32px">
                <input type="checkbox" :checked="allBansSelected" title="全选" @change="toggleAllBans" />
              </th>
              <th>IP</th>
              <th>原因</th>
              <th>详情</th>
              <th>封禁时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in bans" :key="b.ip">
              <td><input type="checkbox" :checked="banSelected.has(b.ip)" @change="toggleBan(b.ip)" /></td>
              <td style="font-family: monospace">{{ b.ip }}</td>
              <td>{{ banReasonLabel(b.reason) }}</td>
              <td class="muted" style="font-size: 12px; word-break: break-all">{{ b.detail || '—' }}</td>
              <td class="muted" style="font-size: 12px">{{ fmt(b.created_at) }}</td>
              <td><button :disabled="banBusy" @click="unban([b.ip])">解封</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="muted" style="margin: 0; font-size: 12px">目前没有被封禁的 IP。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import api from '@/api/client'
import Icon from '@/components/Icon.vue'
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

/** Translate raw update shell/git/docker logs into readable Chinese lines. */

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
    msg.value = '更新已开始：拉取代码、重建镜像、滚动重启，期间面板可能短暂不可访问。'
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
  await Promise.all([loadUpdate(), loadBans()])
}

// ---- IP 封禁（只读写面板自己的数据库，不请求 Oracle，所以随页面一起加载）
type IpBanRow = { ip: string; reason: string; detail: string; created_at: string }
const bans = ref<IpBanRow[]>([])
const banTotal = ref(0)
const banViewer = ref<any>(null)
const banUaText = ref('')
const banNameText = ref('')
const banEmptyUa = ref(true)
const banRecommended = ref<string[]>([])
const manualBanIp = ref('')
const banBusy = ref(false)
const banSelected = reactive(new Set<string>())
// 上一次从服务器填进表单的规则。刷新列表 / 封禁 / 解封都会重读，只有表单还没被改过时
// 才用新读到的覆盖 —— 否则改了一半的规则会在点「解封」的瞬间被冲掉。
// 初值等于空表单，第一次加载才会填进去。
let banRulesShown = JSON.stringify(['', '', true])

const allBansSelected = computed(() => bans.value.length > 0 && bans.value.every((b) => banSelected.has(b.ip)))

function banReasonLabel(r: string) {
  return (
    ({
      user_agent: '封禁 UA',
      empty_user_agent: '没有 UA',
      honeypot_username: '诱饵用户名',
      manual: '手动封禁',
    } as Record<string, string>)[r] || r
  )
}

function banRulesForm() {
  return JSON.stringify([banUaText.value, banNameText.value, banEmptyUa.value])
}

function fillRecommended() {
  const have = new Set(lines(banUaText.value).map((l) => (l.startsWith('~') ? l.toLowerCase() : l)))
  const add = banRecommended.value.filter((r) => !have.has(r))
  if (!add.length) {
    msg.value = '常见爬虫 UA 都已经在列表里了'
    return
  }
  banUaText.value = [...lines(banUaText.value), ...add].join('\n')
  msg.value = `已填入 ${add.length} 条常见爬虫 UA，确认后点「保存规则」才会生效`
}

function lines(text: string) {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

async function loadBans() {
  try {
    const { data } = await api.get('/admin/ip-bans')
    bans.value = data.bans || []
    banTotal.value = data.total || 0
    banViewer.value = data.viewer || null
    banRecommended.value = data.recommended_user_agents || []
    if (banRulesForm() === banRulesShown) {
      banUaText.value = (data.rules?.user_agents || []).join('\n')
      banNameText.value = (data.rules?.usernames || []).join('\n')
      banEmptyUa.value = data.rules?.ban_empty_user_agent !== false
      banRulesShown = banRulesForm()
    }
    // 只保留还在列表里的勾选。
    for (const ip of [...banSelected]) if (!bans.value.some((b) => b.ip === ip)) banSelected.delete(ip)
  } catch (e: any) {
    error.value = e?.message || '读取封禁列表失败'
  }
}

async function saveBanRules() {
  error.value = ''
  banBusy.value = true
  try {
    const { data } = await api.put('/admin/ip-ban-rules', {
      user_agents: lines(banUaText.value),
      usernames: lines(banNameText.value),
      ban_empty_user_agent: banEmptyUa.value,
    })
    // 用服务器规范化之后的写法（~ 规则转小写、去重）回填，并当作「未改动」的基准。
    banUaText.value = (data.rules?.user_agents || []).join('\n')
    banNameText.value = (data.rules?.usernames || []).join('\n')
    banEmptyUa.value = data.rules?.ban_empty_user_agent !== false
    banRulesShown = banRulesForm()
    msg.value = '封禁规则已保存'
    await loadBans()
  } catch (e: any) {
    error.value = e?.message || '保存失败'
  } finally {
    banBusy.value = false
  }
}

async function manualBan() {
  const ip = manualBanIp.value.trim()
  if (!ip) return
  error.value = ''
  banBusy.value = true
  try {
    const { data } = await api.post('/admin/ip-bans', { ip })
    msg.value = `已封禁 ${data.ip}`
    manualBanIp.value = ''
    await loadBans()
  } catch (e: any) {
    error.value = e?.message || '封禁失败'
  } finally {
    banBusy.value = false
  }
}

async function unban(ips: string[]) {
  if (!ips.length) return
  if (ips.length > 1 && !confirm(`解封所选的 ${ips.length} 个 IP？`)) return
  error.value = ''
  banBusy.value = true
  try {
    const { data } = await api.post('/admin/ip-bans/unban', { ips })
    msg.value = data.message || '已解封'
    for (const ip of ips) banSelected.delete(ip)
    await loadBans()
  } catch (e: any) {
    error.value = e?.message || '解封失败'
  } finally {
    banBusy.value = false
  }
}

function unbanSelected() {
  return unban([...banSelected])
}

function toggleBan(ip: string) {
  if (banSelected.has(ip)) banSelected.delete(ip)
  else banSelected.add(ip)
}

function toggleAllBans() {
  if (allBansSelected.value) banSelected.clear()
  else for (const b of bans.value) banSelected.add(b.ip)
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
.ok-mark {
  color: var(--ok);
}
</style>
