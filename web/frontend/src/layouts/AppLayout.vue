<template>
  <div class="layout" :class="{ 'nav-open': navOpen }">
    <div v-if="navOpen" class="nav-backdrop" @click="navOpen = false" />

    <header class="mobile-topbar">
      <button type="button" class="icon-btn" aria-label="打开菜单" @click="navOpen = true">☰</button>
      <div class="mobile-brand">
        <span class="title">OCIBot</span>
        <span class="muted small">{{ pageTitle }}</span>
      </div>
      <button type="button" class="icon-btn" :title="theme === 'light' ? '暗色' : '亮色'" @click="toggleTheme">
        {{ theme === 'light' ? '🌙' : '☀️' }}
      </button>
    </header>

    <aside class="sidebar card">
      <div class="brand">
        <div class="logo">OCI</div>
        <div class="brand-text">
          <div class="title">OCIBot Web</div>
          <div class="muted small">
            {{ auth.username }}<span v-if="auth.isAdmin"> · 管理员</span>
          </div>
        </div>
        <button type="button" class="icon-btn sidebar-close" aria-label="关闭菜单" @click="navOpen = false">
          ✕
        </button>
      </div>
      <nav class="nav">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="{ 'nav-active': isNavActive(item) }"
          active-class=""
          exact-active-class=""
          @click="navOpen = false"
        >
          {{ item.label }}
        </router-link>
      </nav>
      <div class="sidebar-foot stack">
        <div v-if="buildLabel" class="muted small build-label" :title="buildFull">
          构建 {{ buildLabel }}
        </div>
        <button type="button" class="theme-btn-desktop" @click="toggleTheme">
          {{ theme === 'light' ? '🌙 暗色模式' : '☀️ 亮色模式' }}
        </button>
        <button type="button" @click="onLogout">退出登录</button>
      </div>
    </aside>

    <main class="main stack">
      <div v-if="workerChecked && !workerAlive" class="error-box worker-banner">
        ⚠️ 后台 Worker 离线（{{ heartbeatText }}）——容量重试、定时开关机、通知都不会执行。
        请在服务器上运行 <code>python -m web.backend.worker</code>，或检查 worker 容器状态。
      </div>
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const navOpen = ref(false)

type NavItem = { to: string; label: string; match?: 'exact' | 'prefix' | 'instances' }

const navItems = computed<NavItem[]>(() => {
  const items: NavItem[] = [
    { to: '/', label: '实例', match: 'instances' },
    { to: '/launch', label: '创建实例', match: 'exact' },
    { to: '/storage', label: '存储', match: 'prefix' },
    { to: '/tenants', label: '租户', match: 'exact' },
    { to: '/jobs', label: '任务中心', match: 'exact' },
    { to: '/account', label: '账号用量', match: 'exact' },
    { to: '/backup', label: '备份恢复', match: 'exact' },
    { to: '/audit', label: '审计日志', match: 'exact' },
    { to: '/settings', label: '设置', match: 'exact' },
  ]
  if (auth.isAdmin) items.push({ to: '/admin', label: '用户管理 / 更新', match: 'exact' })
  return items
})

const pageTitle = computed(() => {
  const hit = navItems.value.find((i) => isNavActive(i))
  return hit?.label || '面板'
})

function isNavActive(item: NavItem): boolean {
  const path = route.path || '/'
  const mode = item.match || 'exact'
  if (mode === 'instances') {
    return path === '/' || path.startsWith('/instances/')
  }
  if (mode === 'prefix') {
    return path === item.to || path.startsWith(item.to + '/')
  }
  return path === item.to
}

const workerAlive = ref(true)
const workerChecked = ref(false)
const heartbeatText = ref('从未收到心跳')
const buildLabel = ref('')
const buildFull = ref('')
let timer: number | undefined

const theme = ref(localStorage.getItem('ocibot_theme') || 'dark')

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
    if (data.heartbeat_age_sec == null) {
      heartbeatText.value = '从未收到心跳'
    } else {
      heartbeatText.value = `上次心跳 ${Math.round(data.heartbeat_age_sec)} 秒前`
    }
    if (data.app_version || data.git_sha) {
      const sha = String(data.git_sha || '')
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

// Close drawer on route change / desktop resize
watch(
  () => route.fullPath,
  () => {
    navOpen.value = false
  },
)

function onResize() {
  if (window.innerWidth > 900) navOpen.value = false
}

onMounted(() => {
  applyTheme()
  checkWorker()
  timer = window.setInterval(checkWorker, 30_000)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 240px 1fr;
  min-height: 100vh;
  min-height: 100dvh;
  gap: 1rem;
  padding: 1rem;
  padding-bottom: max(1rem, env(safe-area-inset-bottom));
}

.mobile-topbar {
  display: none;
}

.nav-backdrop {
  display: none;
}

.sidebar {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  position: sticky;
  top: 1rem;
  height: calc(100vh - 2rem);
  height: calc(100dvh - 2rem);
  max-height: calc(100dvh - 2rem);
  overflow: hidden;
}

.brand {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}

.brand-text {
  min-width: 0;
  flex: 1;
}

.logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #3b82f6, #8b5cf6);
  flex-shrink: 0;
}

.title {
  font-weight: 700;
}

.small {
  font-size: 12px;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  flex: 1;
  min-height: 0;
}

.nav a {
  color: var(--text);
  padding: 0.65rem 0.75rem;
  border-radius: 8px;
  border: 1px solid transparent;
  min-height: 44px;
  display: flex;
  align-items: center;
}

.nav a.nav-active {
  background: #1d4ed855;
  border-color: #3b82f6;
  color: #fff;
  font-weight: 600;
}

html[data-theme='light'] .nav a.nav-active {
  background: #dbeafe;
  border-color: #3b82f6;
  color: #1e3a8a;
}

.sidebar-foot {
  margin-top: auto;
  flex-shrink: 0;
}

.build-label {
  font-size: 11px;
  word-break: break-all;
}

.main {
  min-width: 0;
  max-width: 100%;
}

.sidebar-close {
  display: none;
}

.icon-btn {
  width: 42px;
  height: 42px;
  padding: 0;
  display: inline-grid;
  place-items: center;
  font-size: 1.15rem;
  flex-shrink: 0;
  border-radius: 10px;
}

.worker-banner code {
  background: var(--panel-2);
  padding: 0 0.35rem;
  border-radius: 4px;
  word-break: break-all;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
    gap: 0.65rem;
    padding: 0.65rem;
    padding-top: 0;
  }

  .mobile-topbar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    position: sticky;
    top: 0;
    z-index: 40;
    background: color-mix(in srgb, var(--panel) 92%, transparent);
    border: 1px solid var(--border);
    border-radius: 0 0 var(--radius) var(--radius);
    padding: 0.45rem 0.55rem;
    padding-top: max(0.45rem, env(safe-area-inset-top));
    backdrop-filter: blur(10px);
    margin: 0 -0.65rem;
    width: calc(100% + 1.3rem);
  }

  .mobile-brand {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }

  .mobile-brand .title {
    font-size: 15px;
  }

  .nav-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    background: #00000088;
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
    border-radius: 0;
    transform: translateX(-105%);
    transition: transform 0.2s ease;
    padding-top: max(1rem, env(safe-area-inset-top));
    padding-bottom: max(1rem, env(safe-area-inset-bottom));
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

  .nav {
    flex-direction: column;
    flex-wrap: nowrap;
  }

  .sidebar-foot {
    flex-direction: column;
  }
}
</style>
