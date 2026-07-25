<template>
  <div class="stack webssh">
    <div v-if="phase === 'form'" class="stack">
      <SshCredentialFields v-model="creds" />
      <div class="row">
        <button class="primary" :disabled="busy" @click="connect">
          {{ busy ? '连接中…' : '连接 WebSSH' }}
        </button>
      </div>
      <p v-if="status" class="muted" style="margin: 0; font-size: 12px">{{ status }}</p>
      <div v-if="error" class="error-box">{{ error }}</div>
    </div>
    <div v-else class="stack">
      <div class="row" style="justify-content: space-between">
        <span class="muted" style="font-size: 12px">{{ status || (phase === 'live' ? '已连接' : '连接中…') }}</span>
        <button class="danger" @click="disconnect">断开</button>
      </div>
      <div ref="termEl" class="term-host"></div>
      <div v-if="error" class="error-box">{{ error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { wsUrl } from '@/api/client'
import SshCredentialFields, { type SshCredModel } from '@/components/SshCredentialFields.vue'

const props = defineProps<{
  tenantId: string
  instanceId: string
}>()

const creds = reactive<SshCredModel>({
  username: 'ubuntu',
  port: 22,
  authMode: 'key',
  privateKeyPem: '',
  password: '',
})

const termEl = ref<HTMLElement | null>(null)
const phase = ref<'form' | 'connecting' | 'live'>('form')
const busy = ref(false)
const error = ref('')
const status = ref('')

let term: Terminal | null = null
let fit: FitAddon | null = null
let socket: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null
let authSent = false

function disposeTerm() {
  resizeObserver?.disconnect()
  resizeObserver = null
  try {
    term?.dispose()
  } catch {
    /* ignore */
  }
  term = null
  fit = null
}

function disconnect() {
  try {
    socket?.close()
  } catch {
    /* ignore */
  }
  socket = null
  phase.value = 'form'
  busy.value = false
  authSent = false
  status.value = '已断开'
  disposeTerm()
}

async function ensureTerm() {
  await nextTick()
  if (!termEl.value) return
  if (term) {
    fit?.fit()
    return
  }
  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: 'Consolas, "Courier New", monospace',
    theme: {
      background: '#0b1220',
      foreground: '#e2e8f0',
    },
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(termEl.value)
  fit.fit()
  term.onData((data) => {
    if (socket && socket.readyState === WebSocket.OPEN && phase.value === 'live') {
      socket.send(data)
    }
  })
  resizeObserver = new ResizeObserver(() => {
    try {
      fit?.fit()
      if (term && socket && socket.readyState === WebSocket.OPEN && phase.value === 'live') {
        socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }
    } catch {
      /* ignore */
    }
  })
  resizeObserver.observe(termEl.value)
}

function sendAuth() {
  if (!socket || authSent) return
  authSent = true
  const cols = term?.cols || 120
  const rows = term?.rows || 40
  socket.send(
    JSON.stringify({
      username: creds.username || 'ubuntu',
      port: creds.port || 22,
      private_key_pem: creds.authMode === 'key' ? creds.privateKeyPem : null,
      password: creds.authMode === 'password' ? creds.password : null,
      cols,
      rows,
    }),
  )
  if (creds.authMode === 'password') creds.password = ''
}

async function connect() {
  error.value = ''
  status.value = ''
  authSent = false
  if (creds.authMode === 'key' && !creds.privateKeyPem.trim()) {
    error.value = '请粘贴或选择 SSH 私钥'
    return
  }
  if (creds.authMode === 'password' && !creds.password) {
    error.value = '请输入 SSH 密码'
    return
  }
  busy.value = true
  phase.value = 'connecting'
  await nextTick()
  await ensureTerm()

  const url = wsUrl(`/tenants/${props.tenantId}/instances/${props.instanceId}/webssh`)
  const ws = new WebSocket(url)
  socket = ws
  ws.binaryType = 'arraybuffer'

  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      if (ev.data.startsWith('{') && ev.data.endsWith('}')) {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'ready') {
            status.value = msg.message || '就绪'
            sendAuth()
            return
          }
          if (msg.type === 'connected') {
            phase.value = 'live'
            busy.value = false
            status.value = `已连接 ${msg.username}@${msg.host}`
            return
          }
          if (msg.type === 'error') {
            error.value = msg.message || 'WebSSH 错误'
            busy.value = false
            status.value = msg.message || ''
            if (phase.value !== 'live') {
              // stay on terminal view so user sees error, or bounce to form
              phase.value = 'form'
              disposeTerm()
            }
            return
          }
          if (msg.type === 'pong') return
        } catch {
          // terminal text
        }
      }
      term?.write(ev.data)
      return
    }
    const buf = ev.data as ArrayBuffer
    const text = new TextDecoder('utf-8', { fatal: false }).decode(buf)
    term?.write(text)
  }

  ws.onerror = () => {
    error.value = 'WebSocket 错误'
    busy.value = false
    phase.value = 'form'
    disposeTerm()
  }
  ws.onclose = () => {
    busy.value = false
    if (phase.value === 'live') status.value = '连接已关闭'
    phase.value = 'form'
    socket = null
    disposeTerm()
  }

  status.value = '正在握手…'
}

onBeforeUnmount(() => {
  disconnect()
})
</script>

<style scoped>
.term-host {
  height: min(55dvh, 520px);
  min-height: 220px;
  width: 100%;
  max-width: 100%;
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px;
  overflow: hidden;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.04);
}
@media (max-width: 600px) {
  .term-host {
    height: min(50dvh, 420px);
    min-height: 180px;
  }
}
.term-host :deep(.xterm) {
  height: 100%;
}
.term-host :deep(.xterm-viewport) {
  overflow-y: auto !important;
}
</style>
