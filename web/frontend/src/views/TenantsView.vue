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
        <!-- 批量删除放在页头而不是表格上方弹出一条操作栏：那条栏一出现就把整张表
             往下推，刚勾选的那一行会从鼠标底下滑走（同 showToast 那段注释）。 -->
        <button
          class="danger"
          :disabled="doomedTenants.length === 0 || batchBusy"
          :title="doomedTenants.length ? '' : '先在表格左侧勾选要删除的租户'"
          @click="removeSelected"
        >
          {{
            batchBusy
              ? '删除中…'
              : doomedTenants.length
                ? `删除所选（${doomedTenants.length}）`
                : '批量删除'
          }}
        </button>
        <button class="primary" @click="openCreate">添加租户</button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th class="sel-col">
              <input
                type="checkbox"
                :checked="allSelected"
                :indeterminate="someSelected"
                :disabled="selectableTenants.length === 0 || batchBusy"
                title="全选（已开启删除保护的租户不会被选中）"
                @change="toggleSelectAll"
              />
            </th>
            <th>名称</th>
            <th>区域</th>
            <th>等级</th>
            <th>密码到期</th>
            <th>Tenancy</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="tenants.length === 0">
            <td colspan="8" class="muted empty">还没有租户，点击右上角添加，或粘贴原始 API 配置。</td>
          </tr>
          <tr v-for="t in orderedTenants" :key="t.id" :class="{ 'sub-row': !!t.parent_tenant_id }">
            <td class="sel-col">
              <!-- 副区跟着主租户一起删，所以主租户被勾上时副区显示为已选且锁定，
                   让「会删掉哪些」在点按钮之前就看得见。 -->
              <input
                type="checkbox"
                :checked="selected.has(t.id) || selectedViaParent(t)"
                :disabled="!!deleteBlockReason(t) || selectedViaParent(t) || batchBusy"
                :title="
                  deleteBlockReason(t)
                    ? deleteBlockReason(t) + '，不能删除'
                    : selectedViaParent(t)
                      ? '主租户已选中，副区会随它一起删除'
                      : '选择'
                "
                @change="toggleSelect(t)"
              />
            </td>
            <td class="name-cell">
              <span v-if="t.parent_tenant_id" class="muted sub-tree">└</span>
              <span class="dot" :style="{ background: t.color }"></span>
              {{ t.name }}
              <span v-if="t.parent_tenant_id" class="badge sub-badge">副区</span>
              <span
                class="badge running lock-flag"
                :class="{ 'is-off': !isTenantLocked(t.id) }"
                title="其他页面默认使用该租户"
              >
                <Icon name="lock" :size="12" /> 默认
              </span>
            </td>
            <td>
              {{ t.region }}
              <span v-if="t.region_label && t.region_label !== t.region" class="muted">
                · {{ t.region_label }}
              </span>
            </td>
            <td>{{ tierLabel(t.account_tier) }}</td>
            <!-- Own column on purpose: rendered inside 状态 it added a second line
                 on query, growing the row and pushing every row below it down.
                 Here the header reserves the width and the cell is simply empty
                 until queried, so nothing moves. -->
            <td class="pwd-cell">
              <!-- Always a badge, even before the query: a plain "—" placeholder is
                   shorter than a badge, so the first result grew the row. The
                   placeholder keeps a CJK glyph so its line box matches the real
                   value's exactly. -->
              <span
                class="badge"
                :class="
                  pwdStatus[t.id] ? (pwdStatus[t.id].days > 0 ? 'warn' : 'running') : 'pwd-empty'
                "
              >
                {{
                  pwdStatus[t.id]
                    ? pwdStatus[t.id].days > 0
                      ? pwdStatus[t.id].days + ' 天'
                      : '未设置'
                    : '未查询'
                }}
              </span>
            </td>
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
              <span
                v-if="t.delete_protected"
                class="badge protect-badge"
                title="已开启删除保护：单个删除、批量删除都会被拒绝，需先解除"
              >
                删除保护
              </span>
            </td>
            <td>
              <div class="row row-actions">
                <button
                  :class="{ primary: !isTenantLocked(t.id) }"
                  :title="isTenantLocked(t.id)
                    ? '取消后各页面恢复为默认选第一个租户'
                    : '锁定后，实例 / 存储 / 创建实例 / 账号用量 进入时都自动选它'"
                  @click="toggleLock(t)"
                >
                  {{ isTenantLocked(t.id) ? '取消锁定' : '锁定为默认' }}
                </button>
                <!-- 诊断权限问题的入口。以前 /tenants/{id}/test 全站只有一个调用点:
                     粘贴导入时那个「保存后自动测试连接」复选框。手动添加、编辑、
                     以及任何已存在的租户都没有办法再跑一次 —— 而这恰恰是
                     NotAuthorizedOrNotFound 时唯一能说清「到底哪一项读不到」的检查。 -->
                <button :disabled="busy === t.id" @click="testTenant(t)">测试连接</button>
                <button :disabled="busy === t.id" @click="detectTier(t)">等级查询</button>
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
                  title="读取 defaultPasswordPolicy 的到期天数；若仍在强制改密，会自动关闭后再读一次"
                  @click="passwordExpiry(t)"
                >
                  密码到期查询
                </button>
                <button :disabled="busy === t.id" @click="openEdit(t)">编辑</button>
                <!-- 两种状态的文案字数相同，切换不会改变按钮宽度（见 .row-actions 的说明）。 -->
                <button
                  :disabled="busy === t.id"
                  :title="
                    t.delete_protected
                      ? '解除后才能删除该租户'
                      : '开启后该租户无法被删除（单个删除、批量删除都会被拒绝）'
                  "
                  @click="toggleProtect(t)"
                >
                  {{ t.delete_protected ? '解除保护' : '删除保护' }}
                </button>
                <button
                  class="danger"
                  :disabled="busy === t.id || !!deleteBlockReason(t)"
                  :title="deleteBlockReason(t) ? deleteBlockReason(t) + '，请先解除' : ''"
                  @click="remove(t)"
                >
                  删除
                </button>
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
import Icon from '@/components/Icon.vue'
import {
  isTenantLocked,
  lockTenant,
  syncLockedTenantName,
  unlockTenant,
} from '@/stores/tenantLock'
import { pickAndReadTextFile } from '@/utils/file'
// Row actions report through the fixed-position toast host, never through the
// in-flow banner above the table: that banner appearing pushed the whole table
// down, so the row you just clicked moved out from under the cursor.
import { showToast } from '@/utils/toast'

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
  free_only_mode: true,
})

const regionsFor = ref<Tenant | null>(null)
const regions = ref<TenantRegions | null>(null)
const regionsLoading = ref(false)
const regionsError = ref('')
const regionBusy = ref('')
const regionToAdd = ref('')
/** 递增序号：只有最新一次 openRegions 的响应可以写 regions。 */
let regionsSeq = 0
/**
 * `regions` 里那份列表**属于哪个租户**。
 *
 * 「副区管理」按钮只在 `busy === t.id` 时禁用，而 openRegions 设的是
 * regionsLoading，所以读取期间每一行的按钮都还能点。先点 A 再点 B，若 A 的
 * 响应后到，标题显示的是 B、表格里却是 A 的区域列表 —— 而开通请求是打给
 * regionsFor（B）的。后果不是显示错乱那么轻：区域一旦开通**Oracle 侧无法
 * 取消**（见 app/oci_client.py::subscribe_region 的说明），而且服务端是拿
 * B 的真实已开通列表重新判断的，A 那边「已开通」的状态不作数，于是那句
 * 「把已开通区域…添加为面板租户」的确认文案承诺了不会动 Oracle，实际却在
 * 一个用户根本没选中的租户上创建了不可逆的订阅。
 */
const regionsOwner = ref('')

/** 开启保护的先后；0.4.116 里开的保护没有时间，退回按添加时间。 */
function protectedSince(t: Tenant): number {
  return Date.parse(t.delete_protected_at || t.created_at) || 0
}

/**
 * 开了删除保护的排在最前，多个之间先保护的在前；其余保持添加顺序
 * （Array.prototype.sort 是稳定排序，比较结果为 0 的不会动）。
 */
function byProtection(a: Tenant, b: Tenant): number {
  if (a.delete_protected !== b.delete_protected) return a.delete_protected ? -1 : 1
  return a.delete_protected ? protectedSince(a) - protectedSince(b) : 0
}

/**
 * Primaries (protected first, see byProtection), each immediately followed by
 * its 副区 rows sorted the same way. A protected 副区 moves to the top of its own
 * group, not above its primary — the tree would stop reading as a tree.
 * This order is local to this page on purpose: the server's list order decides
 * which tenant other pages open with when nothing is locked (pickTenantId).
 */
const orderedTenants = computed(() => {
  const primaries = tenants.value.filter((t) => !t.parent_tenant_id).sort(byProtection)
  const out: Tenant[] = []
  for (const p of primaries) {
    out.push(p)
    out.push(...tenants.value.filter((c) => c.parent_tenant_id === p.id).sort(byProtection))
  }
  // Orphans (parent deleted out-of-band) must still be listed, not silently hidden.
  const seen = new Set(out.map((t) => t.id))
  out.push(...tenants.value.filter((t) => !seen.has(t.id)).sort(byProtection))
  return out
})

// ---- 多选 / 批量删除 / 删除保护 ----

/** 用户**亲手**勾选的租户 id。副区随主租户删除的那部分不在这里，见 selectedViaParent。 */
const selected = reactive(new Set<string>())
const batchBusy = ref(false)

type BatchDeleteResult = {
  message: string
  deleted: string[]
  skipped: { id: string; name: string; reason: string }[]
}

/**
 * 这一行现在为什么不能删（'' = 可以删）。和服务端 _delete_block_reason 同一套规则：
 * 副区跟着主租户一起删，所以任何一个副区开了保护，主租户也删不了 —— 否则删主租户
 * 就成了绕过副区保护的后门。服务端会再判一次，这里只是让按钮提前变灰。
 */
function deleteBlockReason(t: Tenant): string {
  if (t.delete_protected) return '已开启删除保护'
  const guarded = tenants.value.filter((c) => c.parent_tenant_id === t.id && c.delete_protected)
  if (guarded.length) return `副区「${guarded.map((c) => c.name).join('、')}」已开启删除保护`
  return ''
}

function selectedViaParent(t: Tenant): boolean {
  return !!t.parent_tenant_id && selected.has(t.parent_tenant_id)
}

const selectableTenants = computed(() => orderedTenants.value.filter((t) => !deleteBlockReason(t)))

/** 点「删除所选」后真正会被删掉的行：勾选的 + 随被勾选主租户一起走的副区。 */
const doomedTenants = computed(() =>
  orderedTenants.value.filter((t) => selected.has(t.id) || selectedViaParent(t)),
)

const allSelected = computed(
  () =>
    selectableTenants.value.length > 0 &&
    selectableTenants.value.every((t) => selected.has(t.id) || selectedViaParent(t)),
)
const someSelected = computed(() => doomedTenants.value.length > 0 && !allSelected.value)

function toggleSelect(t: Tenant) {
  if (selected.has(t.id)) selected.delete(t.id)
  else if (!deleteBlockReason(t)) selected.add(t.id)
}

function toggleSelectAll() {
  if (allSelected.value) {
    selected.clear()
    return
  }
  for (const t of selectableTenants.value) selected.add(t.id)
}

/**
 * 丢掉已经不存在、或者已经变成不可删的选择。列表重载、开启保护之后都要跑一次，
 * 否则「删除所选（N）」里会数进一个服务端必然拒绝的租户。
 */
function pruneSelection() {
  for (const id of [...selected]) {
    const t = tenants.value.find((x) => x.id === id)
    if (!t || deleteBlockReason(t)) selected.delete(id)
  }
}

async function toggleProtect(t: Tenant) {
  const next = !t.delete_protected
  if (!next && !confirm(`解除「${t.name}」的删除保护？\n\n解除后即可删除该租户。`)) return
  busy.value = t.id
  try {
    const { data } = await api.post<Tenant>(`/tenants/${t.id}/delete-protection`, {
      protected: next,
    })
    replaceTenant(data)
    pruneSelection()
    showToast(next ? `已为「${t.name}」开启删除保护` : `已解除「${t.name}」的删除保护`)
  } catch (e: any) {
    showToast(e?.message || '操作失败', 'err', 5000)
  } finally {
    busy.value = ''
  }
}

async function removeSelected() {
  const doomed = doomedTenants.value
  if (!doomed.length) return
  // 只发亲手勾选的 id：副区由服务端跟着主租户删，重复发也会被去重，但没必要。
  const ids = orderedTenants.value.filter((t) => selected.has(t.id)).map((t) => t.id)
  const viaParent = doomed.filter((t) => !selected.has(t.id))
  const lines = doomed
    .slice(0, 15)
    .map((t) => `· ${t.name}（${t.region}）${t.parent_tenant_id ? ' [副区]' : ''}`)
  if (doomed.length > 15) lines.push(`… 另有 ${doomed.length - 15} 个`)
  const extra = viaParent.length ? `\n\n其中 ${viaParent.length} 个副区会随主租户一起删除。` : ''
  if (
    !confirm(
      `批量删除以下 ${doomed.length} 个租户？\n\n${lines.join('\n')}${extra}\n\n` +
        '（仅移出面板，不会删除 Oracle 上的区域订阅或实例）',
    )
  ) {
    return
  }
  batchBusy.value = true
  try {
    const { data } = await api.post<BatchDeleteResult>('/tenants/batch-delete', { ids })
    for (const id of data.deleted) selected.delete(id)
    if (regionsFor.value && data.deleted.includes(regionsFor.value.id)) closeRegions()
    // 正在编辑的租户被删了，表单再保存只会得到一个 404。
    if (editingId.value && data.deleted.includes(editingId.value)) showForm.value = false
    if (data.skipped.length) {
      // 界面上已经不让勾选受保护的租户，走到这里说明别处（另一个标签页）刚改过保护状态。
      const why = data.skipped.map((s) => `${s.name || s.id}：${s.reason}`).join('；')
      showToast(`${data.message}。${why}`, 'err', 8000)
    } else {
      showToast(data.message)
    }
    await load()
  } catch (e: any) {
    showToast(e?.message || '批量删除失败', 'err', 5000)
  } finally {
    batchBusy.value = false
  }
}

/** 每个租户的 defaultPasswordPolicy 到期天数（0 = 未设置 = 永不过期）。 */
type PwdStatus = { days: number }
const pwdStatus = reactive<Record<string, PwdStatus>>({})

type PwdPolicy = { name: string; days: number; is_default: boolean; is_template: boolean }

/** 读取 defaultPasswordPolicy 的天数；读不到就退回面板算出的结论。 */
async function readPasswordDays(tenantId: string): Promise<number | null> {
  const { data } = await api.get<{
    ok: boolean
    message: string
    data?: {
      effective?: { expires?: boolean; days?: number; all_policies?: PwdPolicy[] }
      errors?: string[]
    }
  }>(`/tenants/${tenantId}/oci-password-policy`)
  const eff = data.data?.effective
  if (!eff) throw new Error(data.message || '未能读取密码策略')
  const def = (eff.all_policies || []).find((p) => p.is_default)
  // defaultPasswordPolicy 是控制台登录真正生效的那条；找不到它时才退回结论值。
  return def ? def.days : eff.expires ? eff.days ?? 0 : 0
}

/**
 * 密码到期查询：读 defaultPasswordPolicy 的天数。
 *
 * 若仍设着有效期，顺手调用关闭强制改密再读一次 —— 这两步合并成一个按钮是
 * 操作者要求的工作流（他要的结果始终是「关掉」）。按钮提示里写明了会执行关闭，
 * 免得一个叫「查询」的动作意外改了 Oracle 配置。
 */
async function passwordExpiry(t: Tenant) {
  busy.value = t.id
  try {
    let days = await readPasswordDays(t.id)
    if (days && days > 0) {
      const { data } = await api.post<{ ok: boolean; message: string }>(
        `/tenants/${t.id}/oci-password-policy/disable-expiry`,
      )
      if (!data.ok) {
        showToast(`${t.name}: 当前 ${days} 天后过期，关闭失败：${data.message || '未知原因'}`, 'err', 5000)
        pwdStatus[t.id] = { days }
        return
      }
      days = await readPasswordDays(t.id)
    }
    pwdStatus[t.id] = { days: days ?? 0 }
    showToast(
      (days ?? 0) > 0
        ? `${t.name}: defaultPasswordPolicy = ${days} 天`
        : `${t.name}: defaultPasswordPolicy 未设置有效期（密码不会过期）`,
    )
  } catch (e: any) {
    showToast(e?.message || '密码到期查询失败', 'err', 5000)
  } finally {
    busy.value = ''
  }
}

function tierLabel(t: string) {
  return { paid: '升级', free: '免费' }[t] || '未知'
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
  form.free_only_mode = t.free_only_mode ?? true
  formKeyFile.value = ''
  showForm.value = true
}

async function load() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data
  pruneSelection()
  // This page lists every tenant instead of picking one, so pickTenantId never
  // runs here and the sidebar chip would show a lock with no name after a fresh
  // sign-in — the session carries the id, not the name.
  syncLockedTenantName(data)
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
  saving.value = true
  try {
    const { data } = await api.post<Tenant>('/tenants/import', {
      api_text: paste.api_text,
      private_key_pem: paste.private_key_pem,
      name: paste.name,
      description: paste.description,
      test_connection: paste.test_connection,
    })
    showToast(`已添加「${data.name}」`)
    if (paste.test_connection) {
      try {
        const t = await api.post<{ ok: boolean; message: string }>(`/tenants/${data.id}/test`)
        if (t.data.ok) showToast(`连接测试：${t.data.message}`, 'ok', 4000)
        else showToast(`已保存，但连接测试失败：${t.data.message}`, 'err', 6000)
      } catch (e: any) {
        showToast(`已保存，但连接测试失败：${e?.message || e}`, 'err', 6000)
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
        free_only_mode: form.free_only_mode,
      }
      if (form.private_key_pem.trim()) {
        payload.private_key_pem = form.private_key_pem
      }
      const { data } = await api.patch<Tenant>(`/tenants/${editingId.value}`, payload)
      replaceTenant(data)
      showToast('已保存')
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
        free_only_mode: form.free_only_mode,
      })
      showToast('已添加')
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
  if (isTenantLocked(t.id)) {
    unlockTenant()
    showToast(`已取消锁定「${t.name}」，各页面恢复为默认选第一个租户`)
    return
  }
  lockTenant(t)
  showToast(`已锁定「${t.name}」：实例 / 存储 / 创建实例 / 账号用量 进入时都会自动选它`)
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
  regionsOwner.value = ''
  regionsError.value = ''
  regionToAdd.value = ''
  regionsLoading.value = true
  const seq = ++regionsSeq
  try {
    const { data } = await api.get<TenantRegions>(`/tenants/${t.id}/regions`)
    // 被更新的一次 openRegions 取代了，这份响应必须丢掉：写进去就会出现
    // 「标题是 B、列表是 A」的错配，而开通请求是打给标题那个租户的。
    if (seq !== regionsSeq) return
    if (!data.ok) {
      regionsError.value = data.message || '读取区域失败'
      return
    }
    regions.value = data
    regionsOwner.value = t.id
  } catch (e: any) {
    if (seq !== regionsSeq) return
    regionsError.value = e?.message || '读取区域失败'
  } finally {
    // spinner 只认序号，不认租户：若把租户判断也加进来，切换途中的那次请求
    // 回来时不满足条件，regionsLoading 就再也没人关掉了（InstancesView
    // 的 load() 踩过同一个坑，那里的注释写了原委）。
    if (seq === regionsSeq) regionsLoading.value = false
  }
}

/**
 * Subscribe (or, when `alreadySubscribed`, just add the panel row for) one region.
 * The server is idempotent about the Oracle side, so both cases hit one endpoint.
 */
async function subscribeRegion(region: string, alreadySubscribed: boolean) {
  const t = regionsFor.value
  if (!t || !region) return
  // 第二道闸：开通是**不可逆**的，所以在发请求前再确认一次「屏幕上这份区域
  // 列表确实属于当前这个租户」。openRegions 的序号守卫已经挡住了错配，这里
  // 兜住任何以后新增的、绕过它的写入路径 —— 代价是一次比较，收益是不会在
  // 用户没选中的账号上创建永久订阅。
  if (regionsOwner.value !== t.id) {
    showToast('区域列表与当前租户不一致，请重新打开「副区管理」后再试', 'err', 6000)
    return
  }
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
  regionBusy.value = region
  try {
    const { data } = await api.post<{ ok: boolean; message: string }>(
      `/tenants/${t.id}/regions/subscribe`,
      { region, confirm: true, add_tenant: true },
    )
    if (data.ok) {
      showToast(data.message || '已开通', 'ok', 4000)
      await load()
      await openRegions(t)
    } else {
      showToast(data.message || '开通副区失败', 'err', 6000)
    }
  } catch (e: any) {
    showToast(e?.message || '开通副区失败', 'err', 6000)
  } finally {
    regionBusy.value = ''
  }
}

async function testTenant(t: Tenant) {
  busy.value = t.id
  error.value = ''
  try {
    const { data } = await api.post<{ ok: boolean; message: string }>(`/tenants/${t.id}/test`)
    if (data.ok) {
      showToast(`${t.name}：${data.message}`, 'ok', 4000)
    } else {
      // 失败的诊断刻意**不**走 toast：它有 300 字左右，而 showToast 会截断到
      // 200 字，被砍掉的恰好是「该加哪条策略」那半句 —— 整条信息里唯一可操作的
      // 部分。而且 toast 6 秒自动消失、点一下是关闭而不是选中，没法复制去查。
      error.value = `${t.name}：${data.message}`
    }
  } catch (e: any) {
    error.value = `${t.name}：${e?.message || '测试失败'}`
  } finally {
    busy.value = ''
  }
}

async function detectTier(t: Tenant) {
  busy.value = t.id
  try {
    const { data } = await api.get(`/tenants/${t.id}/account`)
    const tier = data?.data?.tier || '未知'
    const reason = data?.data?.tier_reason || data?.message || ''
    showToast(`${t.name}: ${tier}${reason ? ' — ' + reason : ''}`, 'ok', 3000)
    await load()
  } catch (e: any) {
    showToast(e?.message || '识别失败', 'err', 5000)
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
  busy.value = t.id
  try {
    const { data } = await api.delete<{ message: string }>(`/tenants/${t.id}`)
    showToast(data?.message || '已删除')
    selected.delete(t.id)
    if (regionsFor.value?.id === t.id) closeRegions()
    await load()
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'err', 5000)
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e: any) {
    showToast(e?.message || '加载失败', 'err', 5000)
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
/* Fixed width so a result never resizes the column: a wider column narrows the
   actions column until its buttons wrap, which grows EVERY row in the table. */
.pwd-cell {
  font-size: 12px;
  white-space: nowrap;
  width: 5.6rem;
  min-width: 5.6rem;
}
.pwd-empty {
  visibility: hidden;
}

/* The 🔒 默认 badge keeps its space when hidden. Rendering it conditionally
   changed the name column's width on click, which narrowed the actions column
   until its buttons wrapped — every row in the table grew by 21px. Reserving
   the slot makes locking a pure repaint. */
.lock-flag.is-off {
  visibility: hidden;
}
.lock-flag {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
}
/* 操作按钮保持一行：表格外层已有横向滚动，宁可让这一列滚动，
   也不让按钮折成两行把每行撑高一倍。 */
.row-actions {
  flex-wrap: nowrap;
  gap: 0.35rem;
}
.row-actions button {
  white-space: nowrap;
}
.sel-col {
  width: 34px;
}
/* 勾选列和「删除保护」按钮让表格又宽了一截。没有下限的话，表格一挤，中文名称
   会被压到按字折行（一格两三个字、行高翻倍）；给个下限，宁可让表格横向滚动。
   状态列反过来**不要** nowrap：三个徽章排一行有 200px，正是它把表格挤爆的。 */
.name-cell {
  min-width: 8rem;
}
.protect-badge {
  color: var(--accent);
  background: var(--accent-soft);
  /* 徽章之间可以换行，徽章自己不能从中间断成「删除 / 保护」两行。 */
  white-space: nowrap;
}
/* 全局的 html[data-theme='dark'] .badge 优先级更高，会把颜色压回灰色。
   不能写成 :global(html[data-theme='dark']) .protect-badge —— Vue 的 :global()
   会吞掉后半截，编译出来是一条裸的 html[data-theme="dark"] 规则。普通后代选择器
   在 scoped 样式里只会给最后一段加 data-v 属性，正好是想要的。 */
html[data-theme='dark'] .protect-badge {
  color: var(--accent);
  background: var(--accent-soft);
}
code {
  font-size: 12px;
  background: var(--panel-2);
  padding: 0.05rem 0.3rem;
  border-radius: 4px;
}

</style>
