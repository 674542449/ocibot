<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>容量雷达</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          创建实例前先问 Oracle 各可用域还有没有货 ·
          <strong>只读，不会创建任何实例</strong> · 点「开始探测」才请求 Oracle
        </p>
      </div>
      <div class="page-tools">
        <select v-model="tenantId" :disabled="busy" @change="onTenantChange">
          <option v-if="!tenants.length" value="" disabled>请先添加租户</option>
          <option v-for="t in tenants" :key="t.id" :value="t.id">{{ t.name }} · {{ t.region }}</option>
        </select>
        <button class="primary" :disabled="busy || !tenantId" @click="probe">
          {{ busy ? (progress || '探测中…') : '开始探测' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <!-- 探测目标必须和结果同屏可见：容量报告是**按规格**出的，
         4C24G 无货不代表 1C6G 无货。参数看不见的话结果就没有意义。 -->
    <div class="card stack">
      <div class="row" style="justify-content: space-between; align-items: baseline">
        <strong style="font-size: 14px">探测目标</strong>
        <span class="muted" style="font-size: 12px">{{ RADAR_SHAPE }}（Always Free ARM）</span>
      </div>
      <div class="grid-2">
        <div class="field">
          <label>OCPU</label>
          <input v-model.number="form.ocpus" type="number" min="1" max="4" step="1" :disabled="busy" />
        </div>
        <div class="field">
          <label>内存 GB</label>
          <input v-model.number="form.memory" type="number" min="1" max="24" step="1" :disabled="busy" />
        </div>
      </div>
      <div class="row" style="gap: 0.4rem; flex-wrap: wrap">
        <button
          v-for="p in PRESETS"
          :key="p.label"
          type="button"
          class="ghost"
          :disabled="busy"
          @click="applyPreset(p)"
        >{{ p.label }}</button>
      </div>
    </div>

    <!-- 「还没探测」必须占据本该是网格的那块面积，并且直接把最危险的误读说出来。
         留白无论多克制都会被读成「探过了，没有容量」—— 那会让人放弃一台其实
         开得出来的机器。 -->
    <div v-if="!result && !busy" class="card stack">
      <div>
        <strong>还没有探测。上面的空白不代表没有容量。</strong>
        <p class="muted" style="margin: 0.35rem 0 0; font-size: 13px">
          点右上角「开始探测」后，面板会对该租户的每一个可用域各发
          <strong>一次只读请求</strong>（CreateComputeCapacityReport），
          问 Oracle 现在还开不开得出这个规格的机器。不会创建任何实例，也不占用任何额度。
        </p>
      </div>
      <div>
        <div class="muted" style="font-size: 12px; margin-bottom: 0.35rem">结果会用这四种状态表示：</div>
        <div class="legend">
          <span v-for="k in (['available','out_of_capacity','not_supported','unknown'] as RadarStatus[])" :key="k" class="legend-item">
            <span class="badge" :class="badgeClass(k)">{{ glyph(k) }} {{ statusText(k) }}</span>
            <span class="muted" style="font-size: 12px">{{ statusHint(k) }}</span>
          </span>
        </div>
      </div>
      <p class="muted" style="font-size: 12px; margin: 0">
        没有该租户的可用域列表时无法探测。若提示「请先加载配置」，去
        <router-link :to="{ path: '/launch', query: tenantId ? { tenant: tenantId } : {} }">创建实例</router-link>
        页点一次「加载配置」即可（那份列表会被缓存，雷达直接复用）。
      </p>
    </div>

    <template v-if="result">
      <div v-if="result.secondary_region" class="card warn-box">
        <strong>⚠ 这是一个副区租户</strong>
        <div class="muted" style="font-size: 12px; margin-top: 0.25rem">
          Always Free 只存在于主区。这里探到的「有货」指的是<strong>能不能开出来</strong>，
          不是「免费的有货」—— 副区创建的实例一律按量计费。
        </div>
      </div>

      <div v-if="result.retry_job_active" class="card warn-box">
        <strong>⚠ 这个租户有正在运行的抢机任务</strong>
        <div class="muted" style="font-size: 12px; margin-top: 0.25rem">
          容量报告和抢机用的是同一个 Oracle 请求速率桶。频繁探测会挤占抢机重试的预算，
          反而降低抢到机器的概率。
        </div>
      </div>

      <div v-if="staleMinutes >= 5" class="card warn-box">
        <strong>⚠ 这份结果已经 {{ staleMinutes }} 分钟了</strong>
        <div class="muted" style="font-size: 12px; margin-top: 0.25rem">
          A1 的免费容量以秒计变化。建议重新探测后再去创建。
        </div>
      </div>

      <div v-if="result.message" class="card muted" style="font-size: 13px">{{ result.message }}</div>

      <div class="radar-grid" role="status" aria-live="polite">
        <div
          v-for="r in result.results"
          :key="r.availability_domain"
          class="card ad-card"
          :class="`st-${r.status}`"
        >
          <div class="row" style="justify-content: space-between; align-items: baseline; gap: 0.5rem">
            <span class="badge" :class="badgeClass(r.status)">{{ glyph(r.status) }} {{ statusText(r.status) }}</span>
            <span class="muted mono ad-name" :title="r.availability_domain">{{ shortAd(r.availability_domain) }}</span>
          </div>

          <!-- reason 只在 unknown 时非空。红色在这一页只有一个含义：我们没能问到答案。 -->
          <p v-if="r.reason" class="reason">{{ r.reason }}</p>

          <div v-for="c in r.configs" :key="`${c.ocpus}-${c.memory_in_gbs}`" class="cfg-row">
            <span class="badge" :class="badgeClass(c.status)">{{ glyph(c.status) }}</span>
            <span :class="{ mono: true, primary: c.primary }">
              {{ fmtNum(c.ocpus) }} OCPU / {{ fmtNum(c.memory_in_gbs) }} GB
              <span v-if="c.primary" class="muted" style="font-size: 11px">（本次目标）</span>
            </span>
            <!-- available_count 对普通租户**恒为 null**。绝不渲染成 0：
                 「可开 0 台」和「无货」长得一样，而它真实的含义是
                 「有货，但 Oracle 不告诉你还剩几台」。 -->
            <span v-if="c.available_count != null" class="muted" style="font-size: 12px">
              可开 {{ c.available_count }} 台
            </span>
          </div>

          <!-- FD 只是证据，不是可选项：本项目创建实例时从不指定故障域，
               Oracle 自己挑。所以这里是只读芯片，不可点。 -->
          <div v-if="faultDomainsOf(r).length" class="fd-row">
            <span
              v-for="fd in faultDomainsOf(r)"
              :key="fd.fault_domain"
              class="badge fd-chip"
              :class="badgeClass(fd.status)"
              :title="`${fd.fault_domain}：${statusText(fd.status)}`"
            >{{ shortFd(fd.fault_domain) }} {{ glyph(fd.status) }}</span>
          </div>

          <div class="row" style="justify-content: space-between; align-items: center; margin-top: 0.15rem">
            <span class="muted" style="font-size: 11px">
              {{ r.cached ? `缓存 ${Math.round(r.cache_age_sec)} 秒前` : '刚刚探测' }}
            </span>
            <router-link
              v-if="r.status === 'available'"
              :to="launchLink(r.availability_domain)"
            ><button type="button" class="primary" style="padding: 0.2rem 0.6rem; font-size: 12px">去创建 →</button></router-link>
          </div>
        </div>
      </div>

      <p class="muted" style="font-size: 12px; margin: 0">
        容量报告是<strong>某一瞬间的快照，不是预留</strong>。探到有货不保证一定创建得成功
        （从看到结果到点下创建之间隔着一整个往返，而免费 A1 的窗口以秒计）；
        探到无货也不保证一定失败 —— Oracle 自己的 CLI 上有一个至今未关闭的
        issue 记录过 A1.Flex 报告结论倒置的案例。所以这一页给的是<strong>建议</strong>，
        不是许可：任何时候都可以直接去创建，被拒了就把它挂进抢机任务。
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, reactive } from 'vue'
import { useRoute } from 'vue-router'
import api, { type Tenant } from '@/api/client'
import { pickTenantId } from '@/stores/tenantLock'

const RADAR_SHAPE = 'VM.Standard.A1.Flex'

type RadarStatus = 'available' | 'out_of_capacity' | 'not_supported' | 'unknown'

type FaultDomain = { fault_domain: string; status: RadarStatus; available_count: number | null }
type ConfigResult = {
  ocpus: number
  memory_in_gbs: number
  primary: boolean
  status: RadarStatus
  /** 普通租户恒为 null —— 只有 DRCC / 白名单租户拿得到数字。null ≠ 0。 */
  available_count: number | null
  fault_domains: FaultDomain[]
}
type AdResult = {
  availability_domain: string
  status: RadarStatus
  /** 只在 status === 'unknown' 时非空。 */
  reason: string
  configs: ConfigResult[]
  cached: boolean
  cache_age_sec: number
}
type RadarResult = {
  ok: boolean
  shape: string
  region: string
  checked_at: string
  overall: RadarStatus
  results: AdResult[]
  retry_job_active: boolean
  secondary_region: boolean
  message: string
}

const route = useRoute()
const tenants = ref<Tenant[]>([])
const tenantId = ref('')
const busy = ref(false)
const progress = ref('')
const error = ref('')
const result = ref<RadarResult | null>(null)
const checkedAtMs = ref(0)

const form = reactive({ ocpus: 4, memory: 24 })

const PRESETS = [
  { label: '4C / 24G（免费上限）', ocpus: 4, memory: 24 },
  { label: '2C / 12G', ocpus: 2, memory: 12 },
  { label: '1C / 6G', ocpus: 1, memory: 6 },
]

function applyPreset(p: { ocpus: number; memory: number }) {
  form.ocpus = p.ocpus
  form.memory = p.memory
  // 规格变了，旧结论就不再是对这组参数的回答。留着它会让人照着一个
  // 属于别的规格的绿标去创建。
  result.value = null
}

/**
 * 请求序号 + 捕获租户，形状照抄 LaunchView.checkQuotaForForm。
 *
 * 不写 `if (busy.value) return` 就完事：那是本仓库修过的一类 bug —— 一次慢响应
 * 落在用户切走之后，会把 A 租户的结论渲染在 B 的名下。spinner 只按序号复位
 * （按租户 id 复位会在切回原租户时卡死）。
 */
let seq = 0

async function probe() {
  if (!tenantId.value) return
  const mine = ++seq
  const wanted = tenantId.value
  busy.value = true
  error.value = ''
  progress.value = ''
  try {
    const { data } = await api.post<RadarResult>(`/tenants/${wanted}/capacity-report`, {
      shape: RADAR_SHAPE,
      ocpus: form.ocpus,
      memory_in_gbs: form.memory,
      availability_domain: '',
    })
    if (mine !== seq || tenantId.value !== wanted) return
    result.value = data
    checkedAtMs.value = data.checked_at ? Date.parse(data.checked_at) : Date.now()
  } catch (e: any) {
    if (mine !== seq || tenantId.value !== wanted) return
    error.value = e?.message || '探测失败'
  } finally {
    if (mine === seq) busy.value = false
  }
}

function onTenantChange() {
  // 换租户必须整块清空。不清的话「去创建」按钮会带着 A 的可用域名跳进 B 的创建页，
  // 而那边的 applyTemplate 会静默丢掉一个不在 B 的 AD 列表里的值 ——
  // 用户以为参数带过去了，其实没有。
  result.value = null
  error.value = ''
  checkedAtMs.value = 0
}

const staleMinutes = computed(() => {
  if (!checkedAtMs.value) return 0
  return Math.floor((Date.now() - checkedAtMs.value) / 60000)
})

function faultDomainsOf(r: AdResult): FaultDomain[] {
  // 主配置的 FD 明细。可能是空数组 —— Oracle 只回一行 AD 级汇总时就没有 FD 可展示。
  const primary = r.configs.find((c) => c.primary) || r.configs[0]
  return primary?.fault_domains || []
}

function statusText(s: RadarStatus) {
  switch (s) {
    case 'available':
      return '有货'
    case 'out_of_capacity':
      return '暂无容量'
    case 'not_supported':
      return '不提供此机型'
    default:
      return '读不到'
  }
}

function statusHint(s: RadarStatus) {
  switch (s) {
    case 'available':
      return '现在开得出来（不是预留）'
    case 'out_of_capacity':
      return '等一会儿或挂抢机任务'
    case 'not_supported':
      return '这个可用域根本没有这种机器，等也没用'
    default:
      return '权限/限流等原因没问到答案，不代表没有容量'
  }
}

/** 每个状态同时给颜色 + 字形 + 中文词，不只靠颜色（色盲可读）。 */
function glyph(s: RadarStatus) {
  switch (s) {
    case 'available':
      return '●'
    case 'out_of_capacity':
      return '○'
    case 'not_supported':
      return '⊘'
    default:
      return '?'
  }
}

/**
 * 红色在这一页只有一个含义：**我们没能问到答案**。
 *
 * 「没货」用琥珀而不是红 —— 和任务中心保持一致（那边 a.capacity 为真时用的也是
 * 中性 badge，红色只留给真正的失败）。把「没货」染红会让人放弃一台等一会儿
 * 就能开出来的机器。
 */
function badgeClass(s: RadarStatus) {
  switch (s) {
    case 'available':
      return 'running'
    case 'out_of_capacity':
      return 'warn'
    case 'not_supported':
      return 'stopped'
    default:
      return 'err'
  }
}

function shortAd(ad: string) {
  // "kIdk:AP-TOKYO-1-AD-1" -> "AP-TOKYO-1-AD-1"；完整值在 title 里。
  const i = ad.indexOf(':')
  return i >= 0 ? ad.slice(i + 1) : ad
}

function shortFd(fd: string) {
  return fd.replace(/^FAULT-DOMAIN-/, 'FD')
}

function fmtNum(v: number) {
  return Number.isInteger(v) ? String(v) : String(v)
}

function launchLink(ad: string) {
  // query 的 key 名必须和 InstanceDetailView 的「同款创建」完全一致：
  // LaunchView 的 pendingTemplate/applyTemplate 认的就是这几个名字，
  // 它会在用户点「加载配置」之后逐项校验再填入。不传 fd —— 创建路径从不指定故障域。
  return {
    path: '/launch',
    query: {
      tenant: tenantId.value,
      from: 'radar',
      shape: RADAR_SHAPE,
      ocpus: String(form.ocpus),
      memory: String(form.memory),
      ad,
    },
  }
}

async function loadTenants() {
  const { data: rows } = await api.get<Tenant[]>('/tenants')
  tenants.value = rows
  if (tenantId.value && rows.some((t) => t.id === tenantId.value)) return
  tenantId.value = pickTenantId(rows, route.query.tenant)
}

// 进页面只拉租户列表（本地数据库，不碰 Oracle）。容量探测必须由用户显式点击 ——
// 0.4.20 加过进页自动拉取、0.4.21 整个回退了，理由是请求预算要留给抢机重试循环。
onMounted(() => {
  loadTenants().catch((e: any) => {
    error.value = e?.message || '加载租户失败'
  })
})
</script>

<style scoped>
.radar-grid {
  display: grid;
  /* min(100%, …) 那一版：窄屏上不会溢出。照抄 AccountView 的额度网格。 */
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
  gap: 0.75rem;
}

.ad-card {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  /* 左边缘 3px 状态色条：扫一眼一列边缘就能读出哪个可用域有货。 */
  border-left: 3px solid var(--border);
}
.ad-card.st-available {
  border-left-color: var(--ok);
}
.ad-card.st-out_of_capacity {
  border-left-color: var(--warn);
}
.ad-card.st-unknown {
  border-left-color: var(--danger);
}
.ad-card.st-not_supported {
  /* 斜纹在本项目已有的含义是「不是一个真的选项」（创建页额度条里的「本次占用」）。
     借用它，让这一档不靠颜色也能和「没货」分开 —— 对色盲用户同样成立。 */
  border-left-color: var(--text-secondary);
  background-image: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 6px,
    rgba(128, 128, 128, 0.07) 6px,
    rgba(128, 128, 128, 0.07) 12px
  );
}

.ad-name {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.cfg-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 13px;
  flex-wrap: wrap;
}
.cfg-row .primary {
  font-weight: 600;
}

.fd-row {
  display: flex;
  gap: 0.3rem;
  flex-wrap: wrap;
}
.fd-chip {
  font-size: 11px;
  padding: 0.1rem 0.4rem;
}

.reason {
  margin: 0;
  font-size: 12px;
  /* 后端的权限提示是分条写的（哪条 IAM 策略、为什么「测试连接」不算数）。 */
  white-space: pre-line;
  color: var(--text-secondary);
}

.legend {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
