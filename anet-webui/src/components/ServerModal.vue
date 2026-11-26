<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import NodeInvitationSteps from './NodeInvitationSteps.vue'
import { useAppMessage } from '@/composables/useAppMessage'
import { GetNodeCommandStatus, IssueNodeInvitation, RotateNodeCredential, SetNodeAdmission, UpdateServer } from '@/api/servers'
import type { NodeRole, Server } from '@/models/server'
import { NODE_ROLE_OPTIONS, REGION_OPTIONS } from '@/utils/regions'

// ИСПРАВЛЕНО: Убрано 'show', теперь ловит дефолтный v-model из родителя
const show = defineModel<boolean>()

const props = defineProps<{
  server: Server | null
}>()

const emit = defineEmits<{
  (e: 'updated'): void
  (e: 'close'): void
}>()

const form = ref({
  name: '',
  address: '', // Заменили dsn на address
  public_key: '',
  ssh_user: '',
  is_active: true,
  quic_port: null as number | null,
  ssh_port: null as number | null,
  vnc_port: null as number | null,
  websocket_url: '',
  ahttp_url: '',
  wrtc_url: '',
  wrtc_mode: 'media_video',
  region: '',
  tags: '',
  node_role: 'auto' as NodeRole,
})

const wrtcModeOptions = [
  { title: 'Media Video (Fake Video Track over DTLS-SRTP / JVB) - 4K/Макс. скорость', value: 'media_video' },
  { title: 'P2P Direct (Прямой WebRTC DataChannel)', value: 'p2p_direct' },
  { title: 'JVB DataChannel (Colibri SCTP DataChannel)', value: 'jvb_datachannel' },
  { title: 'WebSocket (Colibri-WS Fallback)', value: 'ws' },
  { title: 'Auto (Автоматический выбор)', value: 'auto' },
]

const loading = ref(false)
const commandLoading = ref(false)
const commandStatus = ref<string | null>(null)
const credentialLoading = ref(false)
const issuedCredential = ref<{ node_id: string; token: string } | null>(null)
const credentialConfig = computed(() => issuedCredential.value
    ? `[control_plane]\nnode_id = "${issuedCredential.value.node_id}"\ntoken = "${issuedCredential.value.token}"`
    : '')
const message = useAppMessage()
let commandAbort = false

const wait = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))

const waitForCommand = async (serverId: string, commandId: string) => {
  const deadline = Date.now() + 30_000
  while (!commandAbort && Date.now() < deadline) {
    const command = await GetNodeCommandStatus(serverId, commandId)
    commandStatus.value = command.status
    if (command.status === 'succeeded') return command
    if (command.status === 'failed') throw new Error(command.error || 'Нода не выполнила команду')
    await wait(750)
  }
  throw new Error('Нода не подтвердила выполнение команды за 30 секунд')
}

const setAdmission = async (accepting: boolean) => {
  if (!props.server) return
  commandLoading.value = true
  commandAbort = false
  commandStatus.value = 'pending'
  try {
    const queued = await SetNodeAdmission(props.server.id, accepting)
    await waitForCommand(props.server.id, queued.command_id)
    message.success(accepting ? 'Нода принимает новые подключения' : 'Новые подключения запрещены')
    emit('updated')
  } catch (e) {
    message.error(e instanceof Error ? e.message : 'Не удалось выполнить команду')
  } finally {
    commandLoading.value = false
    commandStatus.value = null
  }
}

onBeforeUnmount(() => { commandAbort = true })

const isInvitation = ref(false)
const issueCredential = async (invitation: boolean) => {
  if (!props.server) return
  credentialLoading.value = true
  try {
    const issue = invitation ? IssueNodeInvitation : RotateNodeCredential
    issuedCredential.value = await issue(props.server.id)
    isInvitation.value = invitation
  } catch (e) {
    message.error(e instanceof Error ? e.message : 'Не удалось выпустить credential')
  } finally {
    credentialLoading.value = false
  }
}

const copyCredential = async () => {
  if (!issuedCredential.value) return
  await navigator.clipboard.writeText(issuedCredential.value.token)
  message.success('Credential скопирован')
}

const handleCredentialVisibility = (value: boolean) => {
  if (!value) issuedCredential.value = null
}

watch(
    () => props.server,
    (val) => {
      if (val) {
        form.value = {
          name: val.name,
          address: val.address, // Заменили dsn на address
          public_key: val.public_key,
          ssh_user: val.ssh_user || '',
          is_active: val.is_active,
          quic_port: val.quic_port !== undefined ? val.quic_port : null,
          ssh_port: val.ssh_port !== undefined ? val.ssh_port : null,
          vnc_port: val.vnc_port !== undefined ? val.vnc_port : null,
          websocket_url: val.websocket_url || '',
          ahttp_url: val.ahttp_url || '',
          wrtc_url: val.wrtc_url || '',
          wrtc_mode: val.wrtc_mode || 'media_video',
          region: val.region || '',
          tags: val.tags || '',
          node_role: val.node_role || 'auto',
        }
      }
    },
    { immediate: true }
)

const save = async () => {
  if (!props.server) return
  loading.value = true
  try {
    await UpdateServer(props.server.id, form.value)
    emit('updated')
    show.value = false
  } catch (e: any) {
    console.error(e)
    if (e.response?.data) {
      alert(`Ошибка: ${e.response.data}`)
    } else {
      alert('Произошла ошибка при сохранении')
    }
  } finally {
    loading.value = false
  }
}

const close = () => {
  commandAbort = true
  show.value = false
  emit('close')
}
</script>

<template>
  <!-- Основная модалка редактирования -->
  <v-dialog v-model="show" @update:model-value="close" max-width="650px">
    <v-card>
      <v-card-title class="text-h6 pb-4">Редактировать физический сервер</v-card-title>

      <v-card-text>
        <v-form>
          <v-text-field v-model="form.name" label="Название локации" variant="filled" class="mb-3" />

          <div class="mb-3">
            <div class="text-caption text-medium-emphasis mb-1">Тип ноды</div>
            <v-btn-toggle v-model="form.node_role" mandatory color="primary" variant="outlined" density="compact" class="flex-wrap">
              <v-btn v-for="r in NODE_ROLE_OPTIONS" :key="r.value" :value="r.value" :prepend-icon="r.icon" size="small">
                {{ r.short }}
              </v-btn>
            </v-btn-toggle>
          </div>

          <v-row dense class="mb-2">
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.region"
                :items="REGION_OPTIONS"
                item-title="title"
                item-value="code"
                label="Страна / Регион"
                variant="filled"
                clearable
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.tags"
                label="Группы / теги (через запятую)"
                placeholder="Gateway, Exit, VLESS, Fast"
                variant="filled"
              />
            </v-col>
          </v-row>

          <!-- Изменили поле DSN на "IP Адрес или Домен" -->
          <v-text-field v-model="form.address" label="IP Адрес или Домен" placeholder="e.g. 203.0.113.10 или vpn.example.com" variant="filled" class="mb-3" />

          <v-text-field v-model="form.public_key" label="Публичный ключ сервера" variant="filled" class="mb-3" />
          <v-text-field v-model="form.ssh_user" label="Пользователь SSH" variant="filled" class="mb-3" />

          <!-- Размещаем порты side-by-side -->
          <v-row class="mb-1">
            <v-col cols="12" sm="4">
              <v-text-field
                  v-model.number="form.quic_port"
                  type="number"
                  label="QUIC Port (UDP)"
                  variant="filled"
                  hide-details
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                  v-model.number="form.ssh_port"
                  type="number"
                  label="SSH Port (TCP)"
                  variant="filled"
                  hide-details
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                  v-model.number="form.vnc_port"
                  type="number"
                  label="VNC Port (TCP)"
                  variant="filled"
                  hide-details
              />
            </v-col>
          </v-row>

          <v-text-field
              v-model="form.websocket_url"
              label="WebSocket URL"
              placeholder="ws://127.0.0.1:8080/s"
              variant="filled"
              class="mb-3"
          />

          <v-text-field
              v-model="form.ahttp_url"
              label="AHTTP URL (CDN)"
              placeholder="https://your-cdn.some-host.net/api/v2/telemetry"
              variant="filled"
              class="mb-3"
          />

          <v-text-field
              v-model="form.wrtc_url"
              label="WRTC URL (Jitsi / WebRTC Room)"
              placeholder="wrtc://meet.jit.si/your-telemetry-room"
              variant="filled"
              class="mb-3"
          />

          <v-select
              v-if="form.wrtc_url"
              v-model="form.wrtc_mode"
              :items="wrtcModeOptions"
              item-title="title"
              item-value="value"
              label="Режим WRTC (wrtc_mode)"
              variant="filled"
              class="mb-3"
          />

          <v-switch v-model="form.is_active" label="Статус (ВКЛ / ВЫКЛ)" color="success" class="mb-4" />

          <!-- Блок управления подключениями -->
          <v-card v-if="server" variant="outlined" class="mb-4 pa-4">
            <div class="text-subtitle-1 font-weight-bold mb-2">Управление подключениями</div>
            <div class="mb-4">
              Фактическое состояние:
              <v-chip
                  :color="server.runtime ? (server.runtime.accepting_connections ? 'success' : 'warning') : 'default'"
                  size="small"
                  class="ml-2"
              >
                {{ !server.runtime ? 'нет данных' : server.runtime.accepting_connections ? 'принимает новые подключения' : 'новые подключения запрещены' }}
              </v-chip>
            </div>

            <div class="d-flex ga-2 mb-2">
              <v-btn
                  color="success"
                  variant="outlined"
                  :loading="commandLoading"
                  :disabled="server.runtime?.status !== 'online' || server.runtime?.accepting_connections === true"
                  @click="setAdmission(true)"
              >
                Разрешить
              </v-btn>
              <v-btn
                  color="warning"
                  variant="outlined"
                  :loading="commandLoading"
                  :disabled="server.runtime?.status !== 'online' || server.runtime?.accepting_connections === false"
                  @click="setAdmission(false)"
              >
                Запретить
              </v-btn>
            </div>
            <div v-if="commandStatus" class="text-caption text-medium-emphasis mt-2">
              Команда: {{ commandStatus === 'pending' ? 'ожидает ноду' : commandStatus === 'running' ? 'выполняется' : commandStatus }}
            </div>
          </v-card>

          <!-- Блок Control plane credential -->
          <v-card v-if="server" variant="outlined" class="pa-4">
            <div class="text-subtitle-1 font-weight-bold mb-2">Control plane credential</div>
            <div class="mb-2">
              Состояние:
              <v-chip :color="server.has_control_credential ? 'success' : 'warning'" size="small" class="ml-2">
                {{ server.has_control_credential ? 'READY' : 'SETUP REQUIRED' }}
              </v-chip>
            </div>
            <div class="text-caption text-medium-emphasis mb-4">
              Credential хранится на панели только в виде SHA-256 хэша. После перевыпуска текущая нода потеряет доступ, пока новый token не будет записан в её config.
            </div>

            <v-btn variant="outlined" class="mb-3" :loading="credentialLoading" @click="issueCredential(true)">
              Создать приглашение на 15 минут
            </v-btn>
            <div class="d-flex align-center">
              <v-btn variant="tonal" color="error" :loading="credentialLoading" @click="issueCredential(false)" class="mr-4">
                Перевыпустить credential
              </v-btn>
              <span class="text-caption text-error">Текущий токен сразу сбросится!</span>
            </div>
          </v-card>
        </v-form>
      </v-card-text>

      <v-card-actions class="px-6 pb-4">
        <v-spacer />
        <v-btn variant="text" @click="close">Отмена</v-btn>
        <v-btn color="primary" variant="flat" :loading="loading" @click="save">Сохранить</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- Модалка выдачи нового Credential -->
  <v-dialog
      :model-value="issuedCredential !== null"
      max-width="620px"
      persistent
      @update:model-value="handleCredentialVisibility"
  >
    <v-card>
      <v-card-title class="text-h6 pb-4">{{ isInvitation ? 'Одноразовое приглашение' : 'Новый credential ноды' }}</v-card-title>

      <v-card-text>
        <NodeInvitationSteps v-if="isInvitation && issuedCredential && server" :server="server" :invitation="issuedCredential" />
        <template v-else>
        <v-alert type="warning" variant="tonal" class="mb-4">
          Сохраните token сейчас: повторно панель его не покажет.
        </v-alert>

        <v-text-field :model-value="issuedCredential?.node_id" label="node_id" readonly variant="filled" class="mb-3" />

        <v-text-field
            :model-value="issuedCredential?.token"
            :label="isInvitation ? 'X-Enrollment-Token' : 'control_plane.token'"
            type="text"
            readonly
            variant="filled"
            class="mb-3"
        />

        <v-sheet v-if="issuedCredential" color="surface-variant" rounded="lg" border class="pa-4">
          <pre class="credential-pre">{{ credentialConfig }}</pre>
        </v-sheet>
        </template>
      </v-card-text>

      <v-card-actions class="px-6 pb-4">
        <v-spacer />
        <v-btn v-if="!isInvitation" variant="outlined" @click="copyCredential">Скопировать токен</v-btn>
        <v-btn color="primary" variant="flat" @click="issuedCredential = null">Готово</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.credential-pre {
  margin: 0;
  white-space: pre-wrap;
  font-family: 'Fira Code', monospace;
  font-size: 13px;
}
</style>
