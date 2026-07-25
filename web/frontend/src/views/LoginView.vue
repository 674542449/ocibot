<template>
  <div class="login-page">
    <form class="card login-card stack" @submit.prevent="submit">
      <div>
        <h1>OCIBot Web</h1>
        <p class="muted">Oracle Cloud 多租户实例管理面板（网页版）</p>
      </div>

      <div class="tabs row">
        <button type="button" :class="{ primary: mode === 'login' }" @click="mode = 'login'">登录</button>
        <button type="button" :class="{ primary: mode === 'register' }" @click="mode = 'register'">
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
        <input v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" placeholder="123456" />
      </div>

      <div v-if="error" class="error-box">{{ error }}</div>
      <div v-if="hint" class="success-box">{{ hint }}</div>

      <button class="primary" type="submit" :disabled="loading">
        {{ loading ? '请稍候…' : mode === 'login' ? '登录' : '创建账号' }}
      </button>
      <p class="muted" style="font-size: 12px">
        首次使用请注册本地管理员账号。OCI API 私钥仅保存在服务端（加密存储），不会进入浏览器本地。
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
  padding: 1rem;
  padding-top: max(1rem, env(safe-area-inset-top));
  padding-bottom: max(1rem, env(safe-area-inset-bottom));
}
.login-card {
  width: min(420px, 100%);
}
h1 {
  margin: 0 0 0.25rem;
  font-size: clamp(1.25rem, 5vw, 1.5rem);
}
</style>
