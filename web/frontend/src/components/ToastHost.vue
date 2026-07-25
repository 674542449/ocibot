<template>
  <div class="toast-host" aria-live="polite" aria-relevant="additions">
    <div
      v-for="t in items"
      :key="t.id"
      class="toast"
      :class="'toast-' + t.kind"
      role="status"
      @click="dismiss(t.id)"
    >
      {{ t.text }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { dismissToast, listToasts, subscribeToasts, type ToastKind } from '@/utils/toast'

type Item = { id: number; text: string; kind: ToastKind }
const items = shallowRef<readonly Item[]>([])
const unsub = ref<null | (() => void)>(null)

function refresh() {
  items.value = [...listToasts()]
}

function dismiss(id: number) {
  dismissToast(id)
}

onMounted(() => {
  refresh()
  unsub.value = subscribeToasts(refresh)
})

onBeforeUnmount(() => {
  unsub.value?.()
  unsub.value = null
})
</script>

<style scoped>
.toast-host {
  position: fixed;
  left: 50%;
  bottom: 1.35rem;
  transform: translateX(-50%);
  z-index: 9999;
  display: flex;
  flex-direction: column-reverse;
  gap: 0.4rem;
  pointer-events: none;
  max-width: min(92vw, 420px);
  width: max-content;
}
.toast {
  pointer-events: auto;
  cursor: pointer;
  padding: 0.65rem 1rem;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 560;
  box-shadow: var(--shadow-md);
  border: 1px solid var(--border);
  word-break: break-all;
  animation: toast-in 0.18s ease-out;
  background: var(--panel);
  color: var(--text);
}
.toast-ok {
  background: var(--ok-soft);
  border-color: transparent;
  color: #0a6e22;
}
.toast-err {
  background: var(--danger-soft);
  border-color: transparent;
  color: var(--danger);
}
.toast-info {
  background: var(--accent-soft);
  border-color: transparent;
  color: var(--accent);
}
html[data-theme='dark'] .toast-ok {
  color: var(--ok);
}
@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
