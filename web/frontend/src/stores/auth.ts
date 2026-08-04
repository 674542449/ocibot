import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api, { type TokenResponse } from '@/api/client'
import { resetTenantLock, setLockedTenantFromSession } from '@/stores/tenantLock'

export const useAuthStore = defineStore('auth', () => {
  // Auth is the server's HttpOnly cookie. We keep only the username (for display
  // and an optimistic isLoggedIn), and validate the real session via refreshMe().
  const token = ref<string | null>(null)
  const username = ref<string | null>(localStorage.getItem('ocibot_username'))
  const isAdmin = ref(false)
  const totpEnabled = ref(false)
  const sessionChecked = ref(false)

  const isLoggedIn = computed(() => !!username.value)

  function setSession(data: TokenResponse) {
    // The token is never stored (the cookie carries auth); keep it in-memory only.
    token.value = data.access_token
    username.value = data.username
    localStorage.setItem('ocibot_username', data.username)
  }

  function clearLocal() {
    token.value = null
    username.value = null
    isAdmin.value = false
    totpEnabled.value = false
    // Otherwise the next account signing in on this browser would open every
    // page on the previous operator's default tenant.
    resetTenantLock()
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
        locked_tenant_id?: string
      }>('/auth/me')
      username.value = data.username
      isAdmin.value = !!data.is_admin
      totpEnabled.value = !!data.totp_enabled
      // The default tenant lives with the account, so it arrives with the session
      // rather than being read out of this browser's storage.
      setLockedTenantFromSession(String(data.locked_tenant_id || ''))
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
