<template>
  <!-- 一块「标题 + 几行字」形状的灰条，代替还没读到的卡片内容。
       读屏软件读的是里面那句隐藏文字：只有 aria-label、没有文字内容的状态区域，很多读屏软件不念。 -->
  <div class="sk-block" role="status">
    <span class="sr-only">{{ label }}</span>
    <span v-if="title" class="sk sk-title" aria-hidden="true" style="width: 38%" />
    <span v-for="n in lines" :key="n" class="sk" aria-hidden="true" :style="{ width: width(n) }" />
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ lines?: number; title?: boolean; label?: string }>(), {
  lines: 3,
  title: false,
  label: '正在读取…',
})

// 每行长短错开，像一段正在来的文字，而不是一排一样长的砖。
function width(n: number): string {
  return `${[92, 78, 85, 64, 88, 70][(n - 1) % 6]}%`
}
</script>
