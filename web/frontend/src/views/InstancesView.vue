<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>实例</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          选择租户后点击「刷新」加载 · 点名称进详情
        </p>
      </div>
      <div class="page-tools">
        <select v-model="tenantId">
          <option v-if="!tenants.length" value="" disabled>请先添加租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">{{ t.name }} · {{ t.region }}</option>
        </select>
        <input v-model="search" type="search" placeholder="搜索名称 / IP / OCID" />
        <label class="choice muted" style="flex: 0 0 auto">
          <input v-model="resolveIps" type="checkbox" />
          <span>解析 IP</span>
        </label>
        <button class="primary" :disabled="loading || !tenantId" @click="refresh">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
        <button type="button" :disabled="!filtered.length" @click="exportCsv">导出 CSV</button>
        <router-link :to="{ path: '/launch', query: tenantId ? { tenant: tenantId } : {} }">
          <button type="button" class="primary">创建实例</button>
        </router-link>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <!-- 列表不完整时必须说出来。返回一个看着合理的子集而不加任何标记，
         比直接报错更危险：操作者会以为看到的就是全部。 -->
    <div v-if="partialWarn" class="card warn-box">
      <strong>⚠ 这份列表可能不完整</strong>
      <div class="muted" style="font-size: 12px; margin-top: 0.25rem">{{ partialWarn }}</div>
    </div>
    <div v-if="msg" class="success-box">{{ msg }}</div>
    <div v-if="!loading && !loadedOnce && tenantId" class="card muted" style="font-size: 13px">
      为减少 Oracle API 调用，进入本页<strong>不会自动拉取实例</strong>。选择租户后点击右上角「刷新」。
    </div>

    <div v-if="selected.size > 0" class="card row batch-bar">
      <strong>已选 {{ selected.size }} 台</strong>
      <div class="btn-group">
        <button type="button" class="ghost" :disabled="batchBusy" @click="batchPower('START')">批量开机</button>
        <button type="button" class="ghost" :disabled="batchBusy" @click="batchPower('SOFTSTOP')">批量关机</button>
        <button type="button" class="ghost" :disabled="batchBusy" @click="batchPower('SOFTRESET')">批量重启</button>
        <button type="button" class="ghost" :disabled="batchBusy" @click="selected.clear()">取消选择</button>
      </div>
      <span v-if="batchBusy" class="muted" style="font-size: 12px">{{ batchProgress }}</span>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width: 34px">
              <input
                type="checkbox"
                :checked="allSelected"
                title="全选当前列表"
                @change="toggleSelectAll"
              />
            </th>
            <th>名称</th>
            <th>状态</th>
            <!-- 没有「租户」列：这张表一次只列一个租户（顶部下拉选的那个），
                 每行重复同一个名字既占宽度又没有信息量。全租户聚合的
                 GET /api/instances 是故意返回 400 的，不存在混合列表。 -->
            <th>Shape</th>
            <th>公网 IP</th>
            <th>私网 IP</th>
            <th>创建时间</th>
            <th>root 密码</th>
            <th class="action-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && filtered.length === 0">
            <td colspan="9" class="muted empty">
              <template v-if="instances.length">没有匹配搜索的实例</template>
              <template v-else-if="partialWarn">
                没有读到任何实例，而且本次读取是不完整的 —— 这更可能是权限问题，
                而不是「这个账号没有实例」。请到租户页点「测试连接」确认。
              </template>
              <template v-else>暂无实例。请先在「租户」添加 API，再「创建实例」。</template>
            </td>
          </tr>
          <tr v-for="ins in filtered" :key="ins.id + ins.tenant_id">
            <td>
              <input
                type="checkbox"
                :checked="selected.has(selKey(ins))"
                @change="toggleSelect(ins)"
              />
            </td>
            <td class="name-cell">
              <!-- 截断而不是折行：名称是唯一长度完全由用户决定的字段，
                   一台机器起个长名字就会把整行撑高。完整值在 title 里。 -->
              <router-link
                class="name-link"
                :to="`/instances/${ins.tenant_id || tenantId}/${ins.id}`"
                :title="ins.display_name"
              >{{ ins.display_name }}</router-link>
            </td>
            <td>
              <span class="badge" :class="stateClass(ins.lifecycle_state)">{{ ins.lifecycle_state }}</span>
            </td>
            <td>
              {{ ins.shape }}
              <span v-if="ins.free_tier_tag" class="badge">{{ ins.free_tier_tag }}</span>
              <!-- 行内，不是 <div>：独立一行的规格会让「弹性 shape」的行比
                   固定 shape 的行高一截，同一张表里出现两种行高。 -->
              <span v-if="specText(ins)" class="muted spec">{{ specText(ins) }}</span>
            </td>
            <td>
              <span
                class="copyable"
                :class="{ empty: !ins.public_ip }"
                title="单击复制公网 IPv4"
                role="button"
                tabindex="0"
                @click="copyIp(ins.public_ip, $event)"
                @keydown.enter.prevent="copyIp(ins.public_ip)"
              >{{ ins.public_ip || '—' }}</span>
              <!-- IPv6 跟 IPv4 同一行（不再另起一行），并且只显示首尾两段：
                   完整地址最长 39 字符，一行一台机器的表里会把「操作」列
                   顶出屏幕。悬停看完整地址，单击复制的也是完整地址。 -->
              <span
                v-for="ip6 in ins.ipv6_addresses || []"
                :key="ip6"
                class="copyable ipv6-chip"
                :title="`单击复制 IPv6：${ip6}`"
                role="button"
                tabindex="0"
                @click="copyIp(ip6, $event)"
                @keydown.enter.prevent="copyIp(ip6)"
              >{{ shortIpv6(ip6) }}</span>
            </td>
            <td>
              <span
                class="copyable"
                :class="{ empty: !ins.private_ip }"
                title="单击复制私网 IP"
                role="button"
                tabindex="0"
                @click="copyIp(ins.private_ip, $event)"
                @keydown.enter.prevent="copyIp(ins.private_ip)"
              >{{ ins.private_ip || '—' }}</span>
            </td>
            <td>
              <!-- 精确到秒的本地时间；原始 UTC 时间戳放在 title 里，方便和
                   Oracle 控制台核对。 -->
              <span class="created-cell" :title="ins.time_created || ''">
                {{ formatCreated(ins.time_created) }}
              </span>
            </td>
            <td>
              <!-- 密码模式创建时写在实例标签里；密钥模式没有这个值。
                   默认打码，点一下才显示 —— 列表常开着，也常被截图。 -->
              <div class="pwd-wrap">
                <template v-if="ins.root_password">
                  <span
                    class="copyable pwd-cell"
                    :title="revealed.has(ins.id) ? '单击复制 root 密码' : '单击显示 root 密码'"
                    role="button"
                    tabindex="0"
                    @click="onPasswordClick(ins, $event)"
                    @keydown.enter.prevent="onPasswordClick(ins)"
                  >{{ revealed.has(ins.id) ? ins.root_password : '••••••••' }}</span>
                </template>
                <span v-else class="muted">—</span>
                <!-- Editable because the note is written once at launch and goes
                     stale the moment the password is changed over SSH. Shown for
                     key-mode instances too: those start with no note, and there is
                     no reason the operator cannot record one later. -->
                <button
                  type="button"
                  class="ghost pwd-edit"
                  :title="ins.root_password ? '修改密码备注' : '记录 root 密码'"
                  :aria-label="`${ins.display_name} 密码备注`"
                  :disabled="pwdBusy === ins.id"
                  @click="editPassword(ins)"
                >
                  {{ pwdBusy === ins.id ? '…' : '改' }}
                </button>
              </div>
            </td>
            <td class="action-cell">
              <div class="btn-group" role="group" :aria-label="`${ins.display_name} 操作`">
                <router-link :to="`/instances/${ins.tenant_id || tenantId}/${ins.id}`">
                  <button type="button" class="ghost" title="查看详情">详情</button>
                </router-link>
                <button
                  type="button"
                  class="ghost"
                  :disabled="acting === ins.id"
                  title="开机"
                  @click="power(ins, 'START')"
                >
                  开机
                </button>
                <button
                  type="button"
                  class="ghost"
                  :disabled="acting === ins.id"
                  title="软关机"
                  @click="power(ins, 'SOFTSTOP')"
                >
                  关机
                </button>
                <button
                  type="button"
                  class="ghost"
                  :disabled="acting === ins.id"
                  title="软重启"
                  @click="power(ins, 'SOFTRESET')"
                >
                  重启
                </button>
                <button
                  type="button"
                  class="ghost"
                  :disabled="acting === ins.id"
                  title="重命名"
                  @click="rename(ins)"
                >
                  重命名
                </button>
                <!-- 没有「换IP」：换公网 IP 会释放旧地址，是个不可撤销的动作，
                     放在列表里一排小按钮中间太容易点错。详情页有「换公网IP」，
                     那里同时能看到当前地址、IPv6 和实例状态。 -->
                <button
                  type="button"
                  class="danger"
                  :disabled="acting === ins.id"
                  title="终止实例"
                  @click="terminate(ins)"
                >
                  终止
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="muted" style="font-size: 12px; margin: 0">
      共 {{ filtered.length }} / {{ instances.length }} 台 · 单击公网 / 私网 / IPv6 可复制
    </p>
  </div>
</template>

<style scoped>
/* ── 一行一台机器 ────────────────────────────────────────────────
   这张表是用来「扫」的：行高一致时，眼睛沿着一列往下走就能比出状态、
   IP、创建时间；只要有一行因为自己的参数（长名字、弹性规格、多个 IPv6）
   变高，节奏就断了，而且高的那几行看起来像是更重要。
   所以宽度不够时一律横向滚动（外层 .table-wrap 本来就 overflow:auto），
   绝不折行。 */
.table-wrap td {
  white-space: nowrap;
}

/* 例外：空列表那一格是整句话，让它正常折行，否则一句提示就把表撑到
   要横向滚动才看得完。 */
.table-wrap td.empty {
  white-space: normal;
}

/* flex 容器的换行不受 white-space 管，得单独关掉 —— 否则按钮组在窄屏
   下会折成两排，正是要避免的那种「某几行特别高」。 */
.table-wrap .btn-group {
  flex-wrap: nowrap;
}

/* 名称是唯一长度完全由用户决定的字段。给一个上限并省略号截断，
   nowrap 才不会把整张表拉到几屏宽。 */
.name-cell {
  max-width: 20rem;
}

.name-link {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  color: var(--text);
  text-decoration: none;
}

/* 规格跟在 shape 后面同一行，压小一号以免抢了 shape 名字的位置。 */
.spec {
  font-size: 12px;
  margin-left: 0.35rem;
}

.ipv6-chip {
  font-size: 11px;
  margin-left: 0.35rem;
}

/* ── 「操作」列吸附在右边缘 ──────────────────────────────────────
   不折行的代价是表格变宽（实测约 1670px）。1440 的笔记本上可用宽度只有
   1130 左右，最右边这一列会被推出可视区 —— 偏偏它就是要点的那一列。
   吸附之后横向滚动只影响中间那几个只读字段，按钮始终停在原地。

   两个坑：
   - 背景必须**不透明**，否则滚到下面去的单元格会透上来。
   - 分隔线用 inset box-shadow，不用 border：表格是 border-collapse:
     collapse，合并后的边框由 <table> 绘制，不会跟着吸附的单元格移动。
   用 .action-cell 而不是 :last-child，是因为空列表那一行的 colspan 单元格
   也是它那一行的最后一格，不该被吸附。 */
.table-wrap th.action-col,
.table-wrap td.action-cell {
  position: sticky;
  right: 0;
  background: var(--panel);
  box-shadow: inset 1px 0 0 var(--border), inset 0 -1px 0 var(--border);
}

/* 最后一行本来就没有下边框（tbody tr:last-child td），别用阴影补一条出来。 */
.table-wrap tbody tr:last-child td.action-cell {
  box-shadow: inset 1px 0 0 var(--border);
}

/* 右上角这一格要同时压住两个方向：表头已经 sticky 到顶部并占了 z-index:1。 */
.table-wrap th.action-col {
  z-index: 2;
  background: var(--panel-2);
}

/* 行 hover 的底色也得给吸附的这一格补上，否则整行变色时它自己不变。 */
.table-wrap tbody tr:hover td.action-cell {
  background: var(--row-hover);
}

/* 深色下表头用的是半透明底（styles.css 里的 `html[data-theme='dark'] th`，
   rgba(24,25,30,.94)），吸附的格子不能半透明，换成叠加后的同色实色，
   免得右上角和表头其余部分差出一块。

   这里**不要**写成 `:global(html[data-theme='dark']) .table-wrap …`：
   Vue 编译这种「:global 开头 + 后代」的写法会把后代部分整个丢掉，只剩下
   `html[data-theme=dark]{…}`，等于把样式糊到 <html> 上（本项目另有 9 处
   老代码踩了这个坑）。作用的元素本来就在本组件里，直接写祖先选择器即可，
   scope 属性会挂在最后一段上。 */
html[data-theme='dark'] .table-wrap th.action-col {
  background: #191a1e;
}

/* 定宽等宽字体 + 不换行：秒级时间戳在窄屏上会被折成两行，同一列的数字就
   对不齐了，扫一眼比较先后的用途也就没了。表格外层是 .table-wrap，横向
   滚动本来就有。 */
.created-cell {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  white-space: nowrap;
}

.pwd-cell {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: nowrap;
}

/* The edit control keeps a fixed slot whether or not a password is recorded, so
   the column width does not change between rows — a width that depends on the
   cell's contents makes the whole table reflow when one note is cleared. */
.pwd-wrap {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.pwd-edit {
  flex: 0 0 auto;
  min-width: 1.9rem;
  padding: 0.05rem 0.35rem;
  font-size: 12px;
  line-height: 1.6;
}
/* Anchored to the viewport, not to the document. As a sticky card in the flow it
   appeared above the table and pushed every row down the moment you ticked a
   checkbox — the row you had just clicked slid out from under the cursor. Here it
   floats over the content, so selecting changes nothing about the table's
   position. */
.batch-bar {
  position: fixed;
  left: 50%;
  bottom: max(1.25rem, env(safe-area-inset-bottom));
  transform: translateX(-50%);
  z-index: 30;
  gap: 0.9rem;
  width: max-content;
  max-width: min(calc(100vw - 2rem), 760px);
  padding: 0.7rem 1.1rem;
  border-radius: 999px;
  border: 1px solid var(--border-strong);
  box-shadow: var(--shadow-md);
  background: var(--panel-solid);
}

.batch-bar strong {
  font-size: 13px;
  white-space: nowrap;
}

/* The rail is fixed-width, so centring on the viewport would sit slightly left
   of the content it acts on. Nudge it back over the table. */
@media (min-width: 901px) {
  .batch-bar {
    left: calc(50% + var(--sidebar-w) / 2);
  }
}
</style>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import api, { type Instance, type Tenant } from '@/api/client'
import { pickTenantId } from '@/stores/tenantLock'
import { copyText, showToast } from '@/utils/toast'

const route = useRoute()
const tenants = ref<Tenant[]>([])
const instances = ref<Instance[]>([])
const tenantId = ref('')
// Default off: listing without public/private IP resolution is much cheaper on OCI.
const resolveIps = ref(false)
const search = ref('')
const loading = ref(false)
const loadedOnce = ref(false)
/** 非空表示这份列表**不完整**（有 compartment 读不到）。 */
const partialWarn = ref('')
const acting = ref('')
const error = ref('')
const msg = ref('')
const selected = reactive(new Set<string>())
const batchBusy = ref(false)
const batchProgress = ref('')

function selKey(ins: Instance) {
  return `${ins.tenant_id || tenantId.value}|${ins.id}`
}

const allSelected = computed(
  () => filtered.value.length > 0 && filtered.value.every((i) => selected.has(selKey(i))),
)

function toggleSelect(ins: Instance) {
  const key = selKey(ins)
  if (selected.has(key)) selected.delete(key)
  else selected.add(key)
}

function toggleSelectAll() {
  if (allSelected.value) {
    selected.clear()
  } else {
    for (const ins of filtered.value) selected.add(selKey(ins))
  }
}

async function batchPower(action: string) {
  const targets = filtered.value.filter((i) => selected.has(selKey(i)))
  if (!targets.length) return
  const verb: Record<string, string> = { START: '开机', SOFTSTOP: '关机', SOFTRESET: '重启' }
  if (!confirm(`对已选 ${targets.length} 台实例执行「${verb[action] || action}」？`)) return
  error.value = ''
  msg.value = ''
  batchBusy.value = true
  let okCount = 0
  const failures: string[] = []
  try {
    // Sequential on purpose: keeps request rate gentle on the OCI API.
    for (let i = 0; i < targets.length; i++) {
      const ins = targets[i]
      batchProgress.value = `${i + 1}/${targets.length} · ${ins.display_name}`
      try {
        await api.post(`/tenants/${ins.tenant_id || tenantId.value}/instances/${ins.id}/power`, {
          action,
        })
        okCount++
      } catch (e: any) {
        failures.push(`${ins.display_name}: ${e?.message || '失败'}`)
      }
    }
    msg.value = `批量${verb[action] || action}完成：${okCount}/${targets.length} 成功`
    if (failures.length) error.value = failures.join('；')
    selected.clear()
    await load()
  } finally {
    batchBusy.value = false
    batchProgress.value = ''
  }
}

const filtered = computed(() => {
  // Backend already drops TERMINATED; keep a client-side guard for older API caches.
  let list = instances.value.filter(
    (ins) => String(ins.lifecycle_state || '').toUpperCase() !== 'TERMINATED',
  )
  const q = search.value.trim().toLowerCase()
  if (!q) return list
  return list.filter((ins) => {
    const blob = [
      ins.display_name,
      ins.id,
      ins.shape,
      ins.public_ip,
      ins.private_ip,
      ins.tenant_name,
      ins.lifecycle_state,
      ...(ins.ipv6_addresses || []),
    ]
      .join(' ')
      .toLowerCase()
    return blob.includes(q)
  })
})

/** 实例创建时间 → 本地时区的 `YYYY-MM-DD HH:mm:ss`（精确到秒）。
 *  刻意不用 toLocaleString()：它的输出随浏览器语言变，而这一列要能直接和
 *  Oracle 控制台、以及导出的 CSV 逐字对比。 */
function formatCreated(v?: string | null) {
  const raw = String(v || '').trim()
  if (!raw) return '—'
  // 后端返回 ISO 8601；老版本 API 缓存里可能还是空格分隔的形式，一并兜住。
  const d = new Date(raw.includes('T') ? raw : raw.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return raw
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  )
}

/** 弹性 shape 的规格，压成一段行内文本；固定 shape 两个字段都是 null，
 *  返回空串，模板就整个不渲染 —— 空的 `·` 比不显示更难读。 */
function specText(ins: Instance) {
  const parts: string[] = []
  if (ins.ocpus != null) parts.push(`${ins.ocpus} OCPU`)
  if (ins.memory_in_gbs != null) parts.push(`${ins.memory_in_gbs} GB`)
  return parts.join(' · ')
}

/** IPv6 缩写成「首段:…:末段」。前缀认子网、后缀认机器，这两段就够在列表里
 *  区分同一台机器的多个地址；完整值走 title 和复制，一个字符都不丢。
 *  段数不足 3（含 `::1` 这类高度压缩的写法）时原样返回，缩了反而更长。 */
function shortIpv6(ip: string) {
  const groups = String(ip || '')
    .split(':')
    .filter(Boolean)
  if (groups.length < 3) return ip
  return `${groups[0]}:…:${groups[groups.length - 1]}`
}

function stateClass(state: string) {
  const s = (state || '').toUpperCase()
  if (s === 'RUNNING') return 'running'
  if (s === 'STOPPED') return 'stopped'
  if (s.includes('STOP') || s.includes('START') || s === 'PROVISIONING') return 'warn'
  if (s.includes('TERMINAT')) return 'err'
  return ''
}

function exportCsv() {
  const rows = filtered.value
  const header = [
    'display_name',
    'lifecycle_state',
    'tenant_name',
    'shape',
    'ocpus',
    'memory_in_gbs',
    'public_ip',
    'private_ip',
    'region',
    'time_created',
    'id',
  ]
  const lines = [header.join(',')]
  for (const ins of rows) {
    const vals = [
      ins.display_name,
      ins.lifecycle_state,
      ins.tenant_name,
      ins.shape,
      ins.ocpus ?? '',
      ins.memory_in_gbs ?? '',
      ins.public_ip,
      ins.private_ip,
      ins.region,
      // 与列表里显示的一致（本地时区、精确到秒），而不是原始 UTC 串。
      formatCreated(ins.time_created),
      ins.id,
    ].map((v) => `"${String(v ?? '').replace(/"/g, '""')}"`)
    lines.push(vals.join(','))
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `ocibot-instances-${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
  showToast(`已导出 ${rows.length} 行`, 'ok')
}

async function copy(text: string) {
  await copyText(text)
}

function copyIp(text?: string | null, ev?: Event) {
  if (ev) {
    ev.preventDefault()
    ev.stopPropagation()
  }
  if (!text) return
  void copyText(text, '已复制 IP')
}

async function loadTenants() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data
  // Default to the locked tenant (else the first); if the current selection was
  // deleted, fall back the same way.
  if (data.length && (!tenantId.value || !data.some((t) => t.id === tenantId.value))) {
    tenantId.value = pickTenantId(data, route.query.tenant)
  }
}

/** 已点开显示明文的实例 id。刷新列表不清空，避免每次刷新又要重点一遍。 */
const revealed = reactive(new Set<string>())

/** 第一次点显示，之后点复制。 */
async function onPasswordClick(ins: Instance, ev?: MouseEvent) {
  if (!ins.root_password) return
  if (!revealed.has(ins.id)) {
    revealed.add(ins.id)
    return
  }
  await copyText(ins.root_password, 'root 密码已复制')
  void ev
}

/** Which row's note is being written, so its button can show progress. */
const pwdBusy = ref('')

/**
 * Edit the remembered root password.
 *
 * It is a memo stored in an OCI freeform tag — nothing authenticates with it —
 * so this only fixes what the panel displays; it does NOT change the password on
 * the machine. The prompt says so, because "改密码" and "改密码备注" are one
 * character apart and the consequences of confusing them are not.
 */
async function editPassword(ins: Instance) {
  const current = ins.root_password || ''
  const next = window.prompt(
    `记录 ${ins.display_name} 的 root 密码。\n` +
      `仅更新面板显示，不会修改服务器上的密码。\n` +
      `留空则清除备注。`,
    current,
  )
  if (next === null) return // cancelled
  const value = next.trim()
  if (value === current) return
  pwdBusy.value = ins.id
  try {
    const { data } = await api.post<{ ok: boolean; message: string }>(
      `/tenants/${ins.tenant_id || tenantId.value}/instances/${ins.id}/root-password`,
      { root_password: value },
    )
    if (!data.ok) {
      showToast(data.message || '更新失败', 'err', 5000)
      return
    }
    // Update in place rather than reloading the whole list: a full refresh here
    // would spend an Oracle round trip per row to redisplay one changed cell.
    ins.root_password = value
    if (!value) revealed.delete(ins.id)
    showToast(data.message || '已更新密码备注', 'ok', 2500)
  } catch (e: any) {
    showToast(e?.message || '更新失败', 'err', 5000)
  } finally {
    pwdBusy.value = ''
  }
}

let loadSeq = 0

/** Manual 刷新: clear the banners, then load. */
function refresh() {
  error.value = ''
  msg.value = ''
  void load()
}

async function load() {
  // Deliberately does NOT clear msg/error: power, rename, terminate and the bulk
  // actions all call load() right after succeeding, so clearing here wiped the
  // very success message (and the bulk failure list) before it could be read.
  // Each of those handlers already clears both refs at entry.
  if (!tenantId.value) {
    instances.value = []
    loading.value = false
    if (!tenants.value.length) {
      error.value = '还没有租户。请先到「租户」页添加 API 配置。'
    }
    return
  }
  loading.value = true
  const seq = ++loadSeq
  // Also capture the tenant: the sequence alone does not catch a switch that
  // happens without starting a new load, which would render tenant A's
  // instances under tenant B's selection.
  const wanted = tenantId.value
  const superseded = () => seq !== loadSeq || tenantId.value !== wanted
  try {
    const res = await api.get<Instance[]>(`/tenants/${tenantId.value}/instances`, {
      params: { resolve_ips: resolveIps.value, include_subcompartments: true },
    })
    // Discard out-of-order responses from a superseded tenant switch.
    if (superseded()) return
    instances.value = res.data
    loadedOnce.value = true
    // 后端在「有 compartment 读不到」时会带上这两个头。没有它，一次被拒绝的
    // 读取会返回空数组，页面照样渲染「暂无实例。请先在「租户」添加 API」——
    // 把权限问题说成空账号，还让人去做一件早就做过的事。部分成功更隐蔽：
    // 列表看着正常，只是少了几台。
    const partial = res.headers?.['x-ocibot-partial']
    partialWarn.value = partial
      ? decodeURIComponent(String(res.headers?.['x-ocibot-partial-reason'] || '')) ||
        '部分 compartment 读取失败'
      : ''
  } catch (e: any) {
    if (superseded()) return
    error.value = e?.message || '加载失败'
    instances.value = []
    partialWarn.value = ''
  } finally {
    // The spinner is owned by SEQUENCE only. Gating it on the tenant check too
    // meant that switching tenant mid-request left loading=true forever: no newer
    // load() had started to take ownership, and 刷新 is :disabled="loading", so
    // the page became permanently unrefreshable.
    if (seq === loadSeq) loading.value = false
    if (!superseded()) {
      // Drop selections that no longer exist in the refreshed list.
      const live = new Set(instances.value.map((i) => selKey(i)))
      for (const key of [...selected]) {
        if (!live.has(key)) selected.delete(key)
      }
    }
  }
}

async function power(ins: Instance, action: string) {
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/power`, { action })
    msg.value = data.message || `${action} 已提交`
    await load()
  } catch (e: any) {
    error.value = e?.message || '操作失败'
  } finally {
    acting.value = ''
  }
}

async function rename(ins: Instance) {
  const name = prompt('新名称', ins.display_name)
  if (!name || !name.trim()) return
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/rename`, {
      display_name: name.trim(),
    })
    msg.value = data.message || '已重命名'
    await load()
  } catch (e: any) {
    error.value = e?.message || '重命名失败'
  } finally {
    acting.value = ''
  }
}

async function terminate(ins: Instance) {
  if (!confirm(`确认终止实例「${ins.display_name}」？此操作危险且可能不可恢复。`)) return
  error.value = ''
  msg.value = ''
  acting.value = ins.id
  try {
    const tid = ins.tenant_id || tenantId.value
    const { data } = await api.post(`/tenants/${tid}/instances/${ins.id}/terminate`, {
      preserve_boot_volume: false,
    })
    msg.value = data.message || '终止已提交'
    await load()
  } catch (e: any) {
    error.value = e?.message || '终止失败'
  } finally {
    acting.value = ''
  }
}

let bootstrapped = false

watch(tenantId, (id) => {
  // Changing tenant clears the previous list; user must click 刷新 to hit OCI.
  instances.value = []
  loadedOnce.value = false
  selected.clear()
  if (!id && !tenants.value.length) {
    error.value = '还没有租户。请先到「租户」页添加 API 配置。'
  } else {
    error.value = ''
  }
})

// Toggling IP resolution only applies on the next manual refresh.
watch(resolveIps, () => {
  if (loadedOnce.value) {
    msg.value = '已切换「解析 IP」；请点击「刷新」重新加载实例列表。'
  }
})

onMounted(async () => {
  try {
    await loadTenants()
    // Intentionally do NOT auto-call OCI list on enter.
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  } finally {
    bootstrapped = true
  }
})
</script>
