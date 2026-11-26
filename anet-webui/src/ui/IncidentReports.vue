<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import { GetIngressRoutes } from '@/api/devices'
import {
  GetTelegramAlertConfig,
  SaveTelegramAlertConfig,
  TestTelegramAlert,
  type TelegramExitOption,
} from '@/api/alertsAndSites'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError, formatDateTime } from '@/utils/format'
import { formatRegionBadge } from '@/utils/regions'

const { confirmAction } = useConfirmDialog()
const appMessage = useAppMessage()

// === Telegram-уведомления ===
const tgLoading = ref(false)
const tgSaving = ref(false)
const tgTesting = ref(false)
const tgHasToken = ref(false)
const tgAvailableExits = ref<TelegramExitOption[]>([])
const tgUpdatedAt = ref<string | null>(null)
const tgForm = reactive({
  enabled: false,
  bot_token: '',
  chat_id: '',
  cooldown_minutes: 15,
  exit_route: 'auto',
  proxy_url: '',
  api_base_url: '',
})

const tgExitOptions = computed(() => [
  { value: 'auto', title: '🌐 Авто — лучший живой выходной сервер' },
  ...tgAvailableExits.value.map((ex) => ({
    value: ex.exit_host,
    title: `${ex.region ? `${formatRegionBadge(ex.region)} ` : ''}${ex.label}${ex.latency_ms != null ? ` · ${Math.round(ex.latency_ms)} мс` : ''}`,
  })),
  { value: 'direct', title: '⚡ Напрямую (без туннеля)' },
])

async function loadTelegram() {
  tgLoading.value = true
  try {
    const cfg = await GetTelegramAlertConfig()
    tgForm.enabled = cfg.enabled
    tgForm.chat_id = cfg.chat_id
    tgForm.cooldown_minutes = cfg.cooldown_minutes
    tgForm.exit_route = cfg.exit_route || 'auto'
    tgForm.proxy_url = cfg.proxy_url || ''
    tgForm.api_base_url = cfg.api_base_url || ''
    tgForm.bot_token = ''
    tgHasToken.value = cfg.has_bot_token
    tgAvailableExits.value = cfg.available_exits || []
    tgUpdatedAt.value = cfg.updated_at ?? null
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось загрузить настройки Telegram'))
  } finally {
    tgLoading.value = false
  }
}

async function saveTelegram() {
  tgSaving.value = true
  try {
    const saved = await SaveTelegramAlertConfig({
      enabled: tgForm.enabled,
      bot_token: tgForm.bot_token.trim() || null,
      chat_id: tgForm.chat_id.trim(),
      cooldown_minutes: Number(tgForm.cooldown_minutes) || 15,
      exit_route: tgForm.exit_route || 'auto',
      proxy_url: tgForm.proxy_url.trim(),
      api_base_url: tgForm.api_base_url.trim(),
    })
    tgHasToken.value = saved.has_bot_token
    tgAvailableExits.value = saved.available_exits || []
    tgUpdatedAt.value = saved.updated_at ?? null
    tgForm.bot_token = ''
    appMessage.success('Настройки Telegram-уведомлений сохранены')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось сохранить настройки Telegram'))
  } finally {
    tgSaving.value = false
  }
}

async function testTelegram() {
  tgTesting.value = true
  try {
    const res = await TestTelegramAlert()
    appMessage.success(res.message || 'Тестовое сообщение отправлено в Telegram')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Ошибка отправки тестового сообщения'))
  } finally {
    tgTesting.value = false
  }
}

// === Ручные сообщения об инцидентах ===
const blank = () => ({ observer: '', direction: '', transport: '', evidence: '' })
type Report = { id: string; operator_id: string; reported_at: string; observation: ReturnType<typeof blank> }

const form = reactive(blank())
const rows = ref<Report[]>([])
const routeSuggestions = ref<{ direction: string; transport: string }[]>([])
const directionItems = computed(() => routeSuggestions.value.map(r => r.direction))
const transportItems = ['VLESS', 'WIREGUARD', 'QUIC', 'WS', 'AHTTP', 'WRTC']
const message = ref('')

function onDirectionPicked(val: string) {
  const found = routeSuggestions.value.find(r => r.direction === val)
  if (found && !form.transport.trim()) {
    form.transport = found.transport
  }
}
const busy = ref(false)
const deletingId = ref<string | null>(null)
const ready = computed(() => Object.values(form).every(v => v.trim()))

async function load() {
  try {
    rows.value = await api<Report[]>('/operator-incidents')
  } catch (err) {
    message.value = 'Не удалось загрузить сообщения.'
    appMessage.error(extractApiError(err, 'Не удалось загрузить сообщения об инцидентах'))
  }
}

async function submit() {
  busy.value = true
  try {
    const row = await api<Report>('/operator-incidents', { method: 'POST', data: { ...form } })
    rows.value = [row, ...rows.value].slice(0, 200)
    Object.assign(form, blank())
    message.value = 'Сообщение сохранено.'
    appMessage.success('Сообщение об инциденте сохранено')
  } catch (err) {
    message.value = 'Не удалось сохранить сообщение.'
    appMessage.error(extractApiError(err, 'Не удалось сохранить сообщение'))
  } finally {
    busy.value = false
  }
}

async function removeReport(report: Report) {
  const accepted = await confirmAction({
    title: 'Удалить сообщение об инциденте?',
    message: `Удалить запись от ${formatDateTime(report.reported_at)} (${report.observation.direction || report.id})?`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return
  deletingId.value = report.id
  try {
    await api('/operator-incidents/' + encodeURIComponent(report.id), { method: 'DELETE' })
    rows.value = rows.value.filter(r => r.id !== report.id)
    appMessage.success('Сообщение об инциденте удалено')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось удалить сообщение'))
  } finally {
    deletingId.value = null
  }
}

onMounted(() => {
  load()
  loadTelegram()
  void GetIngressRoutes().then((res) => {
    routeSuggestions.value = (res.routes || []).map((r) => ({
      direction: `${r.entry_host}:${r.entry_port} → ${r.exit_host} (${r.id})`,
      transport: r.protocol.toUpperCase(),
    }))
  }).catch(() => {})
})
</script>

<template>
  <v-container>
    <!-- Карточка Telegram-алертов -->
    <v-card variant="outlined" class="mb-6 pa-5 rounded-lg">
      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-3">
        <div>
          <h2 class="text-h6 font-weight-bold">Уведомления Telegram</h2>
          <div class="text-caption text-medium-emphasis">
            Автоматические алерты о падении шлюзов, обрыве аплинков, потерях ≥ 50% и восстановлении (проверка каждую минуту).
          </div>
        </div>
        <v-switch
          v-model="tgForm.enabled"
          :label="tgForm.enabled ? 'Включены' : 'Выключены'"
          color="success"
          inset
          hide-details
          density="compact"
        />
      </div>

      <v-row dense>
        <v-col cols="12" md="5">
          <v-text-field
            v-model="tgForm.bot_token"
            type="password"
            :label="tgHasToken ? 'Bot Token (сохранён — оставьте пустым, чтобы не менять)' : 'Bot Token от @BotFather'"
            variant="outlined"
            density="comfortable"
            placeholder="123456:ABC-DEF..."
          />
        </v-col>
        <v-col cols="12" md="4">
          <v-text-field
            v-model="tgForm.chat_id"
            label="Chat ID (пользователь или группа)"
            variant="outlined"
            density="comfortable"
            placeholder="-1001234567890"
          />
        </v-col>
        <v-col cols="12" md="3">
          <v-text-field
            v-model.number="tgForm.cooldown_minutes"
            type="number"
            min="1"
            max="1440"
            label="Интервал повтора (мин)"
            variant="outlined"
            density="comfortable"
          />
        </v-col>
        <v-col cols="12">
          <v-select
            v-model="tgForm.exit_route"
            :items="tgExitOptions"
            item-title="title"
            item-value="value"
            label="Через какой выходной сервер отправлять уведомления Telegram"
            variant="outlined"
            density="comfortable"
            prepend-inner-icon="mdi-routes"
          />
        </v-col>
      </v-row>

      <div class="d-flex align-center flex-wrap ga-2 mt-2">
        <v-btn color="primary" prepend-icon="mdi-content-save" :loading="tgSaving" @click="saveTelegram">
          Сохранить
        </v-btn>
        <v-btn
          variant="tonal"
          color="info"
          prepend-icon="mdi-send"
          :loading="tgTesting"
          :disabled="!tgHasToken || !tgForm.chat_id.trim()"
          @click="testTelegram"
        >
          Отправить тест
        </v-btn>
        <span v-if="tgUpdatedAt" class="text-caption text-medium-emphasis ms-auto">
          Обновлено: {{ formatDateTime(tgUpdatedAt) }}
        </span>
      </div>
    </v-card>

    <h1 class="text-h5 font-weight-bold mb-3">Ручные сообщения об инцидентах</h1>
    <v-form @submit.prevent="submit">
      <v-text-field v-model="form.observer" label="Точка наблюдения" maxlength="256" />
      <v-combobox
        v-model="form.direction"
        :items="directionItems"
        label="Направление (выберите маршрут из списка или введите свой)"
        @update:model-value="onDirectionPicked"
      />
      <v-combobox v-model="form.transport" :items="transportItems" label="Транспорт" />
      <v-textarea v-model="form.evidence" label="Наблюдаемые признаки и результат внешней проверки" maxlength="8192" />
      <v-btn type="submit" :disabled="busy || !ready" :loading="busy">Записать сообщение</v-btn>
    </v-form>
    <p role="status">{{ message }}</p>
    <v-table>
      <thead>
        <tr>
          <th>Время</th>
          <th>Оператор</th>
          <th>Наблюдатель</th>
          <th>Направление</th>
          <th>Транспорт</th>
          <th>Наблюдение</th>
          <th class="text-center">Действия</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ formatDateTime(r.reported_at) }}</td>
          <td>{{ r.operator_id }}</td>
          <td>{{ r.observation.observer }}</td>
          <td>{{ r.observation.direction }}</td>
          <td>{{ r.observation.transport }}</td>
          <td>{{ r.observation.evidence }}</td>
          <td class="text-center">
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="Удалить сообщение"
              :loading="deletingId === r.id"
              @click="removeReport(r)"
            />
          </td>
        </tr>
      </tbody>
    </v-table>
  </v-container>
</template>
