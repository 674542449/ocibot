<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>审计日志</h2>
        <p class="muted" style="margin: 0.2rem 0 0">危险操作与登录记录</p>
      </div>
      <div class="page-tools">
        <button class="primary" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
      </div>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>动作</th>
            <th>目标</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && items.length === 0">
            <td colspan="4" class="muted">暂无审计记录</td>
          </tr>
          <tr v-for="a in items" :key="a.id">
            <td class="muted" style="font-size: 12px; white-space: nowrap">
              {{ formatTime(a.created_at) }}
            </td>
            <td><span class="badge">{{ a.action }}</span></td>
            <td style="font-size: 12px; word-break: break-all">{{ a.target || '—' }}</td>
            <td class="muted" style="font-size: 12px; word-break: break-all">{{ a.detail || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import api, { type AuditItem } from '@/api/client'

const items = ref<AuditItem[]>([])
const loading = ref(false)
const error = ref('')

function formatTime(v: string) {
  if (!v) return '—'
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get<AuditItem[]>('/audit', { params: { limit: 200 } })
    items.value = data
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
