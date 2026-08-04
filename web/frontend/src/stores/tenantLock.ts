/**
 * 锁定租户：把某个租户设为「默认」，其他页面进入时自动选它，不用每页重选一次。
 *
 * Stored on the server against the account, not in localStorage: the choice is a
 * property of the operator, so it has to follow them to another browser, another
 * device, or a private window. It arrives with /auth/me alongside the session and
 * is written back with PUT /auth/locked-tenant.
 *
 * The in-memory ref is what every page reads, so `pickTenantId` stays synchronous
 * — pages pick a tenant during their first render and cannot await a round trip.
 * Writes are optimistic: the UI updates immediately and the request follows, so a
 * click never waits on the network to feel like it worked.
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

import api, { type Tenant } from '@/api/client'

const lockedId = ref('')
/** Display name for the sidebar badge; resolved from the tenant list, so it is a
 *  convenience only — the id is the authoritative part and the server's copy. */
const lockedName = ref('')

export const lockedTenantId = computed(() => lockedId.value)
export const lockedTenantName = computed(() => lockedName.value)
export const hasLockedTenant = computed(() => !!lockedId.value)

/** Seed from the session payload. Called by the auth store on every /auth/me. */
export function setLockedTenantFromSession(id: string): void {
  lockedId.value = id || ''
  if (!id) lockedName.value = ''
}

/** Drop local state on sign-out so the next account does not inherit it. */
export function resetTenantLock(): void {
  lockedId.value = ''
  lockedName.value = ''
}

async function persist(tenantId: string): Promise<void> {
  try {
    await api.put('/auth/locked-tenant', { tenant_id: tenantId })
  } catch {
    // The optimistic value stays for this session rather than snapping back
    // under the cursor; the next /auth/me reconciles it with the server.
  }
}

export function lockTenant(tenant: Tenant): void {
  lockedId.value = tenant.id
  lockedName.value = tenant.name
  void persist(tenant.id)
}

export function unlockTenant(): void {
  lockedId.value = ''
  lockedName.value = ''
  void persist('')
}

/** Resolve the badge's display name from a tenant list, without the fallback
 *  and self-healing that `pickTenantId` performs. Pages that show every tenant
 *  (rather than picking one) call this so the sidebar chip has a name: the
 *  session only carries the id, and the name is not the server's to cache. */
export function syncLockedTenantName(tenants: Tenant[]): void {
  if (!lockedId.value) return
  const found = tenants.find((t) => t.id === lockedId.value)
  if (found) lockedName.value = found.name
}

export function isTenantLocked(id: string): boolean {
  return !!id && lockedId.value === id
}

/**
 * Which tenant a page should open with. Also self-heals: a lock pointing at a
 * tenant that no longer exists (deleted or disabled) is cleared rather than left
 * to silently fall through on every page forever.
 */
export function pickTenantId(tenants: Tenant[], queryTenant?: unknown): string {
  const query = String(queryTenant ?? '')
  if (query && tenants.some((t) => t.id === query)) return query

  if (lockedId.value) {
    const found = tenants.find((t) => t.id === lockedId.value)
    if (found) {
      // Keep the badge honest after a rename.
      if (found.name !== lockedName.value) lockedName.value = found.name
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
