"""Shared icons for sidebar / headers (inline SVG, currentColor)."""

<script setup lang="ts">
withDefaults(
  defineProps<{
    name: string
    size?: number
  }>(),
  { size: 18 },
)
</script>

<template>
  <svg
    class="icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    aria-hidden="true"
  >
    <!-- instances -->
    <g v-if="name === 'instances'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3.5" y="4" width="17" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </g>
    <!-- launch / plus -->
    <g v-else-if="name === 'launch'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 8v8M8 12h8" />
    </g>
    <!-- jobs -->
    <g v-else-if="name === 'jobs'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </g>
    <!-- storage -->
    <g v-else-if="name === 'storage'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <ellipse cx="12" cy="6.5" rx="7.5" ry="2.5" />
      <path d="M4.5 6.5v5c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-5" />
      <path d="M4.5 11.5v5c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-5" />
    </g>
    <!-- tenants -->
    <g v-else-if="name === 'tenants'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="9" cy="9" r="3" />
      <circle cx="16.5" cy="10" r="2.4" />
      <path d="M3.5 18c.6-2.6 2.8-4 5.5-4s4.9 1.4 5.5 4" />
      <path d="M13.2 18c.3-1.7 1.6-2.8 3.3-2.8 1.5 0 2.7.9 3.2 2.3" />
    </g>
    <!-- account / chart -->
    <g v-else-if="name === 'account'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 19V5" />
      <path d="M4 19h16" />
      <path d="M8 15v-4M12 15V8M16 15v-6" />
    </g>
    <!-- backup -->
    <g v-else-if="name === 'backup'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 4v10" />
      <path d="M8.5 10.5 12 14l3.5-3.5" />
      <path d="M5 18h14" />
    </g>
    <!-- audit -->
    <g v-else-if="name === 'audit'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M8 5h11v14H8z" />
      <path d="M5 5h3v14H5z" />
      <path d="M11 9h5M11 12h5M11 15h3" />
    </g>
    <!-- settings -->
    <g v-else-if="name === 'settings'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path
        d="M12 3.5v2.2M12 18.3v2.2M4.9 6.5l1.6 1.6M17.5 15.9l1.6 1.6M3.5 12h2.2M18.3 12h2.2M4.9 17.5l1.6-1.6M17.5 8.1l1.6-1.6"
      />
    </g>
    <!-- admin -->
    <g v-else-if="name === 'admin'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3.5 19 7v5c0 4.2-2.9 7.4-7 8.5-4.1-1.1-7-4.3-7-8.5V7l7-3.5Z" />
      <path d="M9.5 12.2 11.2 14l3.5-3.8" />
    </g>
    <!-- key / password -->
    <g v-else-if="name === 'key'" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="8" cy="14" r="3.2" />
      <path d="M10.5 11.8 19 3.5M16.2 3.8l2.5 2.5M14.5 5.5l2 2" />
    </g>
    <!-- check -->
    <g v-else-if="name === 'check'" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 12.5 10 17.5 19 7" />
    </g>
    <!-- fallback circle -->
    <g v-else stroke="currentColor" stroke-width="1.7">
      <circle cx="12" cy="12" r="8" />
    </g>
  </svg>
</template>

<style scoped>
.icon {
  display: block;
  flex-shrink: 0;
}
</style>
