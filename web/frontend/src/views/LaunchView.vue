<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>创建实例</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          选择租户后点「加载配置」再创建 · 不自动请求 Oracle API
        </p>
      </div>
      <div class="page-tools">
        <router-link to="/"><button type="button">返回实例列表</button></router-link>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <!-- One-time root password: shown until the user confirms they saved it.
         The server never returns it again, so this must not auto-dismiss. -->
    <div v-if="pendingPassword" class="card stack password-reveal">
      <h3 style="margin: 0">请立即保存 root 密码</h3>
      <p class="muted" style="margin: 0; font-size: 13px">
        该密码仅显示一次，服务端不保存明文。离开本页后无法再次查看。
      </p>
      <div class="row" style="gap: 0.5rem; align-items: center; flex-wrap: wrap">
        <code class="password-value">{{ pendingPassword }}</code>
        <button type="button" @click="copyPassword">复制</button>
      </div>
      <div class="row">
        <button class="primary" type="button" @click="dismissPassword">
          我已保存密码，返回实例列表
        </button>
      </div>
    </div>

    <!-- Confirm step -->
    <div v-if="confirmOpen" class="card stack confirm-panel">
      <h3 style="margin: 0">确认创建配置</h3>
      <p class="muted" style="margin: 0; font-size: 13px">
        请再次核对下列参数。确认后才会向 Oracle 提交 LaunchInstance。
      </p>
      <div class="table-wrap">
        <table>
          <tbody>
            <tr v-for="row in confirmRows" :key="row[0]">
              <th class="confirm-k">{{ row[0] }}</th>
              <td style="word-break: break-all">{{ row[1] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="muted warn-text" style="margin: 0; font-size: 12px">
        ⚠ 若勾选了「允许外网直接访问」，系统会放宽云端安全组与系统内防火墙；容量重试会持续调用创建接口，请确认间隔与次数。
        服务端会按 Always Free 额度拦截超额创建（免费/未知账号硬拦；已付费账号仅警告）。
      </p>
      <div v-if="quotaPreview" class="card" style="padding: 0.65rem; font-size: 12px">
        <div class="row" style="justify-content: space-between">
          <strong>{{ quotaPreview.secondary_region ? '副区（按量计费）' : '免费额度预览' }}</strong>
          <span
            v-if="!quotaPreview.secondary_region"
            class="badge"
            :class="quotaPreview.overall_status === 'ok' ? 'running' : 'warn'"
          >
            {{ quotaPreview.overall_status || '—' }}
          </span>
        </div>
        <div class="muted" style="margin-top: 0.25rem">
          {{ (quotaPreview.summary_lines || []).slice(0, 4).join(' · ') || '—' }}
        </div>
        <p v-if="quotaLoadError" class="muted warn-text" style="margin: 0.35rem 0 0">
          {{ quotaLoadError }}
        </p>
      </div>
      <div class="row">
        <button class="primary" :disabled="submitting" @click="doLaunch">
          {{ submitting ? '提交中，请稍候…' : '确认并创建' }}
        </button>
        <button type="button" :disabled="submitting" @click="confirmOpen = false">返回修改</button>
      </div>
      <p v-if="submitting" class="muted" style="margin: 0; font-size: 12px">
        正在准备网络 / NSG 并提交 LaunchInstance，通常需要数秒到一两分钟。请勿关闭页面或重复点击。
      </p>
    </div>

    <div v-show="!confirmOpen" class="card stack">
      <div class="grid-2">
        <div class="field">
          <label>租户 *</label>
          <select v-model="primaryId" @change="onTenantPicked">
            <option disabled value="">选择租户</option>
            <option v-for="t in primaryTenants" :key="t.id" :value="t.id">
              {{ t.name }} · {{ t.region }}
            </option>
          </select>
        </div>
        <div class="field">
          <label>区域（主区 / 副区）</label>
          <select v-model="tenantId" :disabled="!primaryId" @change="onRegionPicked">
            <option v-for="t in regionRows" :key="t.id" :value="t.id">
              {{ t.region }}{{ t.region_label && t.region_label !== t.region ? ' · ' + t.region_label : '' }}
              {{ t.parent_tenant_id ? '（副区）' : '（主区）' }}
            </option>
          </select>
          <p class="field-hint">
            副区需先在「租户」页开通。Always Free 只在主区生效，副区实例按量计费。
          </p>
        </div>
        <div class="field">
          <label>显示名称</label>
          <input v-model="form.display_name" />
        </div>
      </div>

      <div v-if="isSecondaryRegion" class="card warn-panel">
        <strong>已选择副区 {{ selectedTenant?.region }}</strong>
        <p class="muted" style="margin: 0.25rem 0 0; font-size: 12px">
          该区域不属于 Always Free：这里创建的实例（包括 A1.Flex）会按量计费，免费额度面板也不适用。
          若该副区租户仍勾选着「仅使用免费额度」，服务端会拒绝创建。
        </p>
      </div>

      <div class="row" style="margin-top: -0.25rem">
        <button
          type="button"
          class="primary"
          :disabled="!tenantId || loadingMeta"
          @click="loadMeta(true)"
        >
          {{ loadingMeta ? '加载中…' : meta ? '重新加载配置' : '加载配置（镜像 / Shape / 网络）' }}
        </button>
        <span v-if="!meta && tenantId" class="muted" style="font-size: 12px">
          为减少 API 调用，进入本页不会自动拉取租户元数据
        </span>
        <button
          v-if="!isSecondaryRegion"
          type="button"
          :disabled="!tenantId || loadingQuota"
          @click="loadQuotaPreview"
        >
          {{ loadingQuota ? '读取额度中…' : '刷新免费额度' }}
        </button>
      </div>

      <!-- Account free-tier usage, visible while configuring (not only at confirm). -->
      <div v-if="quotaPreview" class="card quota-panel">
        <div class="row" style="justify-content: space-between; align-items: baseline">
          <strong v-if="quotaPreview.secondary_region">副区额度说明</strong>
          <strong v-else>该账号 Always Free 已用额度</strong>
          <span class="row" style="gap: 0.4rem; align-items: center">
            <template v-if="!quotaPreview.secondary_region">
              <span v-if="quotaPreview.account_tier" class="badge">
                {{ quotaPreview.account_tier === 'paid' ? '付费账号' : '免费账号' }}
              </span>
              <span
                class="badge"
                :class="quotaPreview.overall_status === 'ok' ? 'running' : 'warn'"
              >{{ quotaStatusLabel(quotaPreview.overall_status) }}</span>
            </template>
            <span class="badge" :class="freeOnly ? 'running' : 'warn'">
              {{ freeOnly ? '仅免费额度' : '允许超额计费' }}
            </span>
          </span>
        </div>
        <p v-if="quotaPreview.read_incomplete" class="muted warn-text" style="margin: 0.3rem 0 0; font-size: 12px">
          ⚠ 用量读取不完整（Oracle API 报错或限流），下列数字可能偏低，提交时服务端会拒绝创建。
        </p>
        <!-- A 副区 has no free allowance of its own, so per-region gauges would be
             read as free headroom that does not exist. -->
        <p v-if="quotaPreview.secondary_region" class="muted warn-text" style="margin: 0.3rem 0 0; font-size: 12px">
          副区 {{ quotaPreview.region }} 不适用 Always Free 额度（主区 {{ quotaPreview.home_region }}），
          此处资源按量计费。
        </p>
        <div v-else class="quota-grid">
          <div v-for="q in quotaRows" :key="q.key" class="quota-item">
            <div class="quota-label">{{ q.label }}</div>
            <div class="quota-bar">
              <div
                class="quota-fill"
                :class="q.status"
                :style="{ width: Math.min(100, q.ratio * 100) + '%' }"
              />
            </div>
            <div class="quota-nums">
              已用 <strong>{{ q.used }}</strong> / {{ q.limit }}{{ q.unit }}
              <span class="muted">（剩余 {{ q.remaining }}{{ q.unit }}）</span>
            </div>
          </div>
        </div>
        <p v-if="quotaLoadError" class="muted warn-text" style="margin: 0.35rem 0 0; font-size: 12px">
          {{ quotaLoadError }}
        </p>
      </div>

      <!-- Pre-submit verdict for the CURRENT form, from the server's own guard. -->
      <div v-if="quotaVerdict && quotaVerdict.blocked" class="error-box" style="white-space: pre-line">
        <strong>当前配置会超出免费额度，已阻止提交：</strong>
        {{ (quotaVerdict.errors || []).join('\n') }}
      </div>
      <div
        v-else-if="quotaVerdict && (quotaVerdict.warnings || []).length"
        class="card"
        style="padding: 0.6rem; font-size: 12px"
      >
        <strong>{{ freeOnly ? '提醒：' : '该租户已允许超额（可能计费）：' }}</strong>
        {{ (quotaVerdict.warnings || []).join('；') }}
      </div>

      <div v-if="loadingMeta" class="muted">正在加载镜像 / Shape / 网络…</div>
      <div v-if="meta?.network_note" class="muted" style="font-size: 13px">
        网络：{{ meta.network_note }}
        <span v-if="meta.network_created">（已自动创建）</span>
        <span v-if="meta.cached"> · 缓存 {{ meta.cache_age_sec }}s</span>
      </div>

      <div class="field">
        <label>快捷配置（免费套餐）</label>
        <div class="row">
          <button
            v-for="p in presets"
            :key="p.id"
            type="button"
            @click="applyPreset(p)"
          >
            {{ p.label }}
          </button>
          <button type="button" :disabled="!tenantId || loadingMeta" @click="loadMeta(true)">
            刷新元数据
          </button>
        </div>
        <p v-if="presetHint" class="muted" style="margin: 0.35rem 0 0; font-size: 12px">{{ presetHint }}</p>
      </div>

      <div class="grid-2">
        <div class="field">
          <label>Availability Domain *</label>
          <select v-model="form.availability_domain">
            <option v-for="ad in ads" :key="ad" :value="ad">{{ ad }}</option>
          </select>
        </div>
        <div class="field">
          <label>操作系统</label>
          <select v-model="osFamily" @change="onOsFamilyChange">
            <option value="ubuntu">Ubuntu</option>
            <option value="oracle_linux">Oracle Linux</option>
            <option value="custom">自定义镜像（已有镜像）</option>
          </select>
        </div>
        <div class="field">
          <label>镜像 *</label>
          <select v-model="form.image_id" @change="onImageChange">
            <option v-for="img in images" :key="img.id" :value="img.id">{{ img.label || img.display_name }}</option>
          </select>
        </div>
        <div class="field">
          <label>Shape *</label>
          <select v-model="form.shape" @change="onShapeChange">
            <option v-for="s in compatibleShapes" :key="s.shape" :value="s.shape">
              {{ s.label || s.shape }}{{ s.free_tag ? ' · ' + s.free_tag : '' }}
            </option>
          </select>
        </div>
        <div class="field">
          <label>Boot 性能 VPUs/GB</label>
          <select v-model.number="form.boot_volume_vpus_per_gb">
            <option v-for="v in vpuPresets" :key="v.value" :value="v.value">{{ v.label }}</option>
          </select>
        </div>
        <div class="field">
          <label>OCPU{{ isFlex ? '（Flex）' : '（固定）' }}</label>
          <input
            v-model.number="form.ocpus"
            type="number"
            step="1"
            min="1"
            :disabled="!isFlex"
            :title="isFlex ? '' : '此 Shape 为固定规格，不可修改 OCPU'"
          />
        </div>
        <div class="field">
          <label>内存 GB{{ isFlex ? '（Flex）' : '（固定）' }}</label>
          <input
            v-model.number="form.memory_in_gbs"
            type="number"
            step="1"
            min="1"
            :disabled="!isFlex"
            :title="isFlex ? '' : '此 Shape 为固定规格，不可修改内存'"
          />
        </div>
        <div class="field">
          <label>Boot Volume GB（可选，≥50）</label>
          <input v-model="form.boot_volume_size_in_gbs" type="number" min="50" placeholder="留空≈默认" />
        </div>
      </div>
      <p v-if="shapeSpecHint" class="muted" style="margin: 0; font-size: 12px">{{ shapeSpecHint }}</p>

      <div class="field">
        <label>Root 登录方式</label>
        <div class="choice-group">
          <label class="choice">
            <input v-model="form.auth_mode" type="radio" value="key" />
            <span>root + SSH 公钥</span>
          </label>
          <label class="choice">
            <input v-model="form.auth_mode" type="radio" value="password" />
            <span>root + 密码</span>
          </label>
        </div>
      </div>

      <div v-if="form.auth_mode === 'key'" class="field">
        <label>SSH 公钥 *</label>
        <div class="row" style="margin-bottom: 0.4rem">
          <button type="button" @click="pickSshKey">选择 .pub 文件…</button>
          <span v-if="sshFile" class="muted" style="font-size: 12px">{{ sshFile }}</span>
        </div>
        <textarea v-model="form.ssh_public_key" rows="3" spellcheck="false" placeholder="ssh-ed25519 AAAA... comment"></textarea>
      </div>

      <div v-else class="field">
        <label>root 密码（至少 12 位；留空将自动生成）</label>
        <div class="row">
          <input v-model="form.root_password" style="flex:1" />
          <button type="button" @click="genPassword">随机生成</button>
        </div>
      </div>

      <div class="field">
        <label>网络与访问</label>
        <div class="choice-group">
          <label class="choice muted">
            <input v-model="form.assign_public_ip" type="checkbox" />
            <span>分配公网 IPv4</span>
          </label>
          <label class="choice muted">
            <input v-model="form.assign_ipv6_ip" type="checkbox" />
            <span>分配 IPv6</span>
          </label>
          <label class="choice muted">
            <input v-model="form.open_guest_firewall" type="checkbox" />
            <span>允许外网直接访问（放宽防火墙）</span>
          </label>
        </div>
        <p class="field-hint">
          勾选后会放宽云端安全组，并尽量关闭系统内防火墙（ufw/iptables），方便 SSH / 网页直连。
          公网环境下风险更高，不需要外网访问时可取消勾选。
        </p>
      </div>

      <details>
        <summary class="muted" style="cursor: pointer; font-size: 13px">高级：首次启动脚本（cloud-init）</summary>
        <div class="field" style="margin-top: 0.5rem">
          <label>Shell 脚本，实例首次启动时以 root 执行一次（日志在 /var/log/ocibot-user-script.log）</label>
          <textarea
            v-model="form.user_data"
            rows="5"
            spellcheck="false"
            placeholder="#!/bin/bash&#10;apt-get update -y&#10;# 安装你需要的环境…"
          ></textarea>
        </div>
      </details>

      <div class="stack" style="border-top: 1px solid var(--border); padding-top: 0.75rem">
        <div class="choice-group">
          <label class="choice muted">
            <input v-model="form.as_retry" type="checkbox" :disabled="form.auth_mode !== 'key'" />
            <span>容量不足时加入自动重试（仅密钥模式，合规限速）</span>
          </label>
          <label class="choice muted">
            <input v-model="form.retry_all_ads" type="checkbox" :disabled="!form.as_retry" />
            <span>重试时轮询区域全部可用域（{{ ads.length }} 个）</span>
          </label>
        </div>
        <div class="grid-2" v-if="form.as_retry">
          <div class="field">
            <label>重试间隔秒（≥60）</label>
            <input v-model.number="form.retry_interval_sec" type="number" min="60" />
          </div>
          <div class="field">
            <label>最大次数</label>
            <input v-model.number="form.retry_max_attempts" type="number" min="1" max="2000" />
          </div>
        </div>
        <div v-if="form.as_retry && isFlex" class="stack">
          <label class="muted" style="font-size: 13px">
            降级配置（可选）：主配置在全部 AD 都无容量后，按顺序尝试以下更小的配置
          </label>
          <div v-for="(fb, i) in fallbacks" :key="i" class="row">
            <input v-model.number="fb.ocpus" type="number" min="1" step="1" placeholder="OCPU" style="width: 110px" />
            <span class="muted">OCPU /</span>
            <input v-model.number="fb.memory_in_gbs" type="number" min="1" step="1" placeholder="内存 GB" style="width: 110px" />
            <span class="muted">GB</span>
            <button type="button" @click="fallbacks.splice(i, 1)">移除</button>
          </div>
          <button
            v-if="fallbacks.length < 5"
            type="button"
            style="align-self: flex-start"
            @click="fallbacks.push({ ocpus: 2, memory_in_gbs: 12 })"
          >
            + 添加降级配置
          </button>
        </div>
      </div>

      <p class="muted" style="font-size: 12px; margin: 0">
        ⚠ 若勾选了「允许外网直接访问」，外网更容易连上这台机器，请自行确认风险。Compartment / VCN / Subnet 使用账号默认网络。
      </p>

      <div class="row">
        <button
          class="primary"
          :disabled="submitting || loadingQuota || !tenantId || loadingMeta"
          @click="openConfirm"
        >
          {{ loadingQuota ? '校验额度中…' : '下一步：确认配置' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { type Tenant } from '@/api/client'
import { pickTenantId } from '@/stores/tenantLock'
import { pickAndReadTextFile } from '@/utils/file'
import { copyText } from '@/utils/toast'

type ShapeInfo = {
  shape: string
  label?: string
  free_tag?: string
  is_free_tier?: boolean
  is_flexible?: boolean
  ocpus?: number
  memory_in_gbs?: number
  processor_description?: string
}
type ImageInfo = {
  id: string
  label?: string
  display_name?: string
  architecture?: string
  operating_system_version?: string
}
type Preset = {
  id: string
  label: string
  hint?: string
  shape: string
  arch?: string
  ocpus?: number | null
  memory_in_gbs?: number | null
  boot_volume_size_in_gbs?: number
  boot_volume_vpus_per_gb?: number
}

const route = useRoute()
const router = useRouter()

const tenants = ref<Tenant[]>([])
/** Selected primary tenant (the 租户 dropdown). */
const primaryId = ref('')
/**
 * The tenant row every request actually uses. For the home region that is the
 * primary itself; for a 副区 it is the linked secondary-region row, which carries
 * its own OCI region — sessions are bound to one region, so switching region means
 * switching row.
 */
const tenantId = ref('')
const meta = ref<any>(null)
const loadingMeta = ref(false)
const submitting = ref(false)
const confirmOpen = ref(false)
const error = ref('')
const msg = ref('')
// Held until the user acknowledges it; the API returns the generated root
// password exactly once and never exposes it again.
const pendingPassword = ref('')

async function copyPassword() {
  await copyText(pendingPassword.value, 'root 密码已复制')
}

function dismissPassword() {
  pendingPassword.value = ''
  router.push({ path: '/', query: { tenant: tenantId.value } }).catch(() => {})
}
const presetHint = ref('')
const sshFile = ref('')
const quotaPreview = ref<any>(null)
const quotaVerdict = ref<any>(null)
/** Whether this tenant hard-enforces the free caps. Verdict wins; tenant row is the
 *  fallback before any pre-check has run. */
const freeOnly = computed(() => {
  if (quotaVerdict.value && typeof quotaVerdict.value.free_only_mode === 'boolean') {
    return quotaVerdict.value.free_only_mode
  }
  const t = tenants.value.find((x) => x.id === tenantId.value)
  return t ? t.free_only_mode !== false : true
})
const loadingQuota = ref(false)

/** Rows shown in the 租户 dropdown. A 副区 whose primary is missing (disabled, or
 *  restored from a backup without it) is listed on its own rather than hidden. */
const primaryTenants = computed(() =>
  tenants.value.filter(
    (t) => !t.parent_tenant_id || !tenants.value.some((p) => p.id === t.parent_tenant_id),
  ),
)
/** The selected primary plus its 副区 rows — the 区域 dropdown. */
const regionRows = computed(() => {
  const primary = tenants.value.find((t) => t.id === primaryId.value)
  if (!primary) return []
  return [primary, ...tenants.value.filter((t) => t.parent_tenant_id === primary.id)]
})
const selectedTenant = computed(() => tenants.value.find((t) => t.id === tenantId.value))
const isSecondaryRegion = computed(() => !!selectedTenant.value?.parent_tenant_id)

const QUOTA_ROWS: { key: string; label: string; unit: string }[] = [
  { key: 'a1_ocpu', label: 'A1.Flex OCPU', unit: '' },
  { key: 'a1_memory_gb', label: 'A1.Flex 内存', unit: ' GB' },
  { key: 'e2_micro_count', label: 'E2.1.Micro 实例', unit: ' 台' },
  { key: 'block_storage_gb', label: '块存储（含引导卷）', unit: ' GB' },
  // Only present on the full /free-quota read; the pre-submit check omits egress
  // because it cannot affect the verdict.
  { key: 'egress_gb', label: '出网流量（本月·估算）', unit: ' GB' },
]

function fmtNum(n: unknown): string {
  const v = Number(n)
  if (!Number.isFinite(v)) return '—'
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

function quotaStatusLabel(status: string): string {
  return (
    { ok: '正常', warn: '接近上限', critical: '即将用尽', over: '已超额' }[status] || status || '—'
  )
}

/** Per-resource used/limit/remaining rows for the usage panel.
 *  A row whose bucket the server did not return is dropped — rendering it as
 *  「已用 — / —」 just looks broken. */
const quotaRows = computed(() => {
  const buckets = quotaPreview.value?.buckets || {}
  return QUOTA_ROWS.filter((r) => buckets[r.key]).map((r) => {
    const b = buckets[r.key] || {}
    return {
      key: r.key,
      label: r.label,
      unit: r.unit,
      used: fmtNum(b.used),
      limit: fmtNum(b.limit),
      remaining: fmtNum(b.remaining),
      ratio: Number(b.ratio) || 0,
      status: b.status || 'ok',
    }
  })
})
const quotaLoadError = ref('')

const form = reactive({
  display_name: '',
  availability_domain: '',
  shape: '',
  image_id: '',
  auth_mode: 'key' as 'key' | 'password',
  ssh_public_key: '',
  root_password: '',
  ocpus: 4 as number | null,
  memory_in_gbs: 24 as number | null,
  boot_volume_size_in_gbs: '' as number | string,
  boot_volume_vpus_per_gb: 10,
  assign_public_ip: true,
  assign_ipv6_ip: false,
  open_guest_firewall: true,
  user_data: '',
  as_retry: false,
  retry_all_ads: false,
  retry_interval_sec: 180,
  retry_max_attempts: 200,
})

const osFamily = ref<'ubuntu' | 'oracle_linux' | 'custom'>('ubuntu')
const fallbacks = ref<{ ocpus: number; memory_in_gbs: number }[]>([])

const ads = computed(() => (meta.value?.ads as string[]) || [])
const images = computed(() => {
  const byOs = meta.value?.images_by_os as Record<string, ImageInfo[]> | undefined
  if (byOs && byOs[osFamily.value]?.length) return byOs[osFamily.value]
  if (osFamily.value === 'ubuntu') return (meta.value?.images as ImageInfo[]) || []
  return (byOs?.[osFamily.value] as ImageInfo[]) || []
})
const shapes = computed(() => (meta.value?.shapes as ShapeInfo[]) || [])
const presets = computed(() => (meta.value?.quick_presets as Preset[]) || [])
const vpuPresets = computed(
  () =>
    (meta.value?.boot_vpu_presets as { value: number; label: string }[]) || [
      { value: 10, label: '平衡 (10)' },
    ],
)

const selectedImage = computed(() => images.value.find((i) => i.id === form.image_id))
const isArmImage = computed(() => {
  const blob = `${selectedImage.value?.label || ''} ${selectedImage.value?.display_name || ''} ${selectedImage.value?.architecture || ''}`.toLowerCase()
  return blob.includes('arm') || blob.includes('aarch64')
})

const compatibleShapes = computed(() => {
  const list = shapes.value
  if (!list.length) return []
  const armImg = isArmImage.value
  const matched = list.filter((s) => {
    const blob = `${s.shape} ${s.processor_description || ''} ${s.label || ''}`.toLowerCase()
    const armShape = s.shape.includes('A1') || blob.includes('ampere') || blob.includes('arm')
    return armShape === armImg
  })
  return matched.length ? matched : list
})

const isFlex = computed(() => {
  const shape = (form.shape || '').toLowerCase()
  // Only *.Flex shapes allow custom OCPU/memory. Fixed free AMD E2.1.Micro does not.
  if (!shape) return false
  if (shape.includes('e2.1.micro') || shape.endsWith('.micro')) return false
  return shape.endsWith('.flex') || shape.includes('.flex.')
})

const shapeSpecHint = computed(() => {
  if (!form.shape) return ''
  if (isFlex.value) return 'Flex 规格：可自定义 OCPU / 内存'
  if (/e2\.1\.micro/i.test(form.shape)) {
    return 'VM.Standard.E2.1.Micro 为固定规格（1 OCPU / 1 GB），不可修改 OCPU 与内存'
  }
  return '此 Shape 为固定规格，OCPU / 内存由型号决定，不可修改'
})

watch(
  () => form.auth_mode,
  (mode) => {
    if (mode !== 'key') form.as_retry = false
  },
)

function padName() {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const h = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `instance-${m}${day}${h}${mi}`
}

async function loadTenants() {
  const { data } = await api.get<Tenant[]>('/tenants')
  tenants.value = data.filter((t) => t.enabled)
  // ?tenant= (or the locked tenant) may name a 副区 row; select its primary so
  // both dropdowns agree.
  const picked = pickTenantId(tenants.value, route.query.tenant)
  const wanted = tenants.value.find((t) => t.id === picked) || primaryTenants.value[0]
  if (!wanted) return
  const parentListed = tenants.value.some((p) => p.id === wanted.parent_tenant_id)
  primaryId.value = parentListed ? wanted.parent_tenant_id : wanted.id
  tenantId.value = wanted.id
}

let metaSeq = 0
async function loadMeta(force = false) {
  if (!tenantId.value) return
  error.value = ''
  loadingMeta.value = true
  const seq = ++metaSeq
  const requestedTenant = tenantId.value
  try {
    const { data } = await api.get(`/tenants/${requestedTenant}/launch-meta`, {
      params: { force: force === true },
    })
    // Drop stale responses if the user switched tenants mid-flight.
    if (seq !== metaSeq || tenantId.value !== requestedTenant) return
    meta.value = data
    if (!form.display_name) form.display_name = data?.defaults?.display_name || padName()
    if (data?.defaults?.retry_interval_sec) form.retry_interval_sec = data.defaults.retry_interval_sec
    if (data?.defaults?.retry_max_attempts) form.retry_max_attempts = data.defaults.retry_max_attempts
    // Always rebind AD to this tenant's catalog (keep prior only if still valid).
    const adsList: string[] = listAds(data)
    if (adsList.length) {
      if (!adsList.includes(form.availability_domain)) {
        form.availability_domain = adsList[0]
      }
    } else {
      form.availability_domain = ''
    }
    // Rebind the image against the list for the CURRENTLY SELECTED OS family, and
    // keep the prior choice when it is still valid — the same rule used for the AD
    // above. Seeding from data.images (which is Ubuntu-only) discarded a non-Ubuntu
    // 操作系统 selection: the 镜像 select then had no matching option and rendered
    // blank, onImageChange saw no image, and the shape fell back to the x86
    // E2.1.Micro — so the form could submit an ARM image on a fixed x86 shape.
    const imgList = images.value
    if (imgList.length) {
      if (!imgList.some((i: ImageInfo) => i.id === form.image_id)) {
        const preferArm = imgList.find((i: ImageInfo) =>
          /arm|aarch64/i.test(`${i.label} ${i.architecture}`),
        )
        form.image_id = (preferArm || imgList[0]).id
      }
    } else {
      form.image_id = ''
      form.shape = ''
    }
    onImageChange()
  } catch (e: any) {
    if (seq !== metaSeq || tenantId.value !== requestedTenant) return
    meta.value = null
    form.availability_domain = ''
    form.image_id = ''
    form.shape = ''
    error.value = e?.message || '加载创建元数据失败'
  } finally {
    if (seq === metaSeq) loadingMeta.value = false
  }
}

function listAds(data: any): string[] {
  const raw = data?.ads
  if (!Array.isArray(raw)) return []
  return raw.map((a: any) => String(a || '')).filter(Boolean)
}

function onOsFamilyChange() {
  const list = images.value
  if (!list.length) {
    form.image_id = ''
    error.value =
      osFamily.value === 'custom'
        ? '该租户还没有自定义镜像。制作镜像功能已关闭；可改用官方 Ubuntu / Oracle Linux。'
        : '该系统未找到可用镜像'
    return
  }
  error.value = ''
  const preferArm = list.find((i) => /arm|aarch64/i.test(`${i.label} ${i.architecture}`))
  form.image_id = (preferArm || list[0]).id
  onImageChange()
}

function onImageChange() {
  const list = compatibleShapes.value
  if (!list.length) {
    form.shape = ''
    return
  }
  const prefer = list.find((s) => s.shape.includes('A1.Flex')) || list[0]
  form.shape = prefer.shape
  onShapeChange()
}

function onShapeChange() {
  const s = shapes.value.find((x) => x.shape === form.shape)
  if (!s) return
  if (!isFlex.value) {
    // Fixed shapes (e.g. VM.Standard.E2.1.Micro): force catalog OCPU/memory.
    form.ocpus = s.ocpus != null ? Number(s.ocpus) : /e2\.1\.micro/i.test(form.shape) ? 1 : null
    form.memory_in_gbs =
      s.memory_in_gbs != null ? Number(s.memory_in_gbs) : /e2\.1\.micro/i.test(form.shape) ? 1 : null
  } else if (form.ocpus == null) {
    form.ocpus = 4
    form.memory_in_gbs = 24
  }
}

function applyPreset(p: Preset) {
  presetHint.value = p.hint || p.label
  // pick matching image arch
  const wantArm = (p.arch || 'arm') === 'arm'
  const img = images.value.find((i) => {
    const blob = `${i.label || ''} ${i.display_name || ''} ${i.architecture || ''}`.toLowerCase()
    const isArm = blob.includes('arm') || blob.includes('aarch64')
    return isArm === wantArm
  })
  if (img) form.image_id = img.id
  onImageChange()
  const shape = compatibleShapes.value.find((s) => s.shape === p.shape) || shapes.value.find((s) => s.shape === p.shape)
  if (shape) form.shape = shape.shape
  onShapeChange()
  if (p.ocpus != null) form.ocpus = Number(p.ocpus)
  if (p.memory_in_gbs != null) form.memory_in_gbs = Number(p.memory_in_gbs)
  if (p.boot_volume_size_in_gbs != null) form.boot_volume_size_in_gbs = p.boot_volume_size_in_gbs
  if (p.boot_volume_vpus_per_gb != null) form.boot_volume_vpus_per_gb = p.boot_volume_vpus_per_gb
}

function genPassword() {
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
  const lower = 'abcdefghijkmnopqrstuvwxyz'
  const digits = '23456789'
  const symbols = '!@#%^*-_=+'
  const all = upper + lower + digits + symbols
  // Unbiased index in [0, n) from a CSPRNG (rejection sampling) — never Math.random
  // for credential material.
  const rnd = (n: number) => {
    const limit = Math.floor(0x100000000 / n) * n
    const buf = new Uint32Array(1)
    let x = 0
    do {
      crypto.getRandomValues(buf)
      x = buf[0]
    } while (x >= limit)
    return x % n
  }
  const pick = (s: string) => s[rnd(s.length)]
  const chars = [pick(upper), pick(lower), pick(digits), pick(symbols)]
  for (let i = 0; i < 12; i++) chars.push(pick(all))
  // Fisher–Yates shuffle so the guaranteed char classes aren't always in front.
  for (let i = chars.length - 1; i > 0; i--) {
    const j = rnd(i + 1)
    ;[chars[i], chars[j]] = [chars[j], chars[i]]
  }
  form.root_password = chars.join('')
}

async function pickSshKey() {
  const text = await pickAndReadTextFile('.pub,.txt,text/plain')
  if (text == null) return
  if (/PRIVATE KEY/i.test(text)) {
    error.value = '请选择公钥文件（.pub），不要选私钥'
    return
  }
  form.ssh_public_key = text.trim().split(/\r?\n/).filter(Boolean)[0] || ''
  sshFile.value = '已从文件加载公钥'
}

const confirmRows = computed(() => {
  const tenant = tenants.value.find((t) => t.id === tenantId.value)
  const img = images.value.find((i) => i.id === form.image_id)
  const boot =
    form.boot_volume_size_in_gbs === '' || form.boot_volume_size_in_gbs == null
      ? '默认'
      : `${form.boot_volume_size_in_gbs} GB`
  const auth =
    form.auth_mode === 'key'
      ? `SSH 公钥（${(form.ssh_public_key || '').slice(0, 24)}…）`
      : form.root_password
        ? 'root 密码（已填写）'
        : 'root 密码（将自动生成）'
  return [
    ['租户', tenant ? `${tenant.name} · ${tenant.region}` : tenantId.value],
    [
      '区域',
      tenant
        ? `${tenant.region}${isSecondaryRegion.value ? '（副区 · 按量计费）' : '（主区）'}`
        : '—',
    ],
    ['显示名称', form.display_name || '—'],
    ['AD', form.availability_domain || '—'],
    ['镜像', img?.label || img?.display_name || form.image_id || '—'],
    ['Shape', form.shape || '—'],
    ['OCPU / 内存', isFlex.value ? `${form.ocpus} / ${form.memory_in_gbs} GB（可调）` : `${form.ocpus ?? '—'} / ${form.memory_in_gbs ?? '—'} GB（固定规格，不可改）`],
    ['Boot', `${boot} · ${form.boot_volume_vpus_per_gb} VPUs/GB`],
    ['登录', auth],
    ['公网 IPv4', form.assign_public_ip ? '是' : '否'],
    ['IPv6', form.assign_ipv6_ip ? '是' : '否'],
    ['允许外网直接访问', form.open_guest_firewall ? '是' : '否'],
    [
      '容量重试',
      form.as_retry
        ? `是 · 间隔 ${form.retry_interval_sec}s · 最多 ${form.retry_max_attempts} 次${
            form.retry_all_ads ? ' · 轮询全部 AD' : ''
          }${
            fallbacks.value.length
              ? ' · 降级 ' + fallbacks.value.map((f) => `${f.ocpus}C/${f.memory_in_gbs}G`).join('→')
              : ''
          }`
        : '否',
    ],
    ['启动脚本', form.user_data.trim() ? `已填写（${form.user_data.trim().length} 字符）` : '无'],
    ['网络', meta.value?.network_note || '默认公网 VCN/Subnet'],
  ] as [string, string][]
})

async function loadQuotaPreview() {
  if (!tenantId.value || loadingQuota.value) return
  loadingQuota.value = true
  quotaLoadError.value = ''
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/free-quota`)
    quotaPreview.value = data.data || null
  } catch (e: any) {
    quotaPreview.value = null
    quotaLoadError.value = e?.message || '无法读取免费额度（提交时仍会由服务端校验）'
  } finally {
    loadingQuota.value = false
  }
}

/**
 * Ask the SERVER to judge the current form against the free-tier caps.
 *
 * Deliberately not reimplemented client-side: the endpoint runs the same
 * check_launch_quota the launch path enforces with, so the pre-submit verdict
 * cannot drift from what the server will actually do.
 */
async function checkQuotaForForm(): Promise<boolean> {
  quotaVerdict.value = null
  if (!tenantId.value) return true
  loadingQuota.value = true
  try {
    const { data } = await api.post(`/tenants/${tenantId.value}/launch-quota-check`, {
      shape: form.shape,
      image_id: form.image_id,
      ocpus: isFlex.value ? form.ocpus : null,
      memory_in_gbs: isFlex.value ? form.memory_in_gbs : null,
      boot_volume_size_in_gbs: form.boot_volume_size_in_gbs || null,
      boot_volume_vpus_per_gb: form.boot_volume_vpus_per_gb,
    })
    quotaVerdict.value = data
    // Keep the usage panel in sync with the snapshot the verdict used.
    if (data?.buckets) {
      quotaPreview.value = {
        account_tier: data.account_tier,
        read_incomplete: data.read_incomplete,
        overall_status: data.overall_status,
        limits: data.limits,
        usage: data.usage,
        remaining: data.remaining,
        buckets: data.buckets,
        summary_lines: data.summary_lines,
        // Carried through so the panel shows the 副区 note instead of four
        // all-zero gauges that would read as free headroom.
        secondary_region: data.secondary_region,
        region: data.region,
        home_region: data.home_region,
      }
    }
    return !data?.blocked
  } catch (e: any) {
    // A failed pre-check must not block a launch the server might well accept —
    // the server re-validates on submit either way.
    quotaLoadError.value = e?.message || '无法预检免费额度（提交时仍会由服务端校验）'
    return true
  } finally {
    loadingQuota.value = false
  }
}

async function openConfirm() {
  error.value = ''
  msg.value = ''
  if (loadingQuota.value) return
  if (!tenantId.value) {
    error.value = '请选择租户'
    return
  }
  if (form.auth_mode === 'key' && !form.ssh_public_key.trim()) {
    error.value = '请填写或选择 SSH 公钥'
    return
  }
  if (!form.shape || !form.image_id || !form.availability_domain) {
    error.value = '请完整选择 AD / 镜像 / Shape'
    return
  }
  const allowed = await checkQuotaForForm()
  if (!allowed) {
    // Blocked: the reason is rendered from quotaVerdict above the button.
    error.value = '当前配置会超出 Always Free 免费额度，已阻止提交（详见下方说明）'
    return
  }
  confirmOpen.value = true
}

async function doLaunch() {
  error.value = ''
  msg.value = ''
  submitting.value = true
  try {
    const boot =
      form.boot_volume_size_in_gbs === '' || form.boot_volume_size_in_gbs == null
        ? null
        : Number(form.boot_volume_size_in_gbs)
    // Launch can take a while (network/NSG prep). Keep button disabled until response.
    const { data } = await api.post(
      `/tenants/${tenantId.value}/launch`,
      {
        display_name: form.display_name,
        availability_domain: form.availability_domain,
        shape: form.shape,
        image_id: form.image_id,
        auth_mode: form.auth_mode,
        ssh_public_key: form.ssh_public_key,
        root_password: form.root_password,
        ocpus: isFlex.value ? form.ocpus : null,
        memory_in_gbs: isFlex.value ? form.memory_in_gbs : null,
        boot_volume_size_in_gbs: boot,
        boot_volume_vpus_per_gb: form.boot_volume_vpus_per_gb,
        assign_public_ip: form.assign_public_ip,
        assign_ipv6_ip: form.assign_ipv6_ip,
        open_guest_firewall: form.open_guest_firewall,
        user_data: form.user_data.trim(),
        as_retry: form.as_retry,
        retry_all_ads: form.retry_all_ads,
        retry_interval_sec: form.retry_interval_sec,
        retry_max_attempts: form.retry_max_attempts,
        fallback_configs:
          form.as_retry && isFlex.value
            ? fallbacks.value.filter((f) => f.ocpus > 0 && f.memory_in_gbs > 0)
            : [],
      },
      { timeout: 180_000 },
    )
    // Always release UI first so success/error is visible even if navigation fails.
    submitting.value = false
    confirmOpen.value = false
    if (data.ok) {
      msg.value = data.message || '创建已提交'
      if (data.instance_id) {
        msg.value += ` · 实例 ${String(data.instance_id).slice(-12)}`
      }
      if (data.capacity_job_id) msg.value += ` · 任务 ${data.capacity_job_id.slice(0, 8)}…`
      // A queued capacity-retry job (no instance yet) → task centre; else the list.
      const queuedRetry = !!data.capacity_job_id && !data.instance_id
      if (data.root_password) {
        // The server returns this exactly once (it is only hashed into cloud-init,
        // never stored in plaintext). Auto-navigating unmounted this view 800ms
        // later and destroyed the only copy, so hold it here and let the user
        // leave once they have saved it.
        pendingPassword.value = data.root_password
        return
      }
      window.setTimeout(() => {
        if (queuedRetry) {
          router.push({ path: '/jobs' }).catch(() => {})
        } else {
          router.push({ path: '/', query: { tenant: tenantId.value } }).catch(() => {})
        }
      }, 800)
    } else if (data.capacity_job_id) {
      msg.value = data.message || '已加入容量重试'
      error.value = ''
      window.setTimeout(() => {
        router.push({ path: '/jobs' }).catch(() => {})
      }, 800)
    } else {
      error.value = data.message || '创建失败'
    }
  } catch (e: any) {
    // Axios timeout / network: request may still have succeeded server-side.
    const status = e?.response?.status
    if (!status && /timeout/i.test(String(e?.message || ''))) {
      error.value =
        '等待响应超时。创建请求可能已在服务端提交成功，请到「实例」或「任务中心」查看，勿重复连点提交。'
    } else {
      error.value = e?.message || '创建失败'
    }
    submitting.value = false
  }
}

function onTenantPicked() {
  // Switching tenant resets the region to that tenant's own (home) region.
  tenantId.value = primaryId.value
  onRegionPicked()
}

function onRegionPicked() {
  // Clear previous tenant's meta so we never create with stale AD/image/shape.
  meta.value = null
  form.availability_domain = ''
  form.image_id = ''
  form.shape = ''
  presetHint.value = ''
  quotaPreview.value = null
  quotaVerdict.value = null
  quotaLoadError.value = ''
  if (isSecondaryRegion.value) {
    // A 副区 has no Always Free allowance, so the (expensive) usage enumeration
    // would only produce numbers that must not be read as free headroom.
    quotaPreview.value = {
      secondary_region: true,
      region: selectedTenant.value?.region || '',
      home_region: regionRows.value[0]?.region || '',
      buckets: {},
    }
    return
  }
  // Usage is one cheap-ish read and it is the whole point of the panel, so fetch
  // it on selection; the heavier image/shape/network metadata still waits for the
  // explicit 「加载配置」 click.
  void loadQuotaPreview()
}

onMounted(async () => {
  try {
    await loadTenants()
    form.display_name = padName()
    // No automatic launch-meta fetch on enter.
  } catch (e: any) {
    error.value = e?.message || '初始化失败'
  }
})
</script>

<style scoped>
.confirm-k {
  width: 28%;
  min-width: 5.5rem;
  white-space: nowrap;
}
.warn-text {
  color: var(--warn) !important;
}
.quota-panel {
  padding: 0.7rem 0.8rem;
}
.warn-panel {
  padding: 0.6rem 0.8rem;
  border-color: var(--warn);
}
.quota-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.6rem 1rem;
  margin-top: 0.5rem;
}
.quota-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.quota-bar {
  height: 6px;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  overflow: hidden;
  margin: 0.25rem 0;
}
.quota-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.2s ease;
}
.quota-fill.warn {
  background: var(--warn);
}
.quota-fill.critical,
.quota-fill.over {
  background: var(--danger, #e5484d);
}
.quota-nums {
  font-size: 12px;
}
.password-reveal {
  border: 1px solid var(--warn);
}
.password-value {
  font-size: 15px;
  font-weight: 600;
  padding: 0.35rem 0.6rem;
  border-radius: 6px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  user-select: all;
  word-break: break-all;
}
@media (max-width: 600px) {
  .confirm-k {
    width: 34%;
    min-width: 4.5rem;
    font-size: 12px;
  }
}
</style>
