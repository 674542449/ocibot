<template>
  <div class="login-page">
    <!-- Identity canvas. Deliberately dark in both themes: this is the threshold
         into the panel, and a surface that commits to one look reads as designed
         rather than as a page that happens to inherit whatever is set. -->
    <section class="canvas" aria-hidden="true">
      <div class="grid-field"></div>
      <div class="sweep"></div>

      <div class="canvas-inner">
        <img class="mark" src="/logo.svg" width="28" height="28" alt="" />
        <h1 class="wordmark">OCIBOT</h1>
        <p class="lede">在一处管理跨账号、跨区域的服务器。</p>
      </div>

      <!-- Real, not decorative: the build you are about to sign in to. -->
      <dl v-if="health.version" class="readout">
        <div>
          <dt>版本</dt>
          <dd>v{{ health.version }}</dd>
        </div>
        <div>
          <dt>服务</dt>
          <dd><i class="dot"></i>正常</dd>
        </div>
      </dl>
    </section>

    <section class="form-side">
      <form class="login-form stack" @submit.prevent="submit">
        <header class="form-head">
          <h2>{{ mode === 'login' ? '登录' : '创建账号' }}</h2>
          <p class="muted">
            {{ mode === 'login' ? '使用你的面板账号继续。' : '第一个注册的账号将成为管理员。' }}
          </p>
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
          <input
            id="login-user"
            v-model="username"
            autocomplete="username"
            required
            minlength="3"
          />
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
          {{ loading ? '请稍候…' : mode === 'login' ? '登录' : '创建账号' }}
        </button>

        <p class="muted tip">API 私钥仅在服务端加密存储，不会进入浏览器。</p>
      </form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api/client'
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
const health = reactive<{ version: string }>({ version: '' })

onMounted(async () => {
  try {
    const { data } = await api.get<{ version?: string }>('/health')
    health.version = String(data?.version || '')
  } catch {
    // Silent: an unreachable health endpoint is not the sign-in form's problem,
    // and an error here would sit above the field the operator came to fill in.
  }
})

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
.login-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  color: var(--text);
  background: var(--bg);
}

/* ---------------------------------------------------------------- canvas */

.canvas {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: clamp(2rem, 5vw, 4rem);
  /* Fixed palette, not tokens: this panel keeps its look in either theme. */
  background: #0b0d16;
  color: #eef0fa;
}

/* A machined surface rather than a gradient wash — the panel is an instrument. */
.grid-field {
  position: absolute;
  inset: -1px;
  pointer-events: none;
  background-image:
    linear-gradient(to right, rgba(139, 132, 245, 0.09) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(139, 132, 245, 0.09) 1px, transparent 1px);
  background-size: 46px 46px;
  mask-image: radial-gradient(120% 90% at 20% 15%, #000 35%, transparent 78%);
  -webkit-mask-image: radial-gradient(120% 90% at 20% 15%, #000 35%, transparent 78%);
}

/* The single moving element on the page. It reads as an instrument refreshing,
   which is what this product actually does — it waits and watches for capacity.
   One slow orchestrated motion; nothing else on the page animates. */
.sweep {
  position: absolute;
  left: 0;
  right: 0;
  height: 140px;
  pointer-events: none;
  background: linear-gradient(
    to bottom,
    transparent,
    rgba(139, 132, 245, 0.13) 62%,
    rgba(196, 192, 255, 0.5) 78%,
    transparent 79%
  );
  animation: sweep 9s cubic-bezier(0.45, 0, 0.25, 1) infinite;
}

@keyframes sweep {
  0% {
    transform: translateY(-140px);
    opacity: 0;
  }
  12% {
    opacity: 1;
  }
  88% {
    opacity: 1;
  }
  100% {
    transform: translateY(100vh);
    opacity: 0;
  }
}

.canvas-inner {
  position: relative;
  margin-top: auto;
  margin-bottom: auto;
}

.mark {
  display: block;
  width: 28px;
  height: 28px;
  margin-bottom: 1.6rem;
  opacity: 0.9;
}

.wordmark {
  margin: 0;
  font-family: var(--font-mono);
  font-size: clamp(2.4rem, 6.4vw, 4.25rem);
  font-weight: 600;
  letter-spacing: -0.045em;
  line-height: 0.95;
  color: #fff;
}

.lede {
  margin: 1.15rem 0 0;
  max-width: 22ch;
  font-size: clamp(0.95rem, 1.6vw, 1.1rem);
  line-height: 1.6;
  color: rgba(238, 240, 250, 0.62);
}

.readout {
  position: relative;
  display: flex;
  gap: 2.5rem;
  margin: 0;
}

.readout div {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.readout dt {
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  /* 0.42 measured 3.73:1 against the canvas — too low for 10px type. */
  color: rgba(238, 240, 250, 0.55);
}

.readout dd {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 0.45rem;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  color: rgba(238, 240, 250, 0.92);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #3dd68c;
  box-shadow: 0 0 0 3px rgba(61, 214, 140, 0.16);
}

/* ------------------------------------------------------------- form side */

.form-side {
  display: grid;
  place-items: center;
  padding: clamp(1.5rem, 4vw, 3rem);
  padding-top: max(1.5rem, env(safe-area-inset-top));
  padding-bottom: max(1.5rem, env(safe-area-inset-bottom));
}

.login-form {
  width: min(370px, 100%);
}

.form-head h2 {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 650;
  letter-spacing: -0.02em;
}

.form-head p {
  margin: 0.35rem 0 0;
  font-size: 13px;
}

.mode-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.25rem;
  padding: 0.25rem;
  background: var(--panel-2);
  border-radius: var(--radius);
  border: 1px solid var(--border);
}

.mode-tabs button {
  border: none;
  box-shadow: none;
  background: transparent;
  color: var(--text-secondary);
  min-height: 34px;
  font-weight: 560;
}

.mode-tabs button.active {
  background: var(--panel);
  color: var(--accent);
  box-shadow: var(--shadow-sm);
}

/* A six-digit code is read back digit by digit — space it like a keypad. */
.totp {
  font-family: var(--font-mono);
  font-size: 1.1rem;
  letter-spacing: 0.4em;
  text-align: center;
}

.submit {
  width: 100%;
  min-height: 42px;
  font-weight: 600;
}

.tip {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
}

/* ------------------------------------------------------------ responsive */

@media (max-width: 860px) {
  .login-page {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }
  .canvas {
    padding: 1.75rem 1.5rem 1.5rem;
  }
  .canvas-inner {
    margin: 0;
  }
  .mark {
    margin-bottom: 1rem;
  }
  .wordmark {
    font-size: 2rem;
  }
  .lede {
    margin-top: 0.6rem;
    font-size: 0.9rem;
    max-width: none;
  }
  .readout {
    margin-top: 1.5rem;
    gap: 1.75rem;
  }
  .sweep {
    animation-duration: 7s;
  }
}

/* The sweep is ambience, not information — it is the first thing to go. The
   global reduced-motion rule also neutralises it; this keeps it off even if
   that rule is ever scoped differently. */
@media (prefers-reduced-motion: reduce) {
  .sweep {
    display: none;
  }
}
</style>
