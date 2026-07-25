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

/**
 * Fallback for insecure contexts / denied clipboard permission / older browsers.
 * Uses a transient textarea + execCommand('copy') — works on HTTP LAN deploys.
 */
function copyViaExecCommand(text: string): boolean {
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.setAttribute('aria-hidden', 'true')
    // iOS / some WebViews need the field visible & selectable.
    ta.style.position = 'fixed'
    ta.style.top = '0'
    ta.style.left = '0'
    ta.style.width = '1px'
    ta.style.height = '1px'
    ta.style.padding = '0'
    ta.style.border = 'none'
    ta.style.outline = 'none'
    ta.style.boxShadow = 'none'
    ta.style.background = 'transparent'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

async function writeClipboard(text: string): Promise<boolean> {
  // Prefer modern API when available and likely to succeed (secure context).
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through — common on http://LAN-IP, missing permission, or iframe.
    }
  }
  return copyViaExecCommand(text)
}

/** Copy text and show a toast; never throws. Returns true on success. */
export async function copyText(text: string, label = '已复制'): Promise<boolean> {
  // Normalize NBSP / zero-width / BOM that sometimes sneak in from UI text nodes.
  const v = String(text ?? '')
    .replace(/[ ​﻿]/g, ' ')
    .trim()
  if (!v || v === '—' || v === '-' || v === '–') return false
  const ok = await writeClipboard(v)
  const short = v.length > 36 ? `${v.slice(0, 36)}…` : v
  if (ok) {
    showToast(`${label}：${short}`, 'ok')
    return true
  }
  showToast('复制失败（浏览器限制，请手动全选复制）', 'err', 2800)
  return false
}
