import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api, { type TokenResponse } from '@/api/client'

export const useAuthStore = defineStore('auth', () => {
  // Cookie is primary; localStorage token is optional fallback / display.
  const token = ref<string | null>(localStorage.getItem('ocibot_token'))
  const username = ref<string | null>(localStorage.getItem('ocibot_username'))
  const isAdmin = ref(false)
  const totpEnabled = ref(false)
  const sessionChecked = ref(false)

  const isLoggedIn = computed(() => !!username.value || !!token.value)

  function setSession(data: TokenResponse) {
    token.value = data.access_token
    username.value = data.username
    // Keep token for Bearer fallback; cookie is also set by server (HttpOnly).
    localStorage.setItem('ocibot_token', data.access_token)
    localStorage.setItem('ocibot_username', data.username)
  }

  function clearLocal() {
    token.value = null
    username.value = null
    isAdmin.value = false
    totpEnabled.value = false
    localStorage.removeItem('ocibot_token')
    localStorage.removeItem('ocibot_username')
  }

  async function logout() {
    try {
      await api.post('/auth/logout')
    } catch {
      // ignore
    }
    clearLocal()
  }

  async function login(user: string, password: string, totpCode = '') {
    const { data } = await api.post<TokenResponse>('/auth/login', {
      username: user,
      password,
      totp_code: totpCode,
    })
    setSession(data)
    await refreshMe()
  }

  async function register(user: string, password: string) {
    const { data } = await api.post<TokenResponse>('/auth/register', {
      username: user,
      password,
    })
    setSession(data)
    await refreshMe()
  }

  async function refreshMe() {
    try {
      const { data } = await api.get<{
        id: string
        username: string
        is_admin?: boolean
        totp_enabled?: boolean
      }>('/auth/me')
      username.value = data.username
      isAdmin.value = !!data.is_admin
      totpEnabled.value = !!data.totp_enabled
      localStorage.setItem('ocibot_username', data.username)
      return true
    } catch {
      clearLocal()
      return false
    } finally {
      sessionChecked.value = true
    }
  }

  return {
    token,
    username,
    isAdmin,
    totpEnabled,
    isLoggedIn,
    sessionChecked,
    login,
    register,
    logout,
    refreshMe,
    setSession,
    clearLocal,
  }
})
