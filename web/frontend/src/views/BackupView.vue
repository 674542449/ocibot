<template>
  <div class="stack">
    <div>
      <h2 style="margin: 0">备份 / 恢复</h2>
      <p class="muted" style="margin: 0.2rem 0 0">
        导出全部租户（含私钥）为 AES-256 加密 ZIP；恢复会导入为新租户
      </p>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="msg" class="success-box">{{ msg }}</div>

    <div class="card stack">
      <h3 style="margin: 0">导出加密备份</h3>
      <div class="field">
        <label>备份密码（≥6 位）</label>
        <input v-model="exportPassword" type="password" autocomplete="new-password" />
      </div>
      <button class="primary" :disabled="!!busy" @click="doExport">
        {{ busy === 'export' ? '导出中…' : '下载加密 ZIP' }}
      </button>
    </div>

    <div class="card stack">
      <h3 style="margin: 0">从加密 ZIP 恢复</h3>
      <div class="field">
        <label>ZIP 文件</label>
        <input type="file" accept=".zip,application/zip" @change="onFile" />
      </div>
      <div class="field">
        <label>备份密码</label>
        <input v-model="importPassword" type="password" autocomplete="current-password" />
      </div>
      <button class="primary" :disabled="!!busy || !file" @click="doImport">
        {{ busy === 'import' ? '导入中…' : '导入租户' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import api from '@/api/client'

const exportPassword = ref('')
const importPassword = ref('')
const file = ref<File | null>(null)
const busy = ref('')
const error = ref('')
const msg = ref('')

function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  file.value = input.files?.[0] || null
}

async function doExport() {
  error.value = ''
  msg.value = ''
  if (exportPassword.value.trim().length < 6) {
    error.value = '备份密码至少 6 位'
    return
  }
  busy.value = 'export'
  try {
    const res = await api.post(
      '/backup/export',
      { password: exportPassword.value },
      { responseType: 'blob' },
    )
    const blob = new Blob([res.data], { type: 'application/zip' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ocibot-backup-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.zip`
    a.click()
    URL.revokeObjectURL(url)
    msg.value = '备份已下载'
  } catch (e: any) {
    // blob error may need parse
    if (e?.response?.data instanceof Blob) {
      try {
        const text = await e.response.data.text()
        const j = JSON.parse(text)
        error.value = j.detail || text
      } catch {
        error.value = e?.message || '导出失败'
      }
    } else {
      error.value = e?.message || '导出失败'
    }
  } finally {
    busy.value = ''
  }
}

async function doImport() {
  error.value = ''
  msg.value = ''
  if (!file.value) {
    error.value = '请选择 ZIP 文件'
    return
  }
  if (importPassword.value.trim().length < 6) {
    error.value = '备份密码至少 6 位'
    return
  }
  busy.value = 'import'
  try {
    const fd = new FormData()
    fd.append('password', importPassword.value)
    fd.append('file', file.value)
    const { data } = await api.post('/backup/import', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    msg.value = data.message || `已导入 ${data.imported} 个租户`
  } catch (e: any) {
    error.value = e?.message || '导入失败'
  } finally {
    busy.value = ''
  }
}
</script>
