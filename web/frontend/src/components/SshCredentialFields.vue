<template>
  <div class="stack ssh-fields">
    <p v-if="hint" class="muted" style="margin: 0; font-size: 12px">{{ hint }}</p>
    <div class="grid-2">
      <div class="field">
        <label>SSH 用户名</label>
        <div class="row">
          <input v-model="model.username" placeholder="ubuntu" style="flex: 1" />
          <button type="button" @click="model.username = 'ubuntu'">ubuntu</button>
          <button type="button" @click="model.username = 'opc'">opc</button>
          <button type="button" @click="model.username = 'root'">root</button>
        </div>
      </div>
      <div class="field">
        <label>端口</label>
        <input v-model.number="model.port" type="number" min="1" max="65535" />
      </div>
    </div>
    <div class="field">
      <label>认证方式</label>
      <div class="row">
        <label class="row muted" style="font-size: 13px">
          <input v-model="model.authMode" type="radio" value="key" style="width: auto" />
          私钥
        </label>
        <label class="row muted" style="font-size: 13px">
          <input v-model="model.authMode" type="radio" value="password" style="width: auto" />
          密码
        </label>
      </div>
    </div>
    <div v-if="model.authMode === 'key'" class="field">
      <label>SSH 私钥（仅本次会话，不会保存）</label>
      <div class="row">
        <button type="button" @click="pickKey">从文件读取</button>
      </div>
      <textarea
        v-model="model.privateKeyPem"
        rows="4"
        spellcheck="false"
        placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
      ></textarea>
    </div>
    <div v-else class="field">
      <label>SSH 密码（仅本次会话，不会保存）</label>
      <input v-model="model.password" type="password" autocomplete="off" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { pickAndReadTextFile } from '@/utils/file'

export type SshCredModel = {
  username: string
  port: number
  authMode: 'key' | 'password'
  privateKeyPem: string
  password: string
}

const model = defineModel<SshCredModel>({ required: true })

withDefaults(
  defineProps<{
    hint?: string
  }>(),
  {
    hint: '凭证只用于本次操作，不会写入服务器数据库。',
  },
)

async function pickKey() {
  const text = await pickAndReadTextFile('.pem,.key,text/plain')
  if (text == null) return
  model.value.privateKeyPem = text.trim()
  model.value.authMode = 'key'
  model.value.password = ''
}
</script>
