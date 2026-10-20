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
      <p v-if="hostKeyNote" class="muted" style="margin: 0; font-size: 12px">{{ hostKeyNote }}</p>
      <div v-if="error" class="error-box" style="white-space: pre-line">{{ error }}</div>
      <!-- Host key changed: offer the deliberate reset needed after a rebuild. -->
      <div v-if="hostKeyMismatch" class="row" style="gap: 0.5rem; flex-wrap: wrap">
        <button type="button" :disabled="resetting" @click="resetHostKey">
          {{ resetting ? '重置中…' : '重置主机密钥并重试' }}
        </button>
        <span class="muted" style="font-size: 12px">仅在你确认自己重装/重建了该实例时使用</span>
      </div>
    </div>
    <div v-else class="stack">
      <div class="row" style="justify-content: space-between">
        <span class="muted" style="font-size: 12px">{{ status || (phase === 'live' ? '已连接' : '连接中…') }}</span>
        <div class="row" style="gap: 0.4rem">
          <button type="button" :disabled="!hasSelection" @click="copySelection">复制选中</button>
          <button type="button" :disabled="phase !== 'live'" @click="pasteFromClipboard">粘贴</button>
          <button class="danger" @click="disconnect">断开</button>
        </div>
      </div>
      <p class="muted" style="margin: 0; font-size: 12px">
        复制：选中文字后按 Ctrl+C 或单击右键；粘贴：Ctrl+V 或单击右键（没选中文字时）。没有选中文字时 Ctrl+C 仍是中断（^C）。
      </p>
      <div ref="termEl" class="term-host"></div>
      <!-- 浏览器不允许网页直接读剪贴板时（http 访问、拒绝了权限、Firefox）用这个框：
           在普通输入框里粘贴哪儿都行（包括手机长按），再发到终端。 -->
      <div v-if="pasteBoxOpen" class="stack">
        <label class="muted" style="font-size: 12px">把要粘贴的内容贴到下面，再点「发送到终端」</label>
        <textarea ref="pasteBoxEl" v-model="pasteBoxText" rows="4" spellcheck="false"></textarea>
        <div class="row">
          <button class="primary" type="button" :disabled="!pasteBoxText" @click="sendPasteBox">发送到终端</button>
          <button type="button" @click="closePasteBox">取消</button>
        </div>
      </div>
      <div v-if="error" class="error-box">{{ error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import api, { wsUrl } from '@/api/client'
import SshCredentialFields, { type SshCredModel } from '@/components/SshCredentialFields.vue'
import { showToast, writeClipboard } from '@/utils/toast'

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
// Host key state (trust on first use). A mismatch is surfaced with an explicit
// reset action rather than being silently accepted.
const hostKeyNote = ref('')
const hostKeyMismatch = ref(false)
const resetting = ref(false)

async function resetHostKey() {
  resetting.value = true
  try {
    await api.delete(`/tenants/${props.tenantId}/instances/${props.instanceId}/host-key`)
    hostKeyMismatch.value = false
    error.value = ''
    // sendAuth() deliberately wipes the password after transmitting it, so in
    // password mode there is nothing left to reconnect with — ask for it again
    // instead of firing a connect that would immediately fail validation.
    const missingCredential =
      (creds.authMode === 'password' && !creds.password) ||
      (creds.authMode === 'key' && !creds.privateKeyPem.trim())
    if (missingCredential) {
      hostKeyNote.value = '已重置主机密钥记录，请重新填写 SSH 凭据后点击「连接 WebSSH」'
      return
    }
    hostKeyNote.value = '已重置主机密钥记录，正在重新连接…'
    await connect()
  } catch (e: any) {
    error.value = e?.message || '重置主机密钥失败'
  } finally {
    resetting.value = false
  }
}

let term: Terminal | null = null
let fit: FitAddon | null = null
let socket: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null
let authSent = false

// ---- 复制 / 粘贴
//
// xterm.js 在 Windows / Linux 上把 Ctrl+C、Ctrl+V 当成发给服务器的控制键（^C 中断、
// ^V 字面量），于是在终端里既复制不出来也粘贴不进去。这里接管：
//   * 有选中文字时 Ctrl+C（以及 Ctrl+Shift+C、Ctrl+Insert）复制；没选中时 Ctrl+C 照旧是中断；
//   * Ctrl+V（以及 Ctrl+Shift+V、Shift+Insert）交给浏览器原生粘贴 —— xterm 自己的输入框
//     收到 paste 事件后照常发给服务器。原生粘贴不需要剪贴板权限，http 访问也能用；
//   * 右键：有选中就复制；没选中且浏览器允许读剪贴板（https）就直接粘贴，否则留给浏览器
//     自己的右键菜单（选「粘贴」即可，xterm 会把输入框挪到鼠标下）。
// Mac 上 Cmd+C / Cmd+V 本来就不归 xterm 管，浏览器原生处理。
const hasSelection = ref(false)
const pasteBoxOpen = ref(false)
const pasteBoxText = ref('')
const pasteBoxEl = ref<HTMLTextAreaElement | null>(null)
let contextMenuHost: HTMLElement | null = null

async function copySelection() {
  const text = term?.getSelection() || ''
  if (!text) return
  const ok = await writeClipboard(text)
  // 只报字数不回显内容：终端输出可能就是密钥、密码之类。
  showToast(ok ? `已复制 ${text.length} 个字符` : '复制失败（浏览器限制），请改用右键菜单复制', ok ? 'ok' : 'err')
  term?.clearSelection()
  term?.focus()
}

function pasteText(text: string) {
  if (!text || !term || phase.value !== 'live') return
  // term.paste 会处理换行（\r\n → \r）和括号粘贴模式（远端 shell 开了的话，多行粘贴不会被
  // 一行一行地立刻执行），然后经 onData 发给服务器。
  term.paste(text)
  term.focus()
}

/** 浏览器允不允许网页直接读剪贴板：只有 https / localhost 这类安全上下文里才有。 */
function canReadClipboard(): boolean {
  return window.isSecureContext && typeof navigator.clipboard?.readText === 'function'
}

async function pasteFromClipboard() {
  // navigator.clipboard.readText 只在 https（安全上下文）里有，首次会弹权限询问；
  // http 访问、拒绝权限、Firefox 都读不到 —— 那就给一个输入框让用户自己粘进去。
  if (canReadClipboard()) {
    try {
      const text = await navigator.clipboard.readText()
      if (text) {
        pasteText(text)
        return
      }
    } catch {
      /* 退回输入框 */
    }
  }
  openPasteBox()
}

async function openPasteBox() {
  pasteBoxText.value = ''
  pasteBoxOpen.value = true
  await nextTick()
  pasteBoxEl.value?.focus()
}

function closePasteBox() {
  pasteBoxOpen.value = false
  pasteBoxText.value = ''
  term?.focus()
}

function sendPasteBox() {
  pasteText(pasteBoxText.value)
  closePasteBox()
}

function onTerminalKey(e: KeyboardEvent): boolean {
  if (e.type !== 'keydown' || !term) return true
  const key = e.key.toLowerCase()
  const ctrlOnly = e.ctrlKey && !e.altKey && !e.metaKey
  // 复制：Ctrl+Shift+C 总是复制；Ctrl+C 只在有选中时复制，否则仍发 ^C 中断。
  if (ctrlOnly && key === 'c' && (e.shiftKey || term.hasSelection())) {
    e.preventDefault()
    void copySelection()
    return false
  }
  if (ctrlOnly && e.key === 'Insert' && term.hasSelection()) {
    e.preventDefault()
    void copySelection()
    return false
  }
  // 粘贴：不让 xterm 把它当控制键，也**不** preventDefault —— 让浏览器做原生粘贴。
  if (ctrlOnly && key === 'v') return false
  if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && e.key === 'Insert') return false
  return true
}

function onTerminalContextMenu(e: MouseEvent) {
  if (!term) return
  if (term.hasSelection()) {
    e.preventDefault()
    void copySelection()
    return
  }
  if (phase.value === 'live' && canReadClipboard()) {
    e.preventDefault()
    void pasteFromClipboard()
  }
  // 否则不拦：浏览器自己的右键菜单里有「粘贴」。
}

function disposeTerm() {
  resizeObserver?.disconnect()
  resizeObserver = null
  contextMenuHost?.removeEventListener('contextmenu', onTerminalContextMenu)
  contextMenuHost = null
  hasSelection.value = false
  pasteBoxOpen.value = false
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
    // 终端刻意保持深底 —— 这不是「暗色模式」：服务器上的程序输出 ANSI 颜色时默认
    // 背景是深色的，浅底上的黄 / 青字几乎看不见。只是从冷藏青换成和面板同一族的暖黑。
    theme: {
      background: '#1f1e1b',
      foreground: '#f5f4ed',
    },
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(termEl.value)
  fit.fit()
  term.attachCustomKeyEventHandler(onTerminalKey)
  term.onSelectionChange(() => {
    hasSelection.value = !!term?.hasSelection()
  })
  contextMenuHost = termEl.value
  contextMenuHost.addEventListener('contextmenu', onTerminalContextMenu)
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
  hostKeyMismatch.value = false
  authSent = false
  if (creds.authMode === 'key' && !creds.privateKeyPem.trim()) {
    error.value = '请粘贴或选择 SSH 私钥'
    return
  }
  if (creds.authMode === 'password' && !creds.password) {
    error.value = '请输入 SSH 密码'
    return
  }
  // 还挂着一个旧连接就先关掉：否则它会一直占着服务端的 SSH 会话，直到超时。
  if (socket) {
    const old = socket
    socket = null
    try {
      old.close()
    } catch {
      /* ignore */
    }
  }
  busy.value = true
  phase.value = 'connecting'
  await nextTick()
  await ensureTerm()

  const url = wsUrl(`/tenants/${props.tenantId}/instances/${props.instanceId}/webssh`)
  const ws = new WebSocket(url)
  socket = ws
  ws.binaryType = 'arraybuffer'
  // 旧连接的事件可能在新连接建立之后才到（点「断开」后马上「连接」、或重置主机密钥
  // 后自动重连）：close 事件是异步的。下面的处理器改的都是模块级的 socket / term，
  // 不先确认「这还是当前那个连接」，旧 socket 迟到的 onclose 就会把新 socket 置空、
  // 把新终端销毁 —— 新会话从此收不到输入，服务端那条 SSH 连接也成了孤儿。
  const current = () => socket === ws

  ws.onmessage = (ev) => {
    if (!current()) return
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
          if (msg.type === 'hostkey') {
            hostKeyNote.value = msg.message || ''
            return
          }
          if (msg.type === 'error') {
            error.value = msg.message || 'WebSSH 错误'
            busy.value = false
            status.value = msg.code === 'hostkey_mismatch' ? '主机密钥校验失败' : msg.message || ''
            if (msg.code === 'hostkey_mismatch') hostKeyMismatch.value = true
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
    // 直接把字节交给 xterm，不要自己 TextDecoder。
    //
    // 每来一帧就 new 一个 TextDecoder，等于把每帧都当成一段完整的 UTF-8：
    // 一个多字节字符如果正好被 8KiB 的读边界劈成两半，前半帧末尾和后半帧开头
    // 会各自解成替换字符（），中文输出里这种情况很常见。xterm.js 自带的是
    // 跨 write 保持状态的流式 UTF-8 解码器，交给它就不会在边界上出错。
    //
    // 后端现在把终端数据一律走**二进制**帧、控制消息才走文本帧，所以这条分支
    // 拿到的就是原始字节。
    term?.write(new Uint8Array(ev.data as ArrayBuffer))
  }

  ws.onerror = () => {
    if (!current()) return
    error.value = 'WebSocket 错误'
    busy.value = false
    phase.value = 'form'
    disposeTerm()
  }
  ws.onclose = () => {
    if (!current()) return
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
  background: #1f1e1b;
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
