<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>租户 / API 配置</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          粘贴 OCI config；私钥仅服务端加密存储 · 「锁定为默认」后其他页面不用再选租户
        </p>
      </div>
      <div class="page-tools">
        <button class="primary" @click="openCreate">添加租户</button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>区域</th>
            <th>等级</th>
            <th>Tenancy</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="tenants.length === 0">
            <td colspan="6" class="muted">还没有租户，点击右上角添加，或粘贴原始 API 配置。</td>
          </tr>
          <tr v-for="t in orderedTenants" :key="t.id" :class="{ 'sub-row': !!t.parent_tenant_id }">
            <td>
              <span v-if="t.parent_tenant_id" class="muted sub-tree">└</span>
              <span class="dot" :style="{ background: t.color }"></span>
              {{ t.name }}
              <span v-if="t.parent_tenant_id" class="badge sub-badge">副区</span>
              <span v-if="isTenantLocked(t.id)" class="badge running" title="其他页面默认使用该租户">
                🔒 默认
              </span>
            </td>
            <td>
              {{ t.region }}
              <span v-if="t.region_label && t.region_label !== t.region" class="muted">
                · {{ t.region_label }}
              </span>
            </td>
            <td>{{ tierLabel(t.account_tier) }}</td>
            <td class="muted" style="font-size: 12px; word-break: break-all">
              {{ shortId(t.tenancy_ocid) }}
            </td>
            <td>
              <span class="badge" :class="t.enabled ? 'running' : 'stopped'">
                {{ t.enabled ? '启用' : '禁用' }}
              </span>
              <span v-if="!t.free_only_mode" class="badge warn" title="超出 Always Free 不再拦截">
                允许计费
              </span>
              <div v-if="pwdStatus[t.id]" class="pwd-line">
                <span
                  class="badge"
                  :class="pwdStatus[t.id].expires ? 'warn' : 'running'"
                >{{ pwdStatus[t.id].expires ? '会过期' : '永不过期' }}</span>
                <span class="muted">{{ pwdStatus[t.id].summary }}</span>
                <!-- 原始策略值：判断「关闭强制改密」是否生效时，
                     用户要看的就是 defaultPasswordPolicy 的这个数字本身。 -->
                <span
                  v-for="pol in pwdStatus[t.id].policies"
                  :key="pol.name"
                  class="pwd-pol"
                  :class="{ 'pwd-pol-main': pol.is_default }"
                  :title="pol.is_template ? 'Oracle 内置模板，不是控制台登录实际生效的策略' : '控制台登录生效的策略'"
                >
                  {{ pol.name }}=<strong>{{ pol.days > 0 ? pol.days + ' 天' : '未设置' }}</strong>
                  <span v-if="pol.is_template" class="muted">（模板）</span>
                </span>
              </div>
            </td>
            <td>
              <div class="row">
                <button
                  :class="{ primary: !isTenantLocked(t.id) }"
                  :title="isTenantLocked(t.id)
                    ? '取消后各页面恢复为默认选第一个租户'
                    : '锁定后，实例 / 存储 / 创建实例 / 账号用量 进入时都自动选它'"
                  @click="toggleLock(t)"
                >
                  {{ isTenantLocked(t.id) ? '取消锁定' : '锁定为默认' }}
                </button>
                <button :disabled="busy === t.id" @click="test(t)">测试连接</button>
                <button :disabled="busy === t.id" @click="detectTier(t)">识别等级</button>
                <button
                  v-if="!t.parent_tenant_id"
                  :disabled="busy === t.id"
                  title="查看 / 开通该账号的其他国家区域（副区）"
                  @click="openRegions(t)"
                >
                  副区管理
                </button>
                <button
                  :disabled="busy === t.id"
                  title="从 Oracle 读取该账号控制台密码的真实到期时间"
                  @click="loadPasswordStatus(t)"
                >
                  查改密状态
                </button>
                <button
                  :disabled="busy === t.id"
                  title="调用 Oracle Identity Domain API，把控制台强制改密周期改为不强制"
                  @click="disableOciPasswordExpiry(t)"
                >
                  关闭强制改密
                </button>
                <button :disabled="busy === t.id" @click="openEdit(t)">编辑</button>
                <button class="danger" :disabled="busy === t.id" @click="remove(t)">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 副区 (secondary regions) -->
    <div v-if="regionsFor" class="card stack">
      <div class="row" style="justify-content: space-between; align-items: baseline">
        <h3 style="margin: 0">副区管理 · {{ regionsFor.name }}</h3>
        <button type="button" @click="closeRegions">关闭</button>
      </div>
      <p class="muted" style="margin: 0; font-size: 13px">
        副区 = 同一个 Oracle 账号订阅的其他国家 / 地区。开通后面板会自动添加一个同凭据的副区租户，
        实例、存储、WebSSH、创建实例等页面都可直接选它。
      </p>
      <p class="warn-text" style="margin: 0; font-size: 13px">
        ⚠ 两点务必知悉：① Oracle <strong>无法取消</strong>已开通的区域；
        ② <strong>Always Free 只存在于主区</strong>，副区里创建的实例（包括 A1.Flex）都会按量计费，
        因此副区租户默认为「允许超额计费」。免费账号通常没有开通副区的权限。
      </p>

      <div v-if="regionsLoading" class="muted">正在读取区域…</div>
      <div v-else-if="regionsError" class="error-box">{{ regionsError }}</div>
      <template v-else-if="regions">
        <div class="field">
          <label>已开通区域（主区 {{ regions.home_region || '—' }}）</label>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>区域</th>
                  <th>状态</th>
                  <th>面板</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in regions.subscribed" :key="r.region_name">
                  <td>
                    {{ r.region_name }}
                    <span class="muted">· {{ r.region_label }}</span>
                    <span v-if="r.is_home_region" class="badge">主区</span>
                  </td>
                  <td class="muted">{{ r.status || '—' }}</td>
                  <td>
                    <span v-if="r.tenant_id" class="badge running">已添加</span>
                    <button
                      v-else
                      type="button"
                      :disabled="regionBusy !== ''"
                      @click="subscribeRegion(r.region_name, true)"
                    >
                      添加到面板
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="field">
          <label>开通新的副区</label>
          <div class="row">
            <select v-model="regionToAdd" style="min-width: 16rem">
              <option value="">选择区域</option>
              <option v-for="r in regions.available" :key="r.region_name" :value="r.region_name">
                {{ r.region_name }} · {{ r.region_label }}
              </option>
            </select>
            <button
              class="primary"
              type="button"
              :disabled="!regionToAdd || regionBusy !== ''"
              @click="subscribeRegion(regionToAdd, false)"
            >
              {{ regionBusy ? '提交中…' : '开通并添加' }}
            </button>
          </div>
          <p v-if="regions.message" class="muted" style="margin: 0.35rem 0 0; font-size: 12px">
            {{ regions.message }}
          </p>
        </div>
      </template>
    </div>

    <!-- Create: paste-first -->
    <div v-if="showForm && !editingId" class="card stack">
      <h3 style="margin: 0">粘贴原始 API 添加租户</h3>
      <p class="muted" style="margin: 0; font-size: 13px">
        支持 <code>~/.oci/config</code> 格式（user / fingerprint / tenancy / region）。<br />
        私钥推荐：点 <strong>选择私钥文件…</strong> 读取本地 <code>.pem</code>；也可与 config 贴在同一段文本，或粘贴到下方文本框。
      </p>

      <div class="field">
        <label>OCI 配置文本（必填）</label>
        <textarea
          v-model="paste.api_text"
          rows="10"
          spellcheck="false"
          placeholder="[DEFAULT]
user=ocid1.user.oc1..aaaa...
fingerprint=aa:bb:cc:...
tenancy=ocid1.tenancy.oc1..aaaa...
region=ap-tokyo-1
key_file=~/.oci/oci_api_key.pem"
        ></textarea>
      </div>

      <div class="row">
        <button type="button" @click="pasteFromClipboard">从剪贴板粘贴</button>
        <button type="button" @click="parseOnly" :disabled="saving">仅解析预览</button>
        <button type="button" @click="paste.api_text = ''; parsePreview = null">清空</button>
      </div>

      <div v-if="parsePreview" class="parse-box" :class="parsePreview.ok ? 'ok' : 'bad'">
        <div>{{ parsePreview.message }}</div>
        <div v-if="parsePreview.ok || parsePreview.user_ocid" class="muted" style="margin-top: 0.35rem; font-size: 12px">
          name={{ parsePreview.name || '—' }} · region={{ parsePreview.region || '—' }} ·
          key={{ parsePreview.has_private_key ? '已识别私钥' : '缺私钥' }}
        </div>
        <div v-for="(w, i) in parsePreview.warnings || []" :key="i" style="margin-top: 0.25rem">⚠ {{ w }}</div>
      </div>

      <div class="field">
        <label>私钥 PEM（若未包含在上方文本中）</label>
        <div class="row" style="margin-bottom: 0.4rem">
          <button type="button" @click="pickPemForPaste">选择私钥文件…</button>
          <span v-if="pasteKeyFile" class="muted" style="font-size: 12px">{{ pasteKeyFile }}</span>
          <button v-if="paste.private_key_pem" type="button" @click="clearPastePem">清除私钥</button>
        </div>
        <textarea
          v-model="paste.private_key_pem"
          rows="5"
          spellcheck="false"
          placeholder="-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
也可点「选择私钥文件」直接读取 .pem"
        ></textarea>
      </div>

      <div class="grid-2">
        <div class="field">
          <label>显示名称（可选，留空自动用 region / profile）</label>
          <input v-model="paste.name" placeholder="例如 东京-主账号" />
        </div>
        <div class="field">
          <label>备注（可选）</label>
          <input v-model="paste.description" />
        </div>
      </div>

      <label class="choice muted">
        <input v-model="paste.test_connection" type="checkbox" />
        <span>保存后自动测试连接（会请求 Oracle API，默认关闭）</span>
      </label>

      <div class="row">
        <button class="primary" :disabled="saving" @click="importPaste">
          {{ saving ? '保存中…' : '解析并保存' }}
        </button>
        <button type="button" @click="showManual = !showManual">
          {{ showManual ? '收起手动表单' : '改用手动填写' }}
        </button>
        <button @click="showForm = false">取消</button>
      </div>

      <!-- Optional manual fallback -->
      <div v-if="showManual" class="stack manual-block">
        <h4 style="margin: 0">手动填写（备用）</h4>
        <div class="grid-2">
          <div class="field">
            <label>显示名称</label>
            <input v-model="form.name" />
          </div>
          <div class="field">
            <label>Region</label>
            <input v-model="form.region" placeholder="ap-tokyo-1" />
          </div>
          <div class="field">
            <label>User OCID</label>
            <input v-model="form.user_ocid" />
          </div>
          <div class="field">
            <label>Tenancy OCID</label>
            <input v-model="form.tenancy_ocid" />
          </div>
          <div class="field">
            <label>Fingerprint</label>
            <input v-model="form.fingerprint" />
          </div>
          <div class="field">
            <label>Compartment OCID（可选）</label>
            <input v-model="form.compartment_ocid" />
          </div>
        </div>
        <div class="field">
          <label>私钥 PEM</label>
          <div class="row" style="margin-bottom: 0.4rem">
            <button type="button" @click="pickPemForForm">选择私钥文件…</button>
            <span v-if="formKeyFile" class="muted" style="font-size: 12px">{{ formKeyFile }}</span>
          </div>
          <textarea v-model="form.private_key_pem" rows="5"></textarea>
        </div>
        <button class="primary" :disabled="saving" @click="saveManual">手动保存</button>
      </div>
    </div>

    <!-- Edit: field form -->
    <div v-if="showForm && editingId" class="card stack">
      <h3 style="margin: 0">编辑租户</h3>
      <div class="grid-2">
        <div class="field">
          <label>显示名称</label>
          <input v-model="form.name" required />
        </div>
        <div class="field">
          <label>Region</label>
          <input v-model="form.region" placeholder="ap-tokyo-1" />
        </div>
        <div class="field">
          <label>User OCID</label>
          <input v-model="form.user_ocid" />
        </div>
        <div class="field">
          <label>Tenancy OCID</label>
          <input v-model="form.tenancy_ocid" />
        </div>
        <div class="field">
          <label>Fingerprint</label>
          <input v-model="form.fingerprint" />
        </div>
        <div class="field">
          <label>Compartment OCID（可选）</label>
          <input v-model="form.compartment_ocid" />
        </div>
      </div>
      <div class="field">
        <label>私钥 PEM（留空则不修改）</label>
        <div class="row" style="margin-bottom: 0.4rem">
          <button type="button" @click="pickPemForForm">选择私钥文件…</button>
          <span v-if="formKeyFile" class="muted" style="font-size: 12px">{{ formKeyFile }}</span>
        </div>
        <textarea
          v-model="form.private_key_pem"
          rows="6"
          placeholder="•••• 已保存，留空不改"
        ></textarea>
      </div>
      <div class="field">
        <label>备注</label>
        <input v-model="form.description" />
      </div>
      <div class="field">
        <label>月度预算 USD（0=关闭超额提醒；由 Worker 每日检查并推送）</label>
        <input v-model.number="form.budget_monthly_usd" type="number" min="0" step="0.5" />
      </div>
      <div class="field">
        <label class="choice">
          <input v-model="form.free_only_mode" type="checkbox" />
          <span>仅使用免费额度（超出 Always Free 时直接拦截创建 / 扩容）</span>
        </label>
        <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
          建议保持开启。Oracle 账号一旦升级过就会被识别为「付费」，关闭本项后超额只会警告不会拦截，
          可能产生真实费用。确实要用付费资源时再关闭。
        </p>
      </div>

      <div class="row">
        <button class="primary" :disabled="saving" @click="saveManual">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button @click="showForm = false">取消</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import api, { type Tenant, type TenantRegions } from '@/api/client'
import { isTenantLocked, lockTenant, unlockTenant } from '@/stores/tenantLock'
import { pickAndReadTextFile } from '@/utils/file'

type ParsePreview = {
  ok: boolean
  message: string
  name?: string
  user_ocid?: string
  tenancy_ocid?: string
  fingerprint?: string
  region?: string
  compartment_ocid?: string
  has_private_key?: boolean
  key_file_hint?: string
  warnings?: string[]
}

const tenants = ref<Tenant[]>([])
const error = ref('')
const msg = ref('')
const busy = ref('')
const showForm = ref(false)
const showManual = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const parsePreview = ref<ParsePreview | null>(null)
const pasteKeyFile = ref('')
const formKeyFile = ref('')

const paste = reactive({
  api_text: '',
  private_key_pem: '',
  name: '',
  description: '',
  // Default off: never hit Oracle unless the user explicitly opts in.
  test_connection: false,
})

const form = reactive({
  name: '',
  user_ocid: '',
  tenancy_ocid: '',
  fingerprint: '',
  region: 'ap-tokyo-1',
  private_key_pem: '',
  compartment_ocid: '',
  description: '',
  budget_monthly_usd: 0,
  free_only_mode: true,
})

const regionsFor = ref<Tenant | null>(null)
const regions = ref<TenantRegions | null>(null)
const regionsLoading = ref(false)
const regionsError = ref('')
const regionBusy = ref('')
const regionToAdd = ref('')

/** Primaries in name order, each immediately followed by its 副区 rows. */
const orderedTenants = computed(() => {
  const primaries = tenants.value.filter((t) => !t.parent_tenant_id)
  const out: Tenant[] = []
  for (const p of primaries) {
    out.push(p)
    out.push(...tenants.value.filter((c) => c.parent_tenant_id === p.id))
  }
  // Orphans (parent deleted out-of-band) must still be listed, not silently hidden.
  const seen = new Set(out.map((t) => t.id))
  out.push(...tenants.value.filter((t) => !seen.has(t.id)))
  return out
})

/** 每个租户的真实改密状态（点「查改密状态」后填充）。 */
type PwdPolicy = { name: string; days: number; is_default: boolean; is_template: boolean }
type PwdStatus = {
  expires: boolean
  summary: string
  policy: string
  user: string
  policies: PwdPolicy[]
}
const pwdStatus = reactive<Record<string, PwdStatus>>({})

/**
 * 读取 Oracle 上真实的控制台密码到期时间。
 *
 * 这是判断「关闭强制改密」是否生效的唯一依据：那个操作返回成功只代表 PATCH 被接受，
 * 而实际生效与否要看策略的 passwordExpiresAfter 和该用户的密码状态。
 */
async function loadPasswordStatus(t: Tenant) {
  error.value = ''
  msg.value = ''
  busy.value = t.id
  try {
    const { data } = await api.get<{
      ok: boolean
      message: string
      data?: {
        effective?: {
        expires?: boolean
        summary?: string
        policy_name?: string
        all_policies?: PwdPolicy[]
      }
        user?: { user_name?: string; found?: boolean }
        errors?: string[]
      }
    }>(`/tenants/${t.id}/oci-password-policy`)
    const eff = data.data?.effective
    if (!eff) {
      error.value = `${t.name}: ${data.message || '未能读取密码策略'}`
      return
    }
    pwdStatus[t.id] = {
      expires: !!eff.expires,
      summary: eff.summary || '',
      policy: eff.policy_name || '',
      user: data.data?.user?.user_name || '',
      policies: eff.all_policies || [],
    }
    // 读到了策略但没找到用户时，到期日是算不出来的 —— 说清楚，别让人以为“永不过期”。
    const notes = data.data?.errors || []
    if (notes.length) msg.value = `${t.name}: ${notes.join('；')}`
  } catch (e: any) {
    error.value = e?.message || '读取改密状态失败'
  } finally {
    busy.value = ''
  }
}

function tierLabel(t: string) {
  return { paid: '已升级', free: '免费' }[t] || '未知'
}

function shortId(id: string) {
  if (!id) return '—'
  if (id.length <= 22) return id
  return `${id.slice(0, 12)}…${id.slice(-8)}`
}

function replaceTenant(row: Tenant) {
  tenants.value = tenants.value.map((x) => (x.id === row.id ? { ...row } : x))
}

function resetForm() {
  form.name = ''
  form.user_ocid = ''
  form.tenancy_ocid = ''
  form.fingerprint = ''
  form.region = 'ap-tokyo-1'
  form.private_key_pem = ''
  form.compartment_ocid = ''
  form.description = ''
  form.budget_monthly_usd = 0
  form.free_only_mode = true
}

function resetPaste() {
  paste.api_text = ''
  paste.private_key_pem = ''
  paste.name = ''
  paste.description = ''
  paste.test_connection = false
  parsePreview.value = null
  showManual.value = false
  pasteKeyFile.value = ''
  formKeyFile.value = ''
}

async function pickPemForPaste() {
  error.value = ''
  const text = await pickAndReadTextFile('.pem,.key,.txt,text/plain')
  if (text == null) return
  if (!/PRIVATE KEY/i.test(text)) {
    error.value = '所选文件看起来不是私钥 PEM（未找到 PRIVATE KEY 标记）'
    return
  }
  paste.private_key_pem = text.trim()
  pasteKeyFile.value = '已从文件加载私钥'
  await parseOnly()
}

function clearPastePem() {
  paste.private_key_pem = ''
  pasteKeyFile.value = ''
}

async function pickPemForForm() {
  error.value = ''
  const text = await pickAndReadTextFile('.pem,.key,.txt,text/plain')
  if (text == null) return
  if (!/PRIVATE KEY/i.test(text)) {
    error.value = '所选文件看起来不是私钥 PEM（未找到 PRIVATE KEY 标记）'
    return
  }
  form.private_key_pem = text.trim()
  formKeyFile.value = '已从文件加载私钥'
}

function openCreate() {
  editingId.value = null
  resetForm()
  resetPaste()
  showForm.value = true
}

function openEdit(t: Tenant) {
  editingId.value = t.id
  form.name = t.name
  form.user_ocid = t.user_ocid
  form.tenancy_ocid = t.tenancy_ocid
  form.fingerprint = t.fingerprint
  form.region = t.region
  form.private_key_pem = ''
  form.compartment_ocid = t.compartment_ocid
  form.description = t.description
  form.budget_monthly_usd = t.budget_monthly_usd ?? 0
  form.free_only_mode = t.free_only_mode ?? true
  formKeyFile.value = ''
  showForm.value = true
}

async function load() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data
}

async function pasteFromClipboard() {
  error.value = ''
  try {
    const text = await navigator.clipboard.readText()
    if (!text?.trim()) {
      error.value = '剪贴板为空'
      return
    }
    paste.api_text = text
    await parseOnly()
  } catch {
    error.value = '无法读取剪贴板，请手动 Ctrl+V 粘贴到文本框'
  }
}

async function parseOnly() {
  error.value = ''
  msg.value = ''
  try {
    const { data } = await api.post<ParsePreview>('/tenants/parse', {
      api_text: paste.api_text,
      private_key_pem: paste.private_key_pem,
      name: paste.name,
      description: paste.description,
    })
    parsePreview.value = data
    if (data.ok && data.name && !paste.name) {
      // soft-fill name for display
      paste.name = data.name
    }
    // fill manual form too so user can tweak
    if (data.user_ocid) form.user_ocid = data.user_ocid
    if (data.tenancy_ocid) form.tenancy_ocid = data.tenancy_ocid
    if (data.fingerprint) form.fingerprint = data.fingerprint
    if (data.region) form.region = data.region
    if (data.compartment_ocid) form.compartment_ocid = data.compartment_ocid
    if (data.name) form.name = data.name
  } catch (e: any) {
    error.value = e?.message || '解析失败'
    parsePreview.value = null
  }
}

async function importPaste() {
  error.value = ''
  msg.value = ''
  saving.value = true
  try {
    const { data } = await api.post<Tenant>('/tenants/import', {
      api_text: paste.api_text,
      private_key_pem: paste.private_key_pem,
      name: paste.name,
      description: paste.description,
      test_connection: paste.test_connection,
    })
    msg.value = `已添加「${data.name}」`
    if (paste.test_connection) {
      try {
        const t = await api.post<{ ok: boolean; message: string }>(`/tenants/${data.id}/test`)
        if (t.data.ok) msg.value += ` · 连接测试：${t.data.message}`
        else error.value = `已保存，但连接测试失败：${t.data.message}`
      } catch (e: any) {
        error.value = `已保存，但连接测试失败：${e?.message || e}`
      }
    }
    showForm.value = false
    await load()
  } catch (e: any) {
    error.value = e?.message || '导入失败'
  } finally {
    saving.value = false
  }
}

async function saveManual() {
  error.value = ''
  msg.value = ''
  saving.value = true
  try {
    if (editingId.value) {
      const payload: Record<string, unknown> = {
        name: form.name,
        user_ocid: form.user_ocid,
        tenancy_ocid: form.tenancy_ocid,
        fingerprint: form.fingerprint,
        region: form.region,
        compartment_ocid: form.compartment_ocid,
        description: form.description,
        // Clearing the field yields '' which fails float validation with a raw
        // English 422 and rejects the entire edit; treat blank as 0 (alerts off).
        budget_monthly_usd: Number(form.budget_monthly_usd) || 0,
        free_only_mode: form.free_only_mode,
      }
      if (form.private_key_pem.trim()) {
        payload.private_key_pem = form.private_key_pem
      }
      const { data } = await api.patch<Tenant>(`/tenants/${editingId.value}`, payload)
      replaceTenant(data)
      msg.value = '已保存'
    } else {
      await api.post('/tenants', {
        name: form.name,
        user_ocid: form.user_ocid,
        tenancy_ocid: form.tenancy_ocid,
        fingerprint: form.fingerprint,
        region: form.region,
        private_key_pem: form.private_key_pem,
        compartment_ocid: form.compartment_ocid,
        description: form.description,
        budget_monthly_usd: Number(form.budget_monthly_usd) || 0,
        free_only_mode: form.free_only_mode,
      })
      msg.value = '已添加'
    }
    showForm.value = false
    await load()
  } catch (e: any) {
    error.value = e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/** 锁定/取消锁定：其他页面进入时默认选中被锁定的租户。 */
function toggleLock(t: Tenant) {
  msg.value = ''
  error.value = ''
  if (isTenantLocked(t.id)) {
    unlockTenant()
    msg.value = `已取消锁定「${t.name}」，各页面恢复为默认选第一个租户`
    return
  }
  lockTenant(t)
  msg.value = `已锁定「${t.name}」：实例 / 存储 / 创建实例 / 账号用量 进入时都会自动选它`
}

function closeRegions() {
  regionsFor.value = null
  regions.value = null
  regionsError.value = ''
  regionToAdd.value = ''
}

async function openRegions(t: Tenant) {
  regionsFor.value = t
  regions.value = null
  regionsError.value = ''
  regionToAdd.value = ''
  regionsLoading.value = true
  try {
    const { data } = await api.get<TenantRegions>(`/tenants/${t.id}/regions`)
    if (!data.ok) {
      regionsError.value = data.message || '读取区域失败'
      return
    }
    regions.value = data
  } catch (e: any) {
    regionsError.value = e?.message || '读取区域失败'
  } finally {
    regionsLoading.value = false
  }
}

/**
 * Subscribe (or, when `alreadySubscribed`, just add the panel row for) one region.
 * The server is idempotent about the Oracle side, so both cases hit one endpoint.
 */
async function subscribeRegion(region: string, alreadySubscribed: boolean) {
  const t = regionsFor.value
  if (!t || !region) return
  const question = alreadySubscribed
    ? `把已开通区域「${region}」添加为面板租户？\n\n` +
      `会用「${t.name}」的同一份 API 凭据创建一个副区租户。\n` +
      `注意：Always Free 只在主区生效，副区资源按量计费，因此该租户默认允许计费。`
    : `为「${t.name}」开通区域「${region}」？\n\n` +
      `① Oracle 开通后无法取消；\n` +
      `② 副区不属于 Always Free，其中的实例（含 A1.Flex）都会产生费用；\n` +
      `③ 面板会自动添加对应的副区租户（默认允许计费）。`
  if (!confirm(question)) return
  error.value = ''
  msg.value = ''
  regionBusy.value = region
  try {
    const { data } = await api.post<{ ok: boolean; message: string }>(
      `/tenants/${t.id}/regions/subscribe`,
      { region, confirm: true, add_tenant: true },
    )
    if (data.ok) {
      msg.value = data.message || '已开通'
      await load()
      await openRegions(t)
    } else {
      error.value = data.message || '开通副区失败'
    }
  } catch (e: any) {
    error.value = e?.message || '开通副区失败'
  } finally {
    regionBusy.value = ''
  }
}

async function test(t: Tenant) {
  error.value = ''
  msg.value = ''
  busy.value = t.id
  try {
    const { data } = await api.post<{ ok: boolean; message: string }>(`/tenants/${t.id}/test`)
    if (data.ok) msg.value = `${t.name}: ${data.message}`
    else error.value = `${t.name}: ${data.message}`
  } catch (e: any) {
    error.value = e?.message || '测试失败'
  } finally {
    busy.value = ''
  }
}

async function detectTier(t: Tenant) {
  error.value = ''
  msg.value = ''
  busy.value = t.id
  try {
    const { data } = await api.get(`/tenants/${t.id}/account`)
    const tier = data?.data?.tier || '未知'
    const reason = data?.data?.tier_reason || data?.message || ''
    msg.value = `${t.name}: ${tier}${reason ? ' — ' + reason : ''}`
    await load()
  } catch (e: any) {
    error.value = e?.message || '识别失败'
  } finally {
    busy.value = ''
  }
}

async function disableOciPasswordExpiry(t: Tenant) {
  if (
    !confirm(
      `对租户「${t.name}」调用 Oracle API，关闭控制台强制改密（默认约 120 天）？\n\n` +
        `会修改 Identity Domain 的 PasswordPolicy.passwordExpiresAfter。\n` +
        `需要 API 用户具备 Domain / 密码策略管理权限。`,
    )
  ) {
    return
  }
  error.value = ''
  msg.value = ''
  busy.value = t.id
  try {
    const { data } = await api.post<{
      ok: boolean
      message: string
      data?: Record<string, unknown>
    }>(`/tenants/${t.id}/oci-password-policy/disable-expiry`)
    if (data.ok) {
      msg.value = `${t.name}: ${data.message || '已关闭 Oracle 强制改密'}`
      // 立刻回读真实状态：PATCH 返回成功只说明请求被接受了。
      busy.value = ''
      await loadPasswordStatus(t)
      return
    } else {
      error.value = `${t.name}: ${data.message || '关闭强制改密失败'}`
    }
  } catch (e: any) {
    error.value = e?.message || '关闭强制改密失败'
  } finally {
    busy.value = ''
  }
}

async function remove(t: Tenant) {
  // Children share this row's credentials, so the server deletes them with it.
  const children = tenants.value.filter((c) => c.parent_tenant_id === t.id)
  const extra = children.length
    ? `\n\n同时会删除它的 ${children.length} 个副区租户：${children.map((c) => c.region).join('、')}\n` +
      `（仅移出面板，不会删除 Oracle 上的区域订阅或实例）`
    : ''
  if (!confirm(`删除租户「${t.name}」？${extra}`)) return
  error.value = ''
  busy.value = t.id
  try {
    const { data } = await api.delete<{ message: string }>(`/tenants/${t.id}`)
    msg.value = data?.message || '已删除'
    if (regionsFor.value?.id === t.id) closeRegions()
    await load()
  } catch (e: any) {
    error.value = e?.message || '删除失败'
  } finally {
    busy.value = ''
  }
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
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 0.35rem;
}
.parse-box {
  border-radius: 8px;
  padding: 0.65rem 0.8rem;
  font-size: 13px;
  border: 1px solid var(--border);
}
.parse-box.ok {
  background: var(--ok-soft);
  border-color: transparent;
  color: #0a6e22;
}
.parse-box.bad {
  background: var(--danger-soft);
  border-color: transparent;
  color: var(--danger);
}
:global(html[data-theme='dark']) .parse-box.ok {
  color: #7dffa8;
}
:global(html[data-theme='dark']) .parse-box.bad {
  color: #ffb0ad;
}
.manual-block {
  border-top: 1px solid var(--border);
  padding-top: 0.85rem;
  margin-top: 0.25rem;
}
.warn-text {
  color: var(--warn);
}
.sub-row td:first-child {
  padding-left: 0.35rem;
}
.sub-tree {
  margin-right: 0.15rem;
}
.sub-badge {
  margin-left: 0.35rem;
}
.pwd-line {
  margin-top: 0.25rem;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-wrap: wrap;
}
.pwd-pol {
  padding: 0.05rem 0.35rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.pwd-pol-main {
  border-color: var(--accent);
  color: var(--text);
}
code {
  font-size: 12px;
  background: var(--panel-2);
  padding: 0.05rem 0.3rem;
  border-radius: 4px;
}

</style>
