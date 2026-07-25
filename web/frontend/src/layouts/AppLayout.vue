<template>
  <div class="layout">
    <aside class="sidebar card">
      <div class="brand">
        <div class="logo">OCI</div>
        <div>
          <div class="title">OCIBot Web</div>
          <div class="muted small">{{ auth.username }}<span v-if="auth.isAdmin"> · 管理员</span></div>
        </div>
      </div>
      <nav class="nav">
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="{ 'nav-active': isNavActive(item) }"
          active-class=""
          exact-active-class=""
        >
          {{ item.label }}
        </router-link>
      </nav>
      <div class="sidebar-foot stack">
        <button type="button" @click="toggleTheme">
          {{ theme === 'light' ? '🌙 暗色模式' : '☀️ 亮色模式' }}
        </button>
        <button @click="onLogout">退出登录</button>
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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

type NavItem = { to: string; label: string; match?: 'exact' | 'prefix' | 'instances' }

const navItems = computed<NavItem[]>(() => {
  const items: NavItem[] = [
    // `/` is a prefix of every path — must use exact/instances matching, not default active.
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
  if (auth.isAdmin) items.push({ to: '/admin', label: '用户管理', match: 'exact' })
  return items
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
  // exact — also treat /boot-volumes redirect target under storage already handled
  return path === item.to
}

const workerAlive = ref(true)
const workerChecked = ref(false)
const heartbeatText = ref('从未收到心跳')
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
    workerChecked.value = true
  } catch {
    // API unreachable is surfaced by individual pages; don't double-report here.
  }
}

async function onLogout() {
  await auth.logout()
  router.push({ name: 'login' })
}

onMounted(() => {
  applyTheme()
  checkWorker()
  timer = window.setInterval(checkWorker, 30_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 240px 1fr;
  min-height: 100vh;
  gap: 1rem;
  padding: 1rem;
}
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  position: sticky;
  top: 1rem;
  height: calc(100vh - 2rem);
}
.brand {
  display: flex;
  gap: 0.75rem;
  align-items: center;
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
}
.nav a {
  color: var(--text);
  padding: 0.55rem 0.7rem;
  border-radius: 8px;
  border: 1px solid transparent;
}
/* Do not use .router-link-active: link to="/" matches every route as a prefix. */
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
}
.main {
  min-width: 0;
}
.worker-banner code {
  background: var(--panel-2);
  padding: 0 0.35rem;
  border-radius: 4px;
}
@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: static;
    height: auto;
  }
  .nav {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .sidebar-foot {
    flex-direction: row;
  }
}
</style>
