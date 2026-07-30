<template>
  <div class="layout" :class="{ 'nav-open': navOpen }">
    <div v-if="navOpen" class="nav-backdrop" @click="navOpen = false" />

    <header class="mobile-topbar">
      <button type="button" class="icon-btn" aria-label="打开菜单" @click="navOpen = true">☰</button>
      <div class="mobile-brand">
        <span class="title">OCIBot</span>
        <span class="muted small">{{ pageTitle }}</span>
      </div>
      <button
        type="button"
        class="icon-btn"
        :title="theme === 'light' ? '暗色' : '亮色'"
        @click="toggleTheme"
      >
        {{ theme === 'light' ? '🌙' : '☀️' }}
      </button>
    </header>

    <aside class="sidebar">
      <div class="brand">
        <img class="logo-img" src="/logo.svg" width="36" height="36" alt="OCIBot" />
        <div class="brand-text">
          <div class="title">OCIBot</div>
          <div class="muted small truncate">
            {{ auth.username }}<span v-if="auth.isAdmin"> · 管理员</span>
          </div>
        </div>
        <button
          type="button"
          class="icon-btn sidebar-close"
          aria-label="关闭菜单"
          @click="navOpen = false"
        >
          ✕
        </button>
      </div>

      <!-- Always visible: a default tenant that silently applies on four pages
           would otherwise be an unexplained behaviour with no obvious way out. -->
      <div v-if="hasLockedTenant" class="locked-tenant" :title="`各页面默认使用「${lockedTenantName}」`">
        <span class="lock-ico" aria-hidden="true">🔒</span>
        <span class="truncate">{{ lockedTenantName }}</span>
        <button type="button" class="lock-x" title="取消锁定" @click="unlockTenant()">✕</button>
      </div>

      <nav class="nav">
        <div class="nav-section">工作台</div>
        <router-link
          v-for="item in primaryNav"
          :key="item.to"
          :to="item.to"
          :class="{ 'nav-active': isNavActive(item) }"
          active-class=""
          exact-active-class=""
          @click="navOpen = false"
        >
          <span class="nav-ico" aria-hidden="true"><Icon :name="item.icon" :size="18" /></span>
          <span>{{ item.label }}</span>
        </router-link>

        <div class="nav-section">资源</div>
        <router-link
          v-for="item in resourceNav"
          :key="item.to"
          :to="item.to"
          :class="{ 'nav-active': isNavActive(item) }"
          active-class=""
          exact-active-class=""
          @click="navOpen = false"
        >
          <span class="nav-ico" aria-hidden="true"><Icon :name="item.icon" :size="18" /></span>
          <span>{{ item.label }}</span>
        </router-link>

        <div class="nav-section">系统</div>
        <router-link
          v-for="item in systemNav"
          :key="item.to"
          :to="item.to"
          :class="{ 'nav-active': isNavActive(item) }"
          active-class=""
          exact-active-class=""
          @click="navOpen = false"
        >
          <span class="nav-ico" aria-hidden="true"><Icon :name="item.icon" :size="18" /></span>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>

      <div class="sidebar-foot">
        <div v-if="buildLabel" class="muted small build-label" :title="buildFull">
          v{{ appVersion }} · {{ buildLabel }}
        </div>
        <button type="button" class="theme-btn-desktop ghost-btn" @click="toggleTheme">
          {{ theme === 'light' ? '切换暗色' : '切换亮色' }}
        </button>
        <button type="button" class="ghost-btn" @click="onLogout">退出登录</button>
      </div>
    </aside>

    <main class="main stack">
      <!-- Distinct from "worker offline": the worker IS running, it is just not
           allowed to call Oracle. Without saying so, a capacity-retry job that
           never fires looks like a bug. -->
      <div v-if="workerChecked && workerAlive && !backgroundOci" class="worker-banner bg-off">
        后台 OCI 请求已关闭（<code>OCIBOT_WORKER_BACKGROUND_OCI=0</code>）。
        抢机与定时开关机<strong>不会执行</strong>，面板只在你操作时请求 Oracle。
      </div>
      <div v-if="workerChecked && !workerAlive" class="error-box worker-banner">
        后台 Worker 离线（{{ heartbeatText }}）。容量重试 / 定时任务不会执行。请运行
        <code>python -m web.backend.worker</code>
        或检查容器状态。
      </div>
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api/client'
import Icon from '@/components/Icon.vue'
import { useAuthStore } from '@/stores/auth'
import { hasLockedTenant, lockedTenantName, unlockTenant } from '@/stores/tenantLock'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const navOpen = ref(false)

type NavItem = {
  to: string
  label: string
  icon: string
  match?: 'exact' | 'prefix' | 'instances'
}

const primaryNav: NavItem[] = [
  { to: '/', label: '实例', icon: 'instances', match: 'instances' },
  { to: '/launch', label: '创建实例', icon: 'launch', match: 'exact' },
  { to: '/jobs', label: '任务中心', icon: 'jobs', match: 'exact' },
]

const resourceNav: NavItem[] = [
  { to: '/storage', label: '存储', icon: 'storage', match: 'prefix' },
  { to: '/tenants', label: '租户', icon: 'tenants', match: 'exact' },
  { to: '/account', label: '账号用量', icon: 'account', match: 'exact' },
  { to: '/backup', label: '备份恢复', icon: 'backup', match: 'exact' },
]

const systemNav = computed<NavItem[]>(() => {
  const items: NavItem[] = [
    { to: '/audit', label: '审计日志', icon: 'audit', match: 'exact' },
    { to: '/settings', label: '设置', icon: 'settings', match: 'exact' },
  ]
  if (auth.isAdmin) {
    items.push({ to: '/admin', label: '用户管理 / 更新', icon: 'admin', match: 'exact' })
  }
  return items
})

const allNav = computed(() => [...primaryNav, ...resourceNav, ...systemNav.value])

const pageTitle = computed(() => {
  const hit = allNav.value.find((i) => isNavActive(i))
  return hit?.label || '工作台'
})

function isNavActive(item: NavItem): boolean {
  const path = route.path || '/'
  const mode = item.match || 'exact'
  if (mode === 'instances') return path === '/' || path.startsWith('/instances/')
  if (mode === 'prefix') return path === item.to || path.startsWith(item.to + '/')
  return path === item.to
}

const workerAlive = ref(true)
const backgroundOci = ref(true)
const workerChecked = ref(false)
const heartbeatText = ref('从未收到心跳')
const buildLabel = ref('')
const buildFull = ref('')
const appVersion = ref('')
let timer: number | undefined

// Default light (ByteDance console style); respect saved preference.
const theme = ref(localStorage.getItem('ocibot_theme') || 'light')

function applyTheme() {
  document.documentElement.setAttribute('data-theme', theme.value)
}

function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  localStorage.setItem('ocibot_theme', theme.value)
  applyTheme()
}

async function checkWorker() {
  try {
    const { data } = await api.get('/system/status')
    workerAlive.value = !!data.worker_alive
    // Absent on an older backend -> assume on, matching the shipped default.
    backgroundOci.value = data.background_oci !== false
    if (data.heartbeat_age_sec == null) {
      heartbeatText.value = '从未收到心跳'
    } else {
      heartbeatText.value = `上次心跳 ${Math.round(data.heartbeat_age_sec)} 秒前`
    }
    if (data.app_version || data.git_sha) {
      const sha = String(data.git_sha || '')
      appVersion.value = String(data.app_version || '')
      buildLabel.value = sha && sha !== 'unknown' ? sha.slice(0, 7) : data.app_version || ''
      buildFull.value = `app ${data.app_version || '—'} · git ${sha || 'unknown'}`
    }
    workerChecked.value = true
  } catch {
    /* ignore */
  }
}

async function onLogout() {
  navOpen.value = false
  await auth.logout()
  router.push({ name: 'login' })
}

watch(
  () => route.fullPath,
  () => {
    navOpen.value = false
  },
)

function onResize() {
  if (window.innerWidth > 900) navOpen.value = false
}

/**
 * Warm the other routes' chunks while the browser is idle.
 *
 * Views are lazy-loaded, so the FIRST click on each nav item pays for a download
 * before anything renders — on a high-latency link that reads as "the panel is
 * slow" even though the server answered in milliseconds. Fetching them ahead of
 * time turns that into an instant switch. Runs on idle so it never competes with
 * the page the user is actually looking at, and the files are immutably cached,
 * so this costs one download per deploy rather than per visit.
 */
function prefetchRoutes() {
  // InstancesView and this layout ship in the entry bundle (see router), so the
  // list here is only the routes that are still split out.
  const load = [
    () => import('@/views/LaunchView.vue'),
    () => import('@/views/StorageView.vue'),
    () => import('@/views/AccountView.vue'),
    () => import('@/views/JobsView.vue'),
    () => import('@/views/TenantsView.vue'),
    // Heaviest chunk (bundles the terminal), and the one most annoying to wait
    // for, so it is warmed too — just last.
    () => import('@/views/InstanceDetailView.vue'),
  ]
  let i = 0
  const step = () => {
    if (i >= load.length) return
    // A failed prefetch is not an error the user should ever see; the real
    // navigation will retry it.
    load[i++]().catch(() => {}).finally(() => schedule())
  }
  const schedule = () => {
    const ric = (window as any).requestIdleCallback
    if (typeof ric === 'function') ric(step, { timeout: 3000 })
    else window.setTimeout(step, 300)
  }
  schedule()
}

onMounted(() => {
  applyTheme()
  checkWorker()
  timer = window.setInterval(checkWorker, 30_000)
  window.addEventListener('resize', onResize)
  prefetchRoutes()
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;
  min-height: 100vh;
  min-height: 100dvh;
  background: transparent;
}

.mobile-topbar,
.nav-backdrop {
  display: none;
}

.sidebar {
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-right: 1px solid var(--glass-border);
  position: sticky;
  top: 0;
  height: 100vh;
  height: 100dvh;
  max-height: 100dvh;
  overflow: hidden;
  z-index: 20;
  backdrop-filter: blur(22px) saturate(1.4);
  -webkit-backdrop-filter: blur(22px) saturate(1.4);
  box-shadow: var(--glass-highlight), 4px 0 24px rgba(15, 23, 42, 0.04);
  color: var(--text);
}

:global(html[data-theme='dark']) .sidebar {
  box-shadow: var(--glass-highlight), 4px 0 24px rgba(0, 0, 0, 0.35);
}

.brand {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  padding: 1rem 1rem 0.85rem;
  border-bottom: 1px solid var(--border);
}

.worker-banner.bg-off {
  margin: 0 0.75rem 0.5rem;
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--warn);
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.worker-banner.bg-off code {
  font-size: 11px;
  word-break: break-all;
}

.locked-tenant {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0 0.75rem 0.5rem;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  font-size: 12px;
}
.lock-ico {
  flex: 0 0 auto;
}
.lock-x {
  margin-left: auto;
  flex: 0 0 auto;
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 0 0.2rem;
  line-height: 1;
}
.lock-x:hover {
  color: var(--text);
}

.brand-text {
  min-width: 0;
  flex: 1;
}

.logo-img {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  flex-shrink: 0;
  display: block;
  box-shadow: 0 4px 12px rgba(51, 112, 255, 0.35);
  background: transparent;
}

.logo {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 16px;
  color: #fff;
  background: linear-gradient(135deg, #3370ff 0%, #6b4eff 100%);
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(51, 112, 255, 0.35);
}

.title {
  font-weight: 650;
  font-size: 15px;
  letter-spacing: -0.02em;
  color: var(--text);
}

.small {
  font-size: 12px;
}

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  flex: 1;
  min-height: 0;
  padding: 0.65rem 0.65rem 1rem;
}

.nav-section {
  margin: 0.75rem 0.55rem 0.35rem;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted);
}

.nav-section:first-child {
  margin-top: 0.25rem;
}

.nav a {
  color: var(--text-secondary);
  padding: 0.55rem 0.7rem;
  border-radius: 12px;
  border: 1px solid transparent;
  min-height: 40px;
  display: flex;
  align-items: center;
  gap: 0.55rem;
  font-weight: 500;
  transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease,
    box-shadow 0.12s ease;
}

.nav a:hover {
  background: rgba(255, 255, 255, 0.45);
  color: var(--text);
}

:global(html[data-theme='dark']) .nav a:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--text);
}

.nav a.nav-active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
  border-color: rgba(51, 112, 255, 0.12);
  box-shadow: var(--glass-highlight);
}

:global(html[data-theme='dark']) .nav a.nav-active {
  border-color: rgba(91, 145, 255, 0.28);
  color: #9ec0ff;
}

.nav-ico {
  width: 1.25rem;
  height: 1.25rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  opacity: 0.9;
  flex-shrink: 0;
  color: inherit;
}

.sidebar-foot {
  margin-top: auto;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0.75rem 0.85rem 1rem;
  border-top: 1px solid var(--border);
}

.build-label {
  font-size: 11px;
  word-break: break-all;
  padding: 0 0.15rem 0.15rem;
}

.ghost-btn {
  box-shadow: none;
  background: transparent;
  border-color: transparent;
  justify-content: flex-start;
  text-align: left;
  color: var(--text-secondary);
}

.ghost-btn:hover:not(:disabled) {
  background: var(--panel-2);
  color: var(--text);
  border-color: transparent;
}

.main {
  min-width: 0;
  max-width: 100%;
  padding: 1.15rem 1.35rem 1.5rem;
  padding-bottom: max(1.5rem, env(safe-area-inset-bottom));
}

.sidebar-close {
  display: none;
}

.icon-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  display: inline-grid;
  place-items: center;
  font-size: 1.1rem;
  flex-shrink: 0;
  border-radius: 10px;
  box-shadow: none;
  background: transparent;
  border-color: transparent;
}

.icon-btn:hover:not(:disabled) {
  background: var(--panel-2);
  border-color: transparent;
}

.worker-banner code {
  background: var(--panel);
  padding: 0 0.35rem;
  border-radius: 4px;
  word-break: break-all;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .mobile-topbar {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    position: sticky;
    top: 0;
    z-index: 40;
    background: color-mix(in srgb, var(--panel) 78%, transparent);
    border-bottom: 1px solid var(--glass-border);
    padding: 0.4rem 0.55rem;
    padding-top: max(0.4rem, env(safe-area-inset-top));
    backdrop-filter: blur(18px) saturate(1.35);
  }

  .mobile-brand {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    line-height: 1.25;
  }

  .mobile-brand .title {
    font-size: 15px;
  }

  .nav-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(29, 33, 41, 0.45);
    z-index: 50;
  }

  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: min(86vw, 300px);
    max-width: 300px;
    height: 100dvh;
    max-height: 100dvh;
    z-index: 60;
    transform: translateX(-105%);
    transition: transform 0.2s ease;
    border-right: 1px solid var(--border);
    box-shadow: var(--shadow-md);
  }

  .layout.nav-open .sidebar {
    transform: translateX(0);
  }

  .sidebar-close {
    display: inline-grid;
  }

  .theme-btn-desktop {
    display: none;
  }

  .main {
    padding: 0.85rem 0.75rem 1.25rem;
  }
}
</style>
