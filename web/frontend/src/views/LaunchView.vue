<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>创建实例</h2>
        <p class="muted" style="margin: 0.2rem 0 0">
          自动公网网络 · 免费套餐快捷配置
        </p>
      </div>
      <div class="page-tools">
        <router-link to="/"><button type="button">返回实例列表</button></router-link>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

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
      <p class="muted" style="margin: 0; font-size: 12px; color: #fbbf24">
        ⚠ 将开放 Guest 防火墙与实例 NSG 相关规则；容量重试会持续调用 LaunchInstance，请确认间隔与次数。
        服务端会按 Always Free 额度拦截超额创建（免费/未知账号硬拦；已付费账号仅警告）。
      </p>
      <div v-if="quotaPreview" class="card" style="padding: 0.65rem; font-size: 12px">
        <div class="row" style="justify-content: space-between">
          <strong>免费额度预览</strong>
          <span class="badge" :class="quotaPreview.overall_status === 'ok' ? 'running' : 'warn'">
            {{ quotaPreview.overall_status || '—' }}
          </span>
        </div>
        <div class="muted" style="margin-top: 0.25rem">
          {{ (quotaPreview.summary_lines || []).slice(0, 4).join(' · ') || '—' }}
        </div>
        <p v-if="quotaLoadError" class="muted" style="margin: 0.35rem 0 0; color: #fbbf24">
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
          <select v-model="tenantId" @change="() => loadMeta()">
            <option disabled value="">选择租户</option>
            <option v-for="t in tenants" :key="t.id" :value="t.id">{{ t.name }} · {{ t.region }}</option>
          </select>
        </div>
        <div class="field">
          <label>显示名称</label>
          <input v-model="form.display_name" />
        </div>
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
        <div class="row">
          <label class="row" style="gap: 0.35rem"><input v-model="form.auth_mode" type="radio" value="key" style="width:auto" /> root + SSH 公钥</label>
          <label class="row" style="gap: 0.35rem"><input v-model="form.auth_mode" type="radio" value="password" style="width:auto" /> root + 密码</label>
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

      <div class="row">
        <label class="row muted" style="font-size: 13px"><input v-model="form.assign_public_ip" type="checkbox" style="width:auto" /> 分配公网 IPv4</label>
        <label class="row muted" style="font-size: 13px"><input v-model="form.assign_ipv6_ip" type="checkbox" style="width:auto" /> 分配 IPv6</label>
        <label class="row muted" style="font-size: 13px"><input v-model="form.open_guest_firewall" type="checkbox" style="width:auto" /> 开放 Guest 防火墙</label>
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
        <label class="row muted" style="font-size: 13px">
          <input v-model="form.as_retry" type="checkbox" style="width:auto" :disabled="form.auth_mode !== 'key'" />
          容量不足时加入自动重试（仅密钥模式，合规限速）
        </label>
        <label class="row muted" style="font-size: 13px">
          <input v-model="form.retry_all_ads" type="checkbox" style="width:auto" :disabled="!form.as_retry" />
          重试时轮询区域全部可用域（{{ ads.length }} 个）
        </label>
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
        ⚠ 将开放 Guest 防火墙与实例 NSG 相关规则，公网暴露风险请自行确认。Compartment / VCN / Subnet 使用账号默认网络（与桌面版一致）。
      </p>

      <div class="row">
        <button class="primary" :disabled="submitting || !tenantId || loadingMeta" @click="openConfirm">
          下一步：确认配置
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { type Tenant } from '@/api/client'
import { pickAndReadTextFile } from '@/utils/file'

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
const tenantId = ref('')
const meta = ref<any>(null)
const loadingMeta = ref(false)
const submitting = ref(false)
const confirmOpen = ref(false)
const error = ref('')
const msg = ref('')
const presetHint = ref('')
const sshFile = ref('')
const quotaPreview = ref<any>(null)
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
  const q = String(route.query.tenant || '')
  if (q && tenants.value.some((t) => t.id === q)) tenantId.value = q
  else if (tenants.value[0]) tenantId.value = tenants.value[0].id
}

async function loadMeta(force = false) {
  if (!tenantId.value) return
  error.value = ''
  loadingMeta.value = true
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/launch-meta`, {
      params: { force: force === true },
    })
    meta.value = data
    if (!form.display_name) form.display_name = data?.defaults?.display_name || padName()
    if (data?.defaults?.retry_interval_sec) form.retry_interval_sec = data.defaults.retry_interval_sec
    if (data?.defaults?.retry_max_attempts) form.retry_max_attempts = data.defaults.retry_max_attempts
    if (data?.ads?.length && !form.availability_domain) form.availability_domain = data.ads[0]
    // default image + shape
    if (data?.images?.length) {
      const preferArm = data.images.find((i: ImageInfo) => /arm|aarch64/i.test(`${i.label} ${i.architecture}`))
      form.image_id = (preferArm || data.images[0]).id
    }
    onImageChange()
  } catch (e: any) {
    meta.value = null
    error.value = e?.message || '加载创建元数据失败'
  } finally {
    loadingMeta.value = false
  }
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
    ['显示名称', form.display_name || '—'],
    ['AD', form.availability_domain || '—'],
    ['镜像', img?.label || img?.display_name || form.image_id || '—'],
    ['Shape', form.shape || '—'],
    ['OCPU / 内存', isFlex.value ? `${form.ocpus} / ${form.memory_in_gbs} GB（可调）` : `${form.ocpus ?? '—'} / ${form.memory_in_gbs ?? '—'} GB（固定规格，不可改）`],
    ['Boot', `${boot} · ${form.boot_volume_vpus_per_gb} VPUs/GB`],
    ['登录', auth],
    ['公网 IPv4', form.assign_public_ip ? '是' : '否'],
    ['IPv6', form.assign_ipv6_ip ? '是' : '否'],
    ['开放 Guest 防火墙', form.open_guest_firewall ? '是' : '否'],
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
  quotaPreview.value = null
  quotaLoadError.value = ''
  if (!tenantId.value) return
  try {
    const { data } = await api.get(`/tenants/${tenantId.value}/free-quota`)
    quotaPreview.value = data.data || null
  } catch (e: any) {
    quotaLoadError.value = e?.message || '无法读取免费额度（提交时仍会由服务端校验）'
  }
}

async function openConfirm() {
  error.value = ''
  msg.value = ''
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
  await loadQuotaPreview()
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
      if (data.root_password) {
        msg.value += ` · root 密码：${data.root_password}（请立即保存，仅显示一次）`
      }
      if (data.instance_id) {
        msg.value += ` · 实例 ${String(data.instance_id).slice(-12)}`
      }
      if (data.capacity_job_id) msg.value += ` · 任务 ${data.capacity_job_id.slice(0, 8)}…`
      // A queued capacity-retry job (no instance yet) → task centre; else the list.
      const queuedRetry = !!data.capacity_job_id && !data.instance_id
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

onMounted(async () => {
  try {
    await loadTenants()
    form.display_name = padName()
    if (tenantId.value) await loadMeta()
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
@media (max-width: 600px) {
  .confirm-k {
    width: 34%;
    min-width: 4.5rem;
    font-size: 12px;
  }
}
</style>
