<template>
  <div class="login-page">
    <form class="login-card stack" @submit.prevent="submit">
      <div class="login-brand">
        <img class="logo-img" src="/logo.svg" width="44" height="44" alt="OCIBot" />
        <div>
          <h1>OCIBot</h1>
          <p class="muted">Oracle Cloud 多租户实例管理</p>
        </div>
      </div>

      <div class="mode-tabs">
        <button type="button" :class="{ active: mode === 'login' }" @click="mode = 'login'">
          登录
        </button>
        <button type="button" :class="{ active: mode === 'register' }" @click="mode = 'register'">
          注册
        </button>
      </div>

      <div class="field">
        <label>用户名</label>
        <input v-model="username" autocomplete="username" required minlength="3" />
      </div>
      <div class="field">
        <label>密码</label>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          minlength="8"
        />
      </div>
      <div v-if="needTotp" class="field">
        <label>两步验证码（6 位）</label>
        <input
          v-model="totpCode"
          inputmode="numeric"
          autocomplete="one-time-code"
          placeholder="123456"
        />
      </div>

      <div v-if="error" class="error-box">{{ error }}</div>
      <div v-if="hint" class="success-box">{{ hint }}</div>

      <button class="primary submit" type="submit" :disabled="loading">
        {{ loading ? '请稍候…' : mode === 'login' ? '登录' : '创建账号' }}
      </button>
      <p class="muted tip">
        首次注册自动成为管理员。OCI 私钥仅服务端加密存储，不会进入浏览器。
      </p>
    </form>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
    const redirect = (route.query.redirect as string) || '/'
    await router.replace(redirect)
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
  place-items: center;
  padding: 1.25rem;
  padding-top: max(1.25rem, env(safe-area-inset-top));
  padding-bottom: max(1.25rem, env(safe-area-inset-bottom));
  background:
    radial-gradient(900px 420px at 10% -10%, var(--bg-glow-1), transparent),
    radial-gradient(700px 380px at 100% 0%, var(--bg-glow-2), transparent),
    var(--bg);
  color: var(--text);
}

.login-card {
  width: min(420px, 100%);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 1.5rem 1.4rem 1.35rem;
  box-shadow: var(--shadow-md);
}

.login-brand {
  display: flex;
  gap: 0.85rem;
  align-items: center;
}

.logo-img {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: block;
  box-shadow: 0 6px 16px rgba(51, 112, 255, 0.35);
  flex-shrink: 0;
}

.logo {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #3370ff, #6b4eff);
  box-shadow: 0 6px 16px rgba(51, 112, 255, 0.35);
  flex-shrink: 0;
}

h1 {
  margin: 0;
  font-size: clamp(1.25rem, 4.5vw, 1.45rem);
  font-weight: 650;
  letter-spacing: -0.02em;
}

.mode-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.25rem;
  padding: 0.25rem;
  background: var(--panel-2);
  border-radius: 10px;
  border: 1px solid var(--border);
}

.mode-tabs button {
  border: none;
  box-shadow: none;
  background: transparent;
  color: var(--text-secondary);
  min-height: 36px;
  font-weight: 560;
}

.mode-tabs button.active {
  background: var(--panel);
  color: var(--accent);
  box-shadow: var(--shadow-sm);
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
</style>
