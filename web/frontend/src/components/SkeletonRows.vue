<template>
  <!-- 放在 <tbody> 里：画几行灰条代替还没读到的数据。读屏软件只会听到一句「正在读取…」。 -->
  <tr v-for="r in rows" :key="r" class="sk-row" :aria-hidden="r === 1 ? undefined : 'true'">
    <td v-for="c in cols" :key="c">
      <span v-if="r === 1 && c === 1" class="sr-only" role="status">{{ label }}</span>
      <span class="sk" aria-hidden="true" :style="{ width: width(r, c) }" />
    </td>
  </tr>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ cols: number; rows?: number; label?: string }>(), {
  rows: 4,
  label: '正在读取…',
})

// 每格宽度错开一点：一排一样长的砖看起来像坏掉的表格，长短不一才像正在来的数据。
function width(r: number, c: number): string {
  return `${42 + ((r * 7 + c * 13) % 46)}%`
}
</script>
