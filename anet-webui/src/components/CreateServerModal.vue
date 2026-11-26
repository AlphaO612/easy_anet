<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import { useRouter } from 'vue-router'
import NodeInvitationSteps from './NodeInvitationSteps.vue'
import { api as request } from '@/api/client'
import { GetIngressRoutes } from '@/api/devices'
import { CreateServer, IssueNodeInvitation } from '@/api/servers'
import { advanced } from '@/composables/operatorMode'
import type { CreateServerRequest, NodeCredential, NodeRole, Server } from '@/models/server'
import { NODE_ROLE_OPTIONS, REGION_OPTIONS } from '@/utils/regions'

const show = defineModel<boolean>()
const emit = defineEmits<{ created: [] }>()
const router = useRouter()

type Job = { state: 'idle' | 'running' | 'done' | 'failed'; detail: string }

const defaults = (): CreateServerRequest => ({
  name: '',
  address: '',
  public_key: '',
  ssh_user: 'root',
  quic_port: 48010,
  ssh_port: 22,
  is_active: true,
  region: '',
  tags: '',
  node_role: 'exit',
})

const form = ref<CreateServerRequest>(defaults())
const role = ref<NodeRole>('exit')
const sshPassword = ref('')
const panelApiUrl = ref('')
const panelApiCandidates = ref<string[]>([])
const server = ref<Server>()
const invitation = ref<NodeCredential>()
const busy = ref(false)
const error = ref('')
const hostFingerprint = ref('')
const installJob = ref<Job>({ state: 'idle', detail: '' })

let pollTimer: number | undefined
let pollVersion = 0

function detectExternalApi(): string {
  try {
    const candidate = window.location.origin + '/api/v1'
    const url = new URL(candidate)
    const host = url.hostname.toLowerCase()
    if (
      url.protocol === 'https:'
      && !['localhost', '[::1]'].includes(host)
      && !host.startsWith('127.')
      && !host.endsWith('.localhost')
    ) {
      return candidate
    }
  } catch {
    // ignore
  }
  return ''
}

const canQuickInstall = computed(() =>
  Boolean(
    form.value.name.trim()
    && form.value.address.trim()
    && form.value.quic_port
    && form.value.quic_port >= 1
    && form.value.quic_port <= 65535
    && sshPassword.value.length > 0
    && panelApiUrl.value.trim().startsWith('https://'),
  ),
)

const canCreateOnly = computed(() =>
  Boolean(
    form.value.name.trim()
    && form.value.address.trim()
    && form.value.quic_port
    && form.value.quic_port >= 1
    && form.value.quic_port <= 65535,
  ),
)

function extractErr(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const data = err.response?.data as { error?: string } | string | undefined
    if (typeof data === 'string' && data.trim()) return data
    if (data && typeof data === 'object' && typeof data.error === 'string' && data.error.trim()) {
      return data.error
    }
  }
  return fallback
}

async function pollStatus(nodeId: string, version: number) {
  try {
    const job = await request<Job>(`/node-install/${nodeId}/status`)
    if (version !== pollVersion) return
    installJob.value = job
    if (job.state === 'running') {
      pollTimer = window.setTimeout(() => pollStatus(nodeId, version), 2500)
    } else if (job.state === 'done') {
      emit('created')
    }
  } catch (err) {
    if (version === pollVersion) {
      error.value = extractErr(err, 'Не удалось получить статус установки.')
    }
  }
}

watch(show, (open) => {
  window.clearTimeout(pollTimer)
  ++pollVersion
  if (!open) {
    sshPassword.value = ''
    return
  }
  form.value = defaults()
  role.value = 'exit'
  sshPassword.value = ''
  panelApiUrl.value = detectExternalApi()
  panelApiCandidates.value = panelApiUrl.value ? [panelApiUrl.value] : []
  void GetIngressRoutes().then((res) => {
    const list = [...panelApiCandidates.value]
    const pushUrl = (raw?: string | null) => {
      const clean = (raw || '').trim().replace(/\/+$/, '')
      if (!clean) return
      const full = clean.endsWith('/api/v1') ? clean : `${clean}/api/v1`
      if (full.startsWith('https://') && !list.includes(full)) list.push(full)
    }
    pushUrl(res.origin)
    for (const r of res.routes || []) {
      const host = (r.sni || r.entry_host || '').trim()
      if (host && host.includes('.') && !/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
        pushUrl(`https://${host}`)
      }
    }
    panelApiCandidates.value = list
    if (!panelApiUrl.value && list.length) {
      panelApiUrl.value = list[0]!
    }
  }).catch(() => {})
  server.value = undefined
  invitation.value = undefined
  hostFingerprint.value = ''
  installJob.value = { state: 'idle', detail: '' }
  error.value = ''
})

async function createAndInvite() {
  busy.value = true
  error.value = ''
  try {
    if (!server.value) {
      server.value = await CreateServer({
        ...form.value,
        node_role: role.value,
        ssh_user: form.value.ssh_user?.trim() || 'root',
      })
      emit('created')
    }
    invitation.value = await IssueNodeInvitation(server.value.id)
  } catch (err) {
    error.value = extractErr(
      err,
      server.value
        ? 'Сервер сохранён. Выпустите приглашение ещё раз.'
        : 'Не удалось сохранить сервер. Проверьте адрес и порты.',
    )
  } finally {
    busy.value = false
  }
}

async function quickInstallBySsh() {
  if (!canQuickInstall.value) return
  busy.value = true
  error.value = ''
  try {
    if (!server.value) {
      server.value = await CreateServer({
        ...form.value,
        node_role: role.value,
        ssh_user: form.value.ssh_user?.trim() || 'root',
      })
      emit('created')
    }
    if (!invitation.value) {
      invitation.value = await IssueNodeInvitation(server.value.id)
    }
    // 1. Получаем ED25519-отпечаток хоста
    const fp = await request<{ fingerprint: string; address: string }>(
      `/node-install/${server.value.id}/fingerprint`,
      { method: 'POST', data: {} },
    )
    hostFingerprint.value = fp.fingerprint
    // 2. Сразу запускаем автоматическую установку по SSH
    installJob.value = await request<Job>(`/node-install/${server.value.id}/start`, {
      method: 'POST',
      data: {
        fingerprint: fp.fingerprint,
        token: invitation.value.token,
        password: sshPassword.value,
        api: panelApiUrl.value.trim(),
      },
    })
    sshPassword.value = ''
    window.clearTimeout(pollTimer)
    void pollStatus(server.value.id, ++pollVersion)
  } catch (err) {
    error.value = extractErr(err, 'Не удалось запустить установку по SSH. Проверьте IP, логин и пароль.')
  } finally {
    busy.value = false
  }
}

function next(pool = false) {
  if (!server.value) return
  show.value = false
  router.push(
    pool
      ? { path: '/pools', query: { member: server.value.id } }
      : { path: '/profile-topology', query: { [role.value]: server.value.id } },
  )
}

onBeforeUnmount(() => {
  ++pollVersion
  window.clearTimeout(pollTimer)
  sshPassword.value = ''
})
</script>

<template>
  <v-dialog
    v-model="show"
    max-width="680"
    :persistent="busy || installJob.state === 'running'"
  >
    <v-card :title="installJob.state !== 'idle' ? 'Установка ноды по SSH' : invitation ? 'Присоединить сервер' : 'Мастер добавления ноды'">
      <v-card-text>
        <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>

        <!-- Шаг 1: единая форма (IP + логин + пароль SSH) -->
        <template v-if="!invitation && installJob.state === 'idle'">
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.trim="form.name"
                label="Название сервера"
                placeholder="Например, Германия Exit-4"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.trim="form.address"
                label="Публичный IP или домен VPS"
                placeholder="195.78.48.216"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="role"
                label="Тип ноды"
                variant="outlined"
                density="comfortable"
                :items="NODE_ROLE_OPTIONS"
                item-title="title"
                item-value="value"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="form.quic_port"
                label="Порт ANet (UDP)"
                type="number"
                min="1"
                max="65535"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.region"
                :items="REGION_OPTIONS"
                item-title="title"
                item-value="code"
                label="Страна / Регион"
                variant="outlined"
                density="comfortable"
                clearable
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.tags"
                label="Группы / теги (через запятую)"
                placeholder="Exit, Europe, Streaming"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
          </v-row>

          <v-divider class="my-3" />
          <div class="text-subtitle-2 font-weight-bold mb-2">
            Автоустановка по SSH (логин + пароль)
          </div>
          <div class="text-caption text-medium-emphasis mb-3">
            Заполните пароль SSH, чтобы панель сама подключилась к VPS, поставила rootless Docker, загрузила образы и зарегистрировала узел.
            Пароль передаётся одноразово и нигде не сохраняется.
          </div>

          <v-row dense>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model.trim="form.ssh_user"
                label="Логин SSH"
                placeholder="root"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model.number="form.ssh_port"
                label="Порт SSH"
                type="number"
                min="1"
                max="65535"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="5">
              <v-text-field
                v-model="sshPassword"
                type="password"
                autocomplete="new-password"
                label="Пароль SSH (для автоустановки)"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12">
              <v-combobox
                v-model.trim="panelApiUrl"
                :items="panelApiCandidates"
                label="HTTPS-адрес API панели (выберите шлюз или введите свой)"
                placeholder="https://gateway1.altnight.art/api/v1"
                variant="outlined"
                density="compact"
              />
            </v-col>
          </v-row>

          <template v-if="advanced">
            <v-expansion-panels variant="accordion" class="mt-2">
              <v-expansion-panel title="Дополнительные транспорты (WS / AHTTP / WRTC / ключ)">
                <v-expansion-panel-text>
                  <v-text-field v-model="form.public_key" label="Публичный ключ (опционально)" density="compact" variant="outlined" />
                  <v-text-field v-model.number="form.vnc_port" label="Порт VNC" type="number" density="compact" variant="outlined" />
                  <v-text-field v-model="form.websocket_url" label="WebSocket URL" density="compact" variant="outlined" />
                  <v-text-field v-model="form.ahttp_url" label="AHTTP URL" density="compact" variant="outlined" />
                  <v-text-field v-model="form.wrtc_url" label="WRTC URL" density="compact" variant="outlined" />
                  <v-select
                    v-if="form.wrtc_url"
                    v-model="form.wrtc_mode"
                    label="Режим WRTC"
                    density="compact"
                    variant="outlined"
                    :items="['auto', 'media_video', 'p2p_direct', 'jvb_datachannel', 'ws']"
                  />
                </v-expansion-panel-text>
              </v-expansion-panel>
            </v-expansion-panels>
          </template>
        </template>

        <!-- Шаг 2A: Прогресс автоустановки по SSH -->
        <template v-else-if="installJob.state !== 'idle'">
          <div class="mb-3">
            <strong>{{ server?.name }}</strong> · <code>{{ server?.ssh_user || 'root' }}@{{ server?.address }}:{{ server?.ssh_port || 22 }}</code>
          </div>
          <div v-if="hostFingerprint" class="text-caption text-medium-emphasis mb-3">
            SSH ED25519: <code>{{ hostFingerprint }}</code>
          </div>
          <v-alert
            variant="tonal"
            :type="installJob.state === 'done' ? 'success' : installJob.state === 'failed' ? 'error' : 'info'"
            class="mb-3"
          >
            {{ installJob.detail || 'Выполняется установка…' }}
          </v-alert>
          <v-progress-linear v-if="installJob.state === 'running'" indeterminate color="primary" class="mb-2" />
        </template>

        <!-- Шаг 2B: Ручное / расширенное приглашение -->
        <template v-else-if="invitation && server">
          <NodeInvitationSteps :server="server" :invitation="invitation" />
        </template>
      </v-card-text>

      <v-card-actions class="flex-wrap ga-2">
        <v-btn :disabled="busy" @click="show = false">
          {{ invitation || installJob.state !== 'idle' ? 'Закрыть' : 'Отмена' }}
        </v-btn>
        <v-spacer />
        <template v-if="!invitation && installJob.state === 'idle'">
          <v-btn
            variant="tonal"
            :loading="busy"
            :disabled="!canCreateOnly"
            @click="createAndInvite"
          >
            Только создать (ручная установка)
          </v-btn>
          <v-btn
            color="primary"
            prepend-icon="mdi-rocket-launch"
            :loading="busy"
            :disabled="!canQuickInstall"
            @click="quickInstallBySsh"
          >
            Добавить и установить по SSH
          </v-btn>
        </template>
        <template v-else-if="server">
          <v-btn v-if="installJob.state === 'failed'" variant="outlined" @click="installJob = { state: 'idle', detail: '' }">
            Назад к параметрам
          </v-btn>
          <v-btn variant="tonal" @click="next(true)">В пулы балансировки</v-btn>
          <v-btn color="primary" @click="next(false)">В топологию</v-btn>
        </template>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
