<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { isAxiosError } from 'axios'
import { api as request } from '@/api/client'
import type { NodeCredential, Server } from '@/models/server'

const props = defineProps<{ server: Server; invitation: NodeCredential; api: string }>()
type Job = { state: 'idle' | 'running' | 'done' | 'failed'; detail: string }

const password = ref('')
const pin = ref('')
const address = ref('')
const verified = ref(false)
const progress = ref<Job>({ state: 'idle', detail: '' })
const busy = ref(false)
const error = ref('')
const secure = window.isSecureContext
let timer: number | undefined
let revision = 0

function failure(err: unknown) {
  if (isAxiosError(err) && typeof err.response?.data?.error === 'string') return err.response.data.error
  return 'Сервис установки недоступен. Проверьте соединение с панелью и обновите компоненты панели.'
}

const route = (action: string) => `/node-install/${props.server.id}/${action}`

async function poll(version: number) {
  try {
    const job = await request<Job>(route('status'))
    if (version !== revision) return
    progress.value = job
    error.value = ''
    if (job.state === 'running') timer = window.setTimeout(() => poll(version), 2500)
  } catch (err) {
    if (version === revision) error.value = failure(err)
  }
}

watch(() => props.server.id, () => {
  window.clearTimeout(timer)
  password.value = ''
  pin.value = ''
  verified.value = false
  progress.value = { state: 'idle', detail: '' }
  void poll(++revision)
}, { immediate: true })

async function fingerprint() {
  busy.value = true
  verified.value = false
  pin.value = ''
  error.value = ''
  try {
    const found = await request<{ fingerprint: string; address: string }>(route('fingerprint'), {
      method: 'POST', data: {},
    })
    pin.value = found.fingerprint
    address.value = found.address
  } catch (err) {
    error.value = failure(err)
  } finally { busy.value = false }
}

async function install() {
  if (!verified.value || !secure || !props.api || !password.value) return
  error.value = ''
  busy.value = true
  try {
    progress.value = await request<Job>(route('start'), {
      method: 'POST',
      data: { fingerprint: pin.value, token: props.invitation.token, password: password.value, api: props.api },
    })
    window.clearTimeout(timer)
    void poll(++revision)
  } catch (err) {
    error.value = failure(err)
    verified.value = false
  } finally {
    password.value = ''
    busy.value = false
  }
}
onBeforeUnmount(() => { ++revision; window.clearTimeout(timer); password.value = '' })
</script>

<template>
  <section>
    <v-alert v-if="!secure" type="warning" variant="tonal">
      Для ввода SSH-пароля откройте панель через HTTPS или SSH-туннель на localhost.
    </v-alert>
    <template v-else-if="progress.state !== 'running' && progress.state !== 'done'">
      <p class="mb-3">Панель подключится к {{ server.ssh_user || 'root' }}@{{ server.address }},
        порт {{ server.ssh_port || 22 }}, скачает выпуск на эту VPS и зарегистрирует выходной узел.</p>
      <v-text-field v-model="password" type="password" autocomplete="off" label="Пароль SSH новой VPS" />
      <v-btn variant="outlined" :disabled="busy" @click="fingerprint">Получить отпечаток SSH</v-btn>
      <div v-if="pin" class="my-3">
        <p>В консоли провайдера этой VPS выполните</p>
        <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256</code>
        <p class="mt-2">{{ address }} · ED25519</p>
        <code class="ssh-pin">{{ pin }}</code>
        <v-checkbox v-model="verified" hide-details label="Отпечаток совпал с консолью VPS" />
      </div>
      <v-btn class="my-2" color="primary" :loading="busy" :disabled="!verified || !password || !api"
             @click="install">Установить на VPS</v-btn>
    </template>
    <v-alert v-if="progress.detail" class="my-2" variant="tonal"
        :type="progress.state === 'done' ? 'success' : progress.state === 'failed' ? 'error' : 'info'">
      {{ progress.detail }}
    </v-alert>
    <v-progress-linear v-if="progress.state === 'running'" indeterminate class="mt-2" />
    <v-alert v-if="error" type="error" variant="tonal" class="mt-2">
      {{ error }} <v-btn size="small" variant="text" @click="poll(++revision)">Проверить статус</v-btn>
    </v-alert>
  </section>
</template>

<style scoped>
.ssh-pin { overflow-wrap: anywhere; }
</style>
