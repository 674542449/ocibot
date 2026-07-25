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
  bottom: 1.25rem;
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
  padding: 0.55rem 0.9rem;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 8px 24px #00000066;
  border: 1px solid transparent;
  word-break: break-all;
  animation: toast-in 0.18s ease-out;
}
.toast-ok {
  background: #14532d;
  border-color: #22c55e;
  color: #bbf7d0;
}
.toast-err {
  background: #7f1d1d;
  border-color: #ef4444;
  color: #fecaca;
}
.toast-info {
  background: #1e3a5f;
  border-color: #3b82f6;
  color: #dbeafe;
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
