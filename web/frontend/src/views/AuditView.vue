<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>审计日志</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          危险操作与登录记录 · 失败登录含来源 IP 与失败原因
        </p>
      </div>
      <div class="page-tools">
        <label class="choice muted" style="flex: 0 0 auto">
          <input v-model="authOnly" type="checkbox" @change="load" />
          <span>只看登录</span>
        </label>
        <button class="primary" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <!-- 只在有可疑迹象时出现，避免变成一条永远在那儿的横幅被忽略 -->
    <div v-if="alarm" class="card warn-box">
      <strong>⚠ {{ alarm }}</strong>
      <p class="muted" style="margin: 0.3rem 0 0; font-size: 12px">
        「密码正确但两步验证失败」意味着对方已经拿到了密码，只是被 2FA 挡住 ——
        这种情况应立刻改密码。若你还没开 2FA，去「设置」开启。
      </p>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>动作</th>
            <th>账号</th>
            <th>来源 IP</th>
            <th>结果</th>
            <th>客户端</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="6" class="muted empty">暂无审计记录</td>
          </tr>
          <tr v-for="a in rows" :key="a.id">
            <td class="muted" style="font-size: 12px; white-space: nowrap">
              {{ formatTime(a.created_at) }}
            </td>
            <td>
              <span class="badge" :class="actionClass(a.action)">{{ actionLabel(a.action) }}</span>
            </td>
            <td style="font-size: 12px; word-break: break-all">{{ a.target || '—' }}</td>
            <td style="font-size: 12px; white-space: nowrap">
              <span
                v-if="a.parsed.ip"
                class="copyable"
                title="单击复制 IP"
                role="button"
                tabindex="0"
                @click="copyIp(a.parsed.ip)"
                @keydown.enter.prevent="copyIp(a.parsed.ip)"
              >{{ a.parsed.ip }}</span>
              <span v-else class="muted">—</span>
            </td>
            <td style="font-size: 12px">{{ reasonLabel(a.parsed.reason) || a.parsed.note || (a.parsed.consumed ? '' : a.detail) || '—' }}</td>
            <td class="muted" style="font-size: 11px; word-break: break-all; max-width: 22rem">
              {{ a.parsed.ua || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="muted" style="font-size: 12px; margin: 0">
      注：<strong>不记录填写的密码</strong>。失败登录里的密码基本都是别人泄露的真实凭据，
      或你自己打错一个字符的管理员密码；本页会发到浏览器、存在数据库里、也会进备份文件，
      不是存放它们的地方。识别撞库靠的是账号 + IP + 失败原因，这些都在上面。
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import api, { type AuditItem } from '@/api/client'
import { copyText } from '@/utils/toast'

type Parsed = { ip: string; reason: string; ua: string; consumed?: boolean; note?: string }
type Row = AuditItem & { parsed: Parsed }

const items = ref<AuditItem[]>([])
const loading = ref(false)
const error = ref('')
const authOnly = ref(false)

const ACTION_LABELS: Record<string, string> = {
  'auth.login': '登录成功',
  'auth.login_failed': '登录失败',
  'auth.login_blocked': '被限流拦截',
  'auth.login_disabled': '账号已禁用',
  'auth.totp_failed': '两步验证失败',
  'auth.logout_all': '全设备退出',
  // 这四条是随后端 _AUTH_ACTIONS 一起加进「只看登录」视图的。
  //
  // 这张表和后端那个元组必须同步:actionLabel 的兜底是 `|| a`,后端放进来而这里
  // 没有的动作,会在动作列里裸显示成 `auth.change_password` 这样的原始 id。
  // 它们的 write_audit 都没传 detail(auth.py 里 register 传的还是 legacy 的
  // 纯文本 `ip=...`),所以来源 IP / 结果 / 客户端三列本来就会是「—」——
  // 那是数据本身没有,不是渲染坏了。
  'auth.register': '注册账号',
  'auth.change_password': '修改密码',
  'auth.totp_enabled': '开启两步验证',
  'auth.totp_disabled': '关闭两步验证',
  // 抢机成功/结束时有渠道没推出去。没有这一条的话动作列会裸显示
  // `notify.failed`,而这恰恰是操作员最需要一眼认出来的一条 ——
  // 任务是绿的、实例也开出来了,只有通知没送到。
  'notify.failed': '通知推送失败',
}

const REASON_LABELS: Record<string, string> = {
  ok: '成功',
  no_such_user: '用户名不存在',
  bad_password: '密码错误',
  bad_totp_password_was_correct: '两步验证码错误（密码是对的）',
  account_disabled: '账号被禁用',
  rate_limited: '尝试过于频繁，已拦截',
}

function actionLabel(a: string) {
  return ACTION_LABELS[a] || a
}

function actionClass(a: string) {
  if (a === 'auth.login') return 'running'
  if (a === 'auth.totp_failed') return 'over'
  if (a.startsWith('auth.login_')) return 'warn'
  return ''
}

function reasonLabel(r: string) {
  return r ? REASON_LABELS[r] || r : ''
}

/** detail 是 write_audit 写的 JSON；旧记录是 `ip=1.2.3.4` 这种纯文本，两种都要能读。 */
function parseDetail(detail: string): Parsed {
  const out: Parsed = { ip: '', reason: '', ua: '', consumed: false, note: '' }
  const raw = (detail || '').trim()
  if (!raw) return out
  if (raw.startsWith('{')) {
    try {
      const o = JSON.parse(raw)
      out.ip = String(o.ip || '')
      out.reason = String(o.reason || '')
      out.ua = String(o.ua || '')
      // notify.failed 的 detail 是另一种形状(没有 ip/reason/ua)。不专门取一下的话
      // 「结果」列就只剩一个「—」,而「几个渠道没发出去、为什么」正是这条记录的全部内容。
      if (typeof o.failed === 'number') {
        const names = (o.channels || [])
          .map((c: any) => `${c?.name || c?.kind || '?'}: ${c?.detail || '失败'}`)
          .join('；')
        out.note = `${o.failed}/${o.attempted ?? o.failed} 个渠道未推送` + (names ? ` — ${names}` : '')
      }
      out.consumed = true
      return out
    } catch {
      /* fall through to the legacy form */
    }
  }
  const m = raw.match(/ip=([^\s]+)/)
  if (m) {
    out.ip = m[1]
    // 认出来的 legacy 形式要标记掉,否则「结果」列的兜底 `|| a.detail` 会把
    // 整串 `ip=1.2.3.4` 原样印进那一格 —— 而 IP 已经在它自己那一列里显示过了。
    // auth.register 写的就是这种旧格式(auth.py 的 write_audit 没走 JSON)。
    out.consumed = true
  }
  return out
}

const rows = computed<Row[]>(() =>
  items.value.map((a) => ({ ...a, parsed: parseDetail(a.detail) })),
)

/** 只在真有值得看的信号时提示，而不是每次都挂一条横幅。 */
const alarm = computed(() => {
  const recent = rows.value
  const totp = recent.filter((r) => r.action === 'auth.totp_failed').length
  if (totp) return `有 ${totp} 次「密码正确但两步验证失败」`
  const failed = recent.filter((r) => r.action === 'auth.login_failed')
  const blocked = recent.filter((r) => r.action === 'auth.login_blocked').length
  const ips = new Set(failed.map((r) => r.parsed.ip).filter(Boolean))
  if (blocked) return `有 ${blocked} 次登录被限流拦截（来自 ${ips.size || '未知数量'} 个 IP）`
  if (failed.length >= 10) return `最近有 ${failed.length} 次登录失败，来自 ${ips.size} 个 IP`
  return ''
})

function formatTime(v: string) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

async function copyIp(ip: string) {
  if (ip) await copyText(ip, 'IP 已复制')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get<AuditItem[]>('/audit', {
      params: { limit: 200, auth_only: authOnly.value },
    })
    items.value = data
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.warn-box {
  padding: 0.65rem 0.8rem;
  border-color: var(--warn);
}
.badge.over {
  background: var(--danger-soft, rgba(229, 72, 77, 0.15));
  color: var(--danger, #e5484d);
}
</style>
