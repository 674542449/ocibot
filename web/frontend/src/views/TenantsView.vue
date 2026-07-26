<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>租户 / API 配置</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          粘贴 OCI config；私钥仅服务端加密存储
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
          <tr v-for="t in tenants" :key="t.id">
            <td>
              <span class="dot" :style="{ background: t.color }"></span>
              {{ t.name }}
            </td>
            <td>{{ t.region }}</td>
            <td>{{ tierLabel(t.account_tier) }}</td>
            <td class="muted" style="font-size: 12px; word-break: break-all">
              {{ shortId(t.tenancy_ocid) }}
            </td>
            <td>
              <span class="badge" :class="t.enabled ? 'running' : 'stopped'">
                {{ t.enabled ? '启用' : '禁用' }}
              </span>
            </td>
            <td>
              <div class="row">
                <button :disabled="busy === t.id" @click="test(t)">测试连接</button>
                <button :disabled="busy === t.id" @click="detectTier(t)">识别等级</button>
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
import { onMounted, reactive, ref } from 'vue'
import api, { type Tenant } from '@/api/client'
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
})

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
        budget_monthly_usd: form.budget_monthly_usd,
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
  if (!confirm(`删除租户「${t.name}」？`)) return
  error.value = ''
  busy.value = t.id
  try {
    await api.delete(`/tenants/${t.id}`)
    msg.value = '已删除'
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
code {
  font-size: 12px;
  background: var(--panel-2);
  padding: 0.05rem 0.3rem;
  border-radius: 4px;
}

</style>
