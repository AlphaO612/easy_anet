<script setup lang="ts">
import { computed, ref } from 'vue'
import type { NodeCredential, Server } from '@/models/server'
import AutoNodeInstall from './AutoNodeInstall.vue'

const props = defineProps<{ server: Server; invitation: NodeCredential }>()
const mode = ref<'manual' | 'auto'>('auto')
function externalApi(value: string) {
  try {
    const url = new URL(value)
    const host = url.hostname.toLowerCase()
    return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash
      && url.pathname.replace(/\/$/, '').endsWith('/api/v1') && !['localhost', '[::1]'].includes(host)
      && !host.startsWith('127.') && !host.endsWith('.localhost')
  } catch { return false }
}
const suggested = window.location.origin + '/api/v1'
const api = ref(externalApi(suggested) ? suggested : '')
const validApi = computed(() => externalApi(api.value))
const ssh = computed(() => {
  const { address, ssh_user, ssh_port } = props.server
  const user = ssh_user || 'root'
  const port = ssh_port || 22
  if (!/^[A-Za-z0-9][A-Za-z0-9.:-]*$/.test(address) || !/^[a-zA-Z0-9_][a-zA-Z0-9_-]*$/.test(user)
    || !Number.isInteger(port) || port < 1 || port > 65535) return ''
  return `ssh -p ${port} ${user}@${address}`
})
const install = 'file=$(mktemp) && curl -fLsS --retry 2 -o "$file" https://github.com/AlphaO612/easy_anet/releases/download/anet-easy-2026.11.26.2/join-node.sh && bash "$file"'
const payload = computed(() => {
  if (!validApi.value) return ''
  const json = JSON.stringify({
    api: api.value.trim(), host: props.server.address, port: props.server.quic_port || 4519,
    node_id: props.invitation.node_id, token: props.invitation.token,
  })
  return btoa(Array.from(new TextEncoder().encode(json), byte => String.fromCharCode(byte)).join(''))
})
const copied = ref('')
const fallback = ref('')
async function copy(label: string, text: string) {
  fallback.value = copied.value = ''
  try {
    await navigator.clipboard.writeText(text)
    copied.value = label
  } catch {
    fallback.value = text
  }
}
</script>

<template>
  <p class="mb-3">Приглашение предназначено администратору VPS <strong>{{ server.address }}</strong>. Оно действует 15 минут и используется один раз.</p>
  <v-text-field v-model.trim="api" label="HTTPS-адрес API панели для новой VPS" placeholder="https://panel.example.org/api/v1"
    hint="Укажите адрес, доступный с новой VPS. При открытии панели через SSH-туннель потребуется отдельный HTTPS-адрес API." persistent-hint
    :error-messages="api && !validApi ? ['Укажите HTTPS-адрес с окончанием /api/v1, доступный вне localhost.'] : []" />
  <v-radio-group v-model="mode" inline label="Как установить узел" class="mt-4">
    <v-radio label="Автоматически по SSH" value="auto" />
    <v-radio label="Самостоятельно" value="manual" />
  </v-radio-group>
  <AutoNodeInstall v-if="mode === 'auto' && validApi" :server="server" :invitation="invitation" :api="api" />
  <div v-else-if="mode === 'manual'">
    <p><strong>1. На компьютере</strong> откройте терминал и войдите на новую VPS. Если SSH запросит пароль, введите пароль пользователя {{ server.ssh_user || 'root' }} от этой VPS.</p>
    <pre v-if="ssh">{{ ssh }}</pre>
    <v-btn v-if="ssh" size="small" variant="text" @click="copy('Команда SSH', ssh)">Скопировать команду SSH</v-btn>
    <v-alert v-else type="warning">Проверьте адрес, SSH-порт и имя пользователя в карточке сервера.</v-alert>
    <p class="mt-4"><strong>2. На новой VPS</strong> выполните команду ниже. Потребуются Debian или Ubuntu amd64, /dev/net/tun и права root или sudo. Команда скачает готовые образы, проверит контрольную сумму и подготовит Docker.</p>
    <pre>{{ install }}</pre>
    <v-btn size="small" variant="text" @click="copy('Команда установки', install)">Скопировать команду установки</v-btn>
    <p class="mt-4"><strong>3. Когда установщик запросит код</strong>, скопируйте его кнопкой ниже, вставьте в терминал новой VPS и нажмите Enter. При вводе символы скрыты.</p>
    <v-btn :disabled="!validApi || !ssh" variant="tonal" @click="copy('Код приглашения', payload)">Скопировать код приглашения</v-btn>
    <p class="mt-3">После запуска откройте «Серверы» и дождитесь состояния «Онлайн». Если 15 минут истекли, откройте карточку этого же сервера и выпустите новое приглашение.</p>
  </div>
  <span v-if="copied" role="status">{{ copied }} — скопировано.</span>
  <v-textarea v-if="fallback" :model-value="fallback" readonly label="Выделите и скопируйте вручную" rows="3" @focus="($event.target as HTMLTextAreaElement).select()" />
</template>

<style scoped>
pre { margin: 8px 0; padding: 12px; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
