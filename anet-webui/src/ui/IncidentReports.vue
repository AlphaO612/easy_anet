<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import {
  GetTelegramAlertConfig,
  SaveTelegramAlertConfig,
  TestTelegramAlert,
} from '@/api/alertsAndSites'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError, formatDateTime } from '@/utils/format'

const { confirmAction } = useConfirmDialog()
const appMessage = useAppMessage()

// === Telegram-уведомления ===
const tgLoading = ref(false)
const tgSaving = ref(false)
const tgTesting = ref(false)
const tgHasToken = ref(false)
const tgUpdatedAt = ref<string | null>(null)
const tgForm = reactive({
  enabled: false,
  bot_token: '',
  chat_id: '',
  cooldown_minutes: 15,
  proxy_url: '',
  api_base_url: '',
})

async function loadTelegram() {
  tgLoading.value = true
  try {
    const cfg = await GetTelegramAlertConfig()
    tgForm.enabled = cfg.enabled
    tgForm.chat_id = cfg.chat_id
    tgForm.cooldown_minutes = cfg.cooldown_minutes
    tgForm.proxy_url = cfg.proxy_url || ''
    tgForm.api_base_url = cfg.api_base_url || ''
    tgForm.bot_token = ''
    tgHasToken.value = cfg.has_bot_token
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
      proxy_url: tgForm.proxy_url.trim(),
      api_base_url: tgForm.api_base_url.trim(),
    })
    tgHasToken.value = saved.has_bot_token
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
const message = ref('')
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
        <v-col cols="12" md="6">
          <v-text-field
            v-model="tgForm.proxy_url"
            label="HTTP/HTTPS прокси (опционально, если Telegram недоступен напрямую)"
            placeholder="http://user:pass@exit-vps:3128"
            variant="outlined"
            density="comfortable"
          />
        </v-col>
        <v-col cols="12" md="6">
          <v-text-field
            v-model="tgForm.api_base_url"
            label="Альтернативный Bot API URL (reverse proxy на exit-ноде)"
            placeholder="https://tg-relay.example.com"
            variant="outlined"
            density="comfortable"
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
      <v-text-field v-model="form.direction" label="Направление (вход → выход)" maxlength="512" />
      <v-text-field v-model="form.transport" label="Транспорт" maxlength="128" />
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
