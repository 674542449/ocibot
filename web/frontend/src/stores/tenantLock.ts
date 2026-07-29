/**
 * 锁定租户：把某个租户设为「默认」，其他页面进入时自动选它，不用每页重选一次。
 *
 * Stored in localStorage rather than on the server: it is a per-browser UI
 * preference, and keeping it client-side means no new API surface and no
 * migration. The trade-off is that it does not follow you to another browser or
 * device — deliberate, and cheap to redo there.
 *
 * Selection priority every view follows (see `pickTenantId`):
 *   1. `?tenant=` in the URL — a deep link must always win, otherwise clicking
 *      through from an instance to its storage would silently switch tenants.
 *   2. the locked tenant
 *   3. the first tenant in the list
 *
 * Locking does NOT freeze the per-page dropdown: switching tenant inside a page
 * still works and is temporary. The lock only decides what a page opens with.
 */
import { computed, ref } from 'vue'

import type { Tenant } from '@/api/client'

const KEY_ID = 'ocibot_locked_tenant_id'
const KEY_NAME = 'ocibot_locked_tenant_name'

function readStorage(key: string): string {
  // localStorage throws in some privacy modes; a missing preference is not worth
  // breaking the page over.
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function writeStorage(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

const lockedId = ref(readStorage(KEY_ID))
/** Kept only so the sidebar badge can render a name without loading the tenant
 *  list; the id is the authoritative part. */
const lockedName = ref(readStorage(KEY_NAME))

export const lockedTenantId = computed(() => lockedId.value)
export const lockedTenantName = computed(() => lockedName.value)
export const hasLockedTenant = computed(() => !!lockedId.value)

export function lockTenant(tenant: Tenant): void {
  lockedId.value = tenant.id
  lockedName.value = tenant.name
  writeStorage(KEY_ID, tenant.id)
  writeStorage(KEY_NAME, tenant.name)
}

export function unlockTenant(): void {
  lockedId.value = ''
  lockedName.value = ''
  writeStorage(KEY_ID, '')
  writeStorage(KEY_NAME, '')
}

export function isTenantLocked(id: string): boolean {
  return !!id && lockedId.value === id
}

/**
 * Which tenant a page should open with. Also self-heals: a lock pointing at a
 * tenant that no longer exists (deleted, disabled, or belonging to a different
 * account after a re-login on the same browser) is cleared rather than left to
 * silently fall through on every page forever.
 */
export function pickTenantId(tenants: Tenant[], queryTenant?: unknown): string {
  const query = String(queryTenant ?? '')
  if (query && tenants.some((t) => t.id === query)) return query

  if (lockedId.value) {
    const found = tenants.find((t) => t.id === lockedId.value)
    if (found) {
      // Keep the cached display name honest after a rename; the sidebar badge
      // renders from it without loading the tenant list itself.
      if (found.name !== lockedName.value) {
        lockedName.value = found.name
        writeStorage(KEY_NAME, found.name)
      }
      return lockedId.value
    }
    // Only drop the lock once we have actually seen a list — an empty array here
    // usually means "still loading", not "that tenant is gone". A disabled tenant
    // also lands here (some pages list only enabled ones), which is intended:
    // a tenant you cannot use is not a useful default.
    if (tenants.length) unlockTenant()
  }
  return tenants[0]?.id || ''
}
