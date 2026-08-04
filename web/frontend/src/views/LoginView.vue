<template>
  <div class="login-page">
    <!-- Full-bleed stage. The globe is the page; the form floats on it. -->
    <div class="stage" aria-hidden="true">
      <GlobeField />
      <div class="vignette"></div>
      <div class="scanline"></div>
    </div>

    <header class="brand" aria-hidden="true">
      <img class="mark" src="/logo.svg" width="26" height="26" alt="" />
      <span class="brand-name">OCIBOT</span>
    </header>

    <main class="stack-area">
      <section class="hero">
        <h1 class="wordmark">
          <span class="line">OCIBOT</span>
          <span class="line accent">控制台</span>
        </h1>
        <p class="lede">多账号 · 多区域云资源管理</p>
        <p class="modules">实例 · 存储 · 网络 · 容量重试 · WebSSH · 备份</p>
      </section>

      <section class="panel">
        <form class="login-form stack" @submit.prevent="submit">
          <header class="form-head">
            <h2>{{ mode === 'login' ? '登录' : '创建账号' }}</h2>
            <p v-if="mode === 'register'" class="muted">首个注册账号将获得管理员权限。</p>
          </header>

          <div class="mode-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              :aria-selected="mode === 'login'"
              :class="{ active: mode === 'login' }"
              @click="mode = 'login'"
            >
              登录
            </button>
            <button
              type="button"
              role="tab"
              :aria-selected="mode === 'register'"
              :class="{ active: mode === 'register' }"
              @click="mode = 'register'"
            >
              注册
            </button>
          </div>

          <div class="field">
            <label for="login-user">用户名</label>
            <input id="login-user" v-model="username" autocomplete="username" required minlength="3" />
          </div>
          <div class="field">
            <label for="login-pass">密码</label>
            <input
              id="login-pass"
              v-model="password"
              type="password"
              autocomplete="current-password"
              required
              minlength="8"
            />
          </div>
          <div v-if="needTotp" class="field">
            <label for="login-totp">两步验证码</label>
            <input
              id="login-totp"
              v-model="totpCode"
              class="totp"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="6"
              placeholder="000000"
            />
          </div>

          <div v-if="error" class="error-box">{{ error }}</div>
          <div v-if="hint" class="success-box">{{ hint }}</div>

          <button class="primary submit" type="submit" :disabled="loading">
            {{ loading ? (mode === 'login' ? '验证中…' : '创建中…') : mode === 'login' ? '登录' : '创建账号' }}
          </button>

          <p class="muted tip">API 私钥仅在服务端加密存储，不会进入浏览器。</p>
        </form>
      </section>
    </main>

    <!-- Real, not decorative: the build you are about to sign in to. -->
    <footer v-if="health.version" class="readout">
      <span class="ro"><i class="ro-k">版本</i><b>v{{ health.version }}</b></span>
      <span class="ro">
        <i class="ro-k">API</i>
        <b><i class="dot" :class="{ down: health.status !== 'ok' }"></i>{{ apiStatusLabel }}</b>
      </span>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api/client'
import GlobeField from '@/components/GlobeField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const totpCode = ref('')
const needTotp = ref(false)
const loading = ref(false)
const error = ref('')
const hint = ref('')

/** Version of the running build, read before sign-in (/api/health is public). */
const health = reactive<{ version: string; status: string }>({ version: '', status: '' })

onMounted(async () => {
  try {
    const { data } = await api.get<{ version?: string; status?: string }>('/health')
    health.version = String(data?.version || '')
    health.status = String(data?.status || '')
  } catch {
    // Silent: an unreachable health endpoint is not the sign-in form's problem,
    // and an error here would sit above the field the operator came to fill in.
  }
})

/** 'ok' is the only state with a settled meaning; anything else is shown
 *  verbatim rather than flattened into a word that hides which fault it is. */
const apiStatusLabel = computed(() =>
  health.status === 'ok' ? '正常' : health.status || '未知',
)

/** Only follow same-origin in-app paths, never an attacker-supplied absolute URL. */
function safeRedirect(raw: unknown): string {
  const target = typeof raw === 'string' ? raw : ''
  // Must be a single-slash-rooted path: rejects "//evil.com", "https://evil.com"
  // and "javascript:..." regardless of how the router resolves them later.
  if (!/^\/(?!\/)/.test(target)) return '/'
  return target
}

async function submit() {
  error.value = ''
  hint.value = ''
  loading.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(username.value.trim(), password.value, totpCode.value.trim())
    } else {
      await auth.register(username.value.trim(), password.value)
      hint.value = '注册成功'
    }
    await router.replace(safeRedirect(route.query.redirect))
  } catch (e: any) {
    const msg = e?.message || '请求失败'
    if (msg === 'totp_required') {
      needTotp.value = true
      error.value = '该账号已开启两步验证，请输入认证器中的 6 位验证码'
    } else {
      error.value = msg
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* The sign-in page commits to one look in either theme: it is the threshold
   into the panel, not a page that inherits whatever is set inside it. */
.login-page {
  position: relative;
  min-height: 100vh;
  min-height: 100dvh;
  overflow: hidden;
  background: #05060c;
  color: #eceefb;
  display: grid;
  grid-template-rows: auto 1fr auto;
}

/* ------------------------------------------------------------------ stage */

.stage {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

/* Darkens the edges so the form always has a quiet field to sit on, whatever
   the globe is doing behind it. */
.vignette {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(120% 80% at 50% 45%, transparent 30%, rgba(5, 6, 12, 0.82) 78%),
    linear-gradient(90deg, rgba(5, 6, 12, 0.55) 0%, transparent 35%, transparent 55%, rgba(5, 6, 12, 0.92) 88%);
}

.scanline {
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    to bottom,
    rgba(255, 255, 255, 0.018) 0 1px,
    transparent 1px 3px
  );
}

/* ------------------------------------------------------------------ chrome */

.brand {
  position: relative;
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: clamp(1.1rem, 3vw, 2rem) clamp(1.1rem, 4vw, 3rem);
}

.mark {
  width: 26px;
  height: 26px;
  display: block;
}

.brand-name {
  font-family: var(--font-mono);
  font-size: 13px;
  letter-spacing: 0.22em;
  color: rgba(236, 238, 251, 0.72);
}

.stack-area {
  position: relative;
  display: grid;
  grid-template-columns: 1fr minmax(340px, 400px);
  align-items: center;
  gap: clamp(1.5rem, 5vw, 4rem);
  padding: 0 clamp(1.1rem, 4vw, 3rem);
  min-height: 0;
}

/* -------------------------------------------------------------------- hero */

.wordmark {
  margin: 0;
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: clamp(2.1rem, 7.2vw, 5.6rem);
  line-height: 0.92;
  letter-spacing: -0.05em;
  text-transform: uppercase;
}

.line {
  display: block;
  color: #f4f5ff;
}

.line.accent {
  /* The one gradient on the page, on the one phrase that carries the idea. */
  background: linear-gradient(96deg, #8f86ff 0%, #c9c2ff 46%, #6f7bff 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.modules {
  margin: 0.5rem 0 0;
  font-family: var(--font-mono);
  font-size: 12px;
  letter-spacing: 0.04em;
  color: rgba(236, 238, 251, 0.5);
}

.dot.down {
  background: #ff6b6b;
  box-shadow: 0 0 0 3px rgba(255, 107, 107, 0.16);
}

.lede {
  margin: 1.15rem 0 0;
  max-width: 26ch;
  font-size: clamp(0.95rem, 1.5vw, 1.15rem);
  line-height: 1.65;
  color: rgba(236, 238, 251, 0.68);
}

/* ------------------------------------------------------------------- panel */

.panel {
  /* Solid, never translucent: the form is the one thing on this page that has
     to stay readable no matter what is rotating behind it. */
  background: #111320;
  border: 1px solid rgba(143, 134, 255, 0.22);
  border-radius: 16px;
  padding: clamp(1.25rem, 2.5vw, 1.9rem);
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.55);
}

.form-head h2 {
  margin: 0;
  font-size: 1.45rem;
  font-weight: 650;
  letter-spacing: -0.02em;
  color: #f2f3ff;
}

.form-head p {
  margin: 0.3rem 0 0;
  font-size: 13px;
  color: rgba(236, 238, 251, 0.6);
}

.panel :deep(label) {
  color: rgba(236, 238, 251, 0.72);
}

.panel :deep(input) {
  background: #0a0b14;
  border-color: rgba(143, 134, 255, 0.24);
  color: #f2f3ff;
}

.panel :deep(input:focus) {
  border-color: #8f86ff;
}

.mode-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.25rem;
  padding: 0.25rem;
  background: #0a0b14;
  border-radius: 10px;
  border: 1px solid rgba(143, 134, 255, 0.18);
}

.mode-tabs button {
  border: none;
  box-shadow: none;
  background: transparent;
  color: rgba(236, 238, 251, 0.62);
  min-height: 34px;
  font-weight: 560;
}

.mode-tabs button.active {
  background: rgba(143, 134, 255, 0.16);
  color: #cfc9ff;
}

/* A six-digit code is read back a digit at a time — space it like a keypad. */
.totp {
  font-family: var(--font-mono);
  font-size: 1.1rem;
  letter-spacing: 0.4em;
  text-align: center;
}

.submit {
  width: 100%;
  min-height: 44px;
  font-weight: 600;
  background: #8f86ff;
  border-color: transparent;
  color: #14121f;
}

.submit:hover:not(:disabled) {
  background: #a79fff;
}

.tip {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: rgba(236, 238, 251, 0.55);
}

/* ----------------------------------------------------------------- readout */

.readout {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 2rem;
  padding: clamp(1rem, 3vw, 1.75rem) clamp(1.1rem, 4vw, 3rem);
  padding-bottom: max(clamp(1rem, 3vw, 1.75rem), env(safe-area-inset-bottom));
}

.ro {
  display: inline-flex;
  align-items: baseline;
  gap: 0.5rem;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: rgba(236, 238, 251, 0.9);
}

.ro-k {
  font-style: normal;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: rgba(236, 238, 251, 0.55);
}

.ro b {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-weight: 600;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #4ade80;
  box-shadow: 0 0 0 3px rgba(74, 222, 128, 0.16);
}

/* -------------------------------------------------------------- responsive */

@media (max-width: 900px) {
  .stack-area {
    grid-template-columns: 1fr;
    align-content: center;
    gap: 1.5rem;
    padding-top: 1rem;
    padding-bottom: 1.5rem;
  }
  .hero {
    text-align: left;
  }
  .wordmark {
    font-size: clamp(1.9rem, 11vw, 3rem);
  }
  .lede {
    margin-top: 0.7rem;
    max-width: none;
    font-size: 0.92rem;
  }
  .ro-wide {
    display: none;
  }
}

/* The globe holds a single frame; the scanline is pure ambience and goes. */
@media (prefers-reduced-motion: reduce) {
  .scanline {
    display: none;
  }
}
</style>
