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
        <router-link to="/">实例</router-link>
        <router-link to="/launch">创建实例</router-link>
        <router-link to="/boot-volumes">引导卷</router-link>
        <router-link to="/tenants">租户</router-link>
        <router-link to="/jobs">任务中心</router-link>
        <router-link to="/account">账号用量</router-link>
        <router-link to="/backup">备份恢复</router-link>
        <router-link to="/audit">审计日志</router-link>
        <router-link to="/settings">设置</router-link>
        <router-link v-if="auth.isAdmin" to="/admin">用户管理</router-link>
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
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

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
.nav a.router-link-active {
  background: #1d4ed855;
  border-color: #3b82f6;
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
