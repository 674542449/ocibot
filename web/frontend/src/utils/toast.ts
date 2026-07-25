/** Non-blocking toast notifications (fixed bottom; does not shift layout). */

export type ToastKind = 'ok' | 'err' | 'info'

type ToastItem = {
  id: number
  text: string
  kind: ToastKind
}

let seq = 0
const toasts: ToastItem[] = []
const listeners = new Set<() => void>()
const timers = new Map<number, number>()

function emit() {
  for (const fn of listeners) {
    try {
      fn()
    } catch {
      /* ignore */
    }
  }
}

export function subscribeToasts(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function listToasts(): readonly ToastItem[] {
  return toasts
}

export function dismissToast(id: number) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
  const t = timers.get(id)
  if (t) {
    window.clearTimeout(t)
    timers.delete(id)
  }
  emit()
}

export function showToast(text: string, kind: ToastKind = 'ok', ms = 1800) {
  const id = ++seq
  const item: ToastItem = { id, text: String(text || '').slice(0, 200), kind }
  toasts.push(item)
  // Cap stack so a spammy page can't pile up forever.
  while (toasts.length > 4) {
    const old = toasts.shift()
    if (old) {
      const ot = timers.get(old.id)
      if (ot) {
        window.clearTimeout(ot)
        timers.delete(old.id)
      }
    }
  }
  emit()
  if (ms > 0) {
    timers.set(
      id,
      window.setTimeout(() => dismissToast(id), ms) as unknown as number,
    )
  }
  return id
}

/** Copy text and show a toast; never throws. Returns true on success. */
export async function copyText(text: string, label = '已复制'): Promise<boolean> {
  const v = (text || '').trim()
  if (!v || v === '—') return false
  try {
    await navigator.clipboard.writeText(v)
    const short = v.length > 36 ? `${v.slice(0, 36)}…` : v
    showToast(`${label}：${short}`, 'ok')
    return true
  } catch {
    showToast('复制失败', 'err')
    return false
  }
}
