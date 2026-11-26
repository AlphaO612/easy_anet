<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  GetSystemSettings,
  GetTelegramAlertConfig,
  PruneMetrics,
  SaveSystemSettings,
  SaveTelegramAlertConfig,
  TestTelegramAlert,
  type OperatingMode,
} from '@/api/alertsAndSites'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError, formatDateTime } from '@/utils/format'

const appMessage = useAppMessage()

// === Системные настройки (режимы, мульти-выходное ускорение, метрики и ротация) ===
const loading = ref(false)
const saving = ref(false)
const pruning = ref(false)
const updatedAt = ref<string | null>(null)

const form = reactive({
  operating_mode: 'balanced' as OperatingMode,
  multi_exit_enabled: true,
  parallel_transports: true,
  auto_failover: true,
  metrics_enabled: true,
  metrics_retention_days: 30,
  probe_interval_seconds: 30,
  sync_interval_seconds: 30,
  subscription_origin_override: '',
})

const modeOptions: { value: OperatingMode; title: string; subtitle: string; icon: string }[] = [
  {
    value: 'balanced',
    title: 'Сбалансированный',
    subtitle: 'Оптимальное распределение между задержкой и загрузкой выходных серверов.',
    icon: 'mdi-scale-balance',
  },
  {
    value: 'low_latency',
    title: 'Минимальный пинг',
    subtitle: 'Приоритет маршрутам с наименьшим RTT (для звонков, игр и интерактивной работы).',
    icon: 'mdi-flash-outline',
  },
  {
    value: 'high_throughput',
    title: 'Макс. скорость (Мульти-выход)',
    subtitle: 'Агрессивное распараллеливание потоков через несколько выходов и транспортов.',
    icon: 'mdi-speedometer',
  },
  {
    value: 'failover_only',
    title: 'Только резервирование (Failover)',
    subtitle: 'Один основной выход, автоматический переход на резерв только при отказе.',
    icon: 'mdi-shield-refresh-outline',
  },
]

// === Telegram-уведомления и маршрут проксирования ===
const tgSaving = ref(false)
const tgTesting = ref(false)
const tgHasToken = ref(false)
const tgUpdatedAt = ref<string | null>(null)
const tgRouteMode = ref<'direct' | 'proxy' | 'reverse_proxy'>('direct')

const tgForm = reactive({
  enabled: false,
  bot_token: '',
  chat_id: '',
  cooldown_minutes: 15,
  proxy_url: '',
  api_base_url: '',
})

const tgRouteHint = computed(() => {
  if (tgRouteMode.value === 'proxy') {
    return 'Запросы панели к api.telegram.org пойдут через указанный HTTP/HTTPS-прокси (например, поднятый на зарубежном exit-сервере).'
  }
  if (tgRouteMode.value === 'reverse_proxy') {
    return 'Вместо api.telegram.org панель будет отправлять запросы на ваш reverse-proxy URL (например Nginx proxy_pass https://api.telegram.org на EU exit-сервере).'
  }
  return 'Прямое соединение с https://api.telegram.org с хоста панели. Если провайдер блокирует Telegram API — выберите прокси или обратный прокси.'
})

async function loadAll() {
  loading.value = true
  try {
    const [sys, tg] = await Promise.all([GetSystemSettings(), GetTelegramAlertConfig()])
    form.operating_mode = sys.operating_mode
    form.multi_exit_enabled = sys.multi_exit_enabled
    form.parallel_transports = sys.parallel_transports
    form.auto_failover = sys.auto_failover
    form.metrics_enabled = sys.metrics_enabled
    form.metrics_retention_days = sys.metrics_retention_days
    form.probe_interval_seconds = sys.probe_interval_seconds
    form.sync_interval_seconds = sys.sync_interval_seconds
    form.subscription_origin_override = sys.subscription_origin_override
    updatedAt.value = sys.updated_at ?? null

    tgForm.enabled = tg.enabled
    tgForm.chat_id = tg.chat_id
    tgForm.cooldown_minutes = tg.cooldown_minutes
    tgForm.proxy_url = tg.proxy_url || ''
    tgForm.api_base_url = tg.api_base_url || ''
    tgForm.bot_token = ''
    tgHasToken.value = tg.has_bot_token
    tgUpdatedAt.value = tg.updated_at ?? null
    if (tgForm.api_base_url) tgRouteMode.value = 'reverse_proxy'
    else if (tgForm.proxy_url) tgRouteMode.value = 'proxy'
    else tgRouteMode.value = 'direct'
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось загрузить системные настройки'))
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  saving.value = true
  try {
    const saved = await SaveSystemSettings({
      operating_mode: form.operating_mode,
      multi_exit_enabled: form.multi_exit_enabled,
      parallel_transports: form.parallel_transports,
      auto_failover: form.auto_failover,
      metrics_enabled: form.metrics_enabled,
      metrics_retention_days: Number(form.metrics_retention_days) || 30,
      probe_interval_seconds: Number(form.probe_interval_seconds) || 30,
      sync_interval_seconds: Number(form.sync_interval_seconds) || 30,
      subscription_origin_override: form.subscription_origin_override.trim(),
    })
    updatedAt.value = saved.updated_at ?? null
    appMessage.success('Системные настройки сохранены и применены')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось сохранить системные настройки'))
  } finally {
    saving.value = false
  }
}

async function pruneNow() {
  pruning.value = true
  try {
    const res = await PruneMetrics(Number(form.metrics_retention_days) || 30)
    appMessage.success(`Ротация завершена: удалено ${res.deleted_rows} записей старше ${res.retention_days} дн.`)
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось выполнить очистку метрик'))
  } finally {
    pruning.value = false
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
      proxy_url: tgRouteMode.value === 'proxy' ? tgForm.proxy_url.trim() : '',
      api_base_url: tgRouteMode.value === 'reverse_proxy' ? tgForm.api_base_url.trim() : '',
    })
    tgHasToken.value = saved.has_bot_token
    tgUpdatedAt.value = saved.updated_at ?? null
    tgForm.bot_token = ''
    appMessage.success('Настройки Telegram-маршрутизации сохранены')
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
    appMessage.success(res.message || 'Тестовое уведомление отправлено')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Ошибка отправки в Telegram — проверьте прокси/маршрут'))
  } finally {
    tgTesting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <v-container max-width="1120" class="py-6">
    <div class="d-flex justify-space-between align-center flex-wrap ga-3 mb-5">
      <div>
        <h1 class="text-h5 font-weight-bold">Настройки системы</h1>
        <div class="text-body-2 text-medium-emphasis">
          Режимы работы и мульти-выходное ускорение, сбор и ротация метрик, маршрутизация Telegram-уведомлений.
        </div>
      </div>
      <v-btn variant="outlined" prepend-icon="mdi-refresh" :loading="loading" @click="loadAll">
        Обновить
      </v-btn>
    </div>

    <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />

    <!-- Секция 1: Режимы работы и ускорение скорости -->
    <v-card variant="outlined" class="pa-5 rounded-lg mb-5">
      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-4">
        <div>
          <h2 class="text-h6 font-weight-bold">Режим работы и мульти-выходное ускорение</h2>
          <div class="text-caption text-medium-emphasis">
            Управляет приоритетами подписок VLESS, генерацией конфигов ANet (<code>[acceleration]</code>) и использованием нескольких выходных нод и транспортов одновременно.
          </div>
        </div>
        <div class="d-flex ga-2">
          <v-btn size="small" variant="tonal" prepend-icon="mdi-scale-balance" to="/pools">
            Пулы балансировки
          </v-btn>
          <v-btn size="small" variant="tonal" prepend-icon="mdi-routes" to="/profile-topology">
            Топология потоков
          </v-btn>
        </div>
      </div>

      <v-row dense class="mb-4">
        <v-col v-for="m in modeOptions" :key="m.value" cols="12" sm="6">
          <v-card
            :variant="form.operating_mode === m.value ? 'tonal' : 'outlined'"
            :color="form.operating_mode === m.value ? 'primary' : undefined"
            class="pa-3 h-100 cursor-pointer"
            @click="form.operating_mode = m.value"
          >
            <div class="d-flex align-center ga-2 mb-1">
              <v-icon :icon="m.icon" size="20" />
              <span class="font-weight-bold text-body-2">{{ m.title }}</span>
              <v-chip v-if="form.operating_mode === m.value" size="x-small" color="primary" class="ms-auto">
                Активен
              </v-chip>
            </div>
            <div class="text-caption text-medium-emphasis">{{ m.subtitle }}</div>
          </v-card>
        </v-col>
      </v-row>

      <v-row dense>
        <v-col cols="12" md="4">
          <v-switch
            v-model="form.multi_exit_enabled"
            color="primary"
            inset
            label="Мульти-выходное ускорение"
            hint="Использовать сразу несколько выходных нод для повышения суммарной скорости"
            persistent-hint
          />
        </v-col>
        <v-col cols="12" md="4">
          <v-switch
            v-model="form.parallel_transports"
            color="primary"
            inset
            label="Параллельные транспорты"
            hint="Одновременно задействовать QUIC, WS, AHTTP и WRTC на доступных узлах"
            persistent-hint
          />
        </v-col>
        <v-col cols="12" md="4">
          <v-switch
            v-model="form.auto_failover"
            color="success"
            inset
            label="Автоматический Failover"
            hint="Исключать деградировавшие каналы и переключать трафик на здоровые выходы"
            persistent-hint
          />
        </v-col>
      </v-row>
    </v-card>

    <!-- Секция 2: Метрики, интервалы и ротация -->
    <v-card variant="outlined" class="pa-5 rounded-lg mb-5">
      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-4">
        <div>
          <h2 class="text-h6 font-weight-bold">Метрики, ротация и подписки</h2>
          <div class="text-caption text-medium-emphasis">
            Автоматическая очистка устаревших срезов трафика выполняется раз в 6 часов, либо вручную кнопкой ниже.
          </div>
        </div>
        <v-switch
          v-model="form.metrics_enabled"
          :label="form.metrics_enabled ? 'Сбор метрик включён' : 'Сбор метрик выключен'"
          color="success"
          inset
          hide-details
          density="compact"
        />
      </div>

      <v-row dense>
        <v-col cols="12" sm="4">
          <v-text-field
            v-model.number="form.metrics_retention_days"
            type="number"
            min="1"
            max="365"
            label="Хранение метрик (дней)"
            hint="Записи истории старше этого срока удаляются при ротации"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>
        <v-col cols="12" sm="4">
          <v-text-field
            v-model.number="form.probe_interval_seconds"
            type="number"
            min="10"
            max="600"
            label="Интервал сетевых проб (сек)"
            hint="Рекомендуется 30 секунд"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>
        <v-col cols="12" sm="4">
          <v-text-field
            v-model.number="form.sync_interval_seconds"
            type="number"
            min="10"
            max="300"
            label="Интервал синхронизации шлюзов (сек)"
            hint="Обновление клиентов VLESS/WG и счётчиков трафика"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>
        <v-col cols="12" class="mt-2">
          <v-text-field
            v-model="form.subscription_origin_override"
            label="Переопределение домена подписок (опционально)"
            placeholder="https://gateway1.altnight.art (пусто = автоопределение)"
            hint="Задайте альтернативный домен-зеркало (например https://g-w3.altnight.art), если нужно раздавать ссылки подписок через другой вход"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>
      </v-row>

      <div class="d-flex align-center flex-wrap ga-2 mt-4">
        <v-btn color="primary" prepend-icon="mdi-content-save" :loading="saving" @click="saveSettings">
          Сохранить настройки
        </v-btn>
        <v-btn
          variant="tonal"
          color="warning"
          prepend-icon="mdi-broom"
          :loading="pruning"
          @click="pruneNow"
        >
          Очистить метрики старше {{ form.metrics_retention_days }} дн.
        </v-btn>
        <span v-if="updatedAt" class="text-caption text-medium-emphasis ms-auto">
          Обновлено: {{ formatDateTime(updatedAt) }}
        </span>
      </div>
    </v-card>

    <!-- Секция 3: Telegram-алерты с выбором маршрута / прокси -->
    <v-card variant="outlined" class="pa-5 rounded-lg">
      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-3">
        <div>
          <h2 class="text-h6 font-weight-bold">Telegram-уведомления и маршрут проксирования</h2>
          <div class="text-caption text-medium-emphasis">
            Если входная нода с панелью находится в сети с блокировкой <code>api.telegram.org</code>, направьте запросы через прокси или зарубежный exit-сервер.
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

      <v-btn-toggle
        v-model="tgRouteMode"
        mandatory
        color="primary"
        variant="outlined"
        density="compact"
        class="mb-3"
      >
        <v-btn value="direct" prepend-icon="mdi-earth">Напрямую</v-btn>
        <v-btn value="proxy" prepend-icon="mdi-transit-connection-variant">Через HTTP-прокси</v-btn>
        <v-btn value="reverse_proxy" prepend-icon="mdi-server-network">Через свой Bot API URL</v-btn>
      </v-btn-toggle>

      <v-alert type="info" variant="tonal" density="compact" class="mb-4 text-caption">
        {{ tgRouteHint }}
      </v-alert>

      <v-row dense>
        <v-col cols="12" md="5">
          <v-text-field
            v-model="tgForm.bot_token"
            type="password"
            :label="tgHasToken ? 'Bot Token (сохранён — оставьте пустым)' : 'Bot Token от @BotFather'"
            variant="outlined"
            density="comfortable"
            placeholder="123456:ABC-DEF..."
          />
        </v-col>
        <v-col cols="12" md="4">
          <v-text-field
            v-model="tgForm.chat_id"
            label="Chat ID"
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
            label="Повтор (мин)"
            variant="outlined"
            density="comfortable"
          />
        </v-col>

        <v-col v-if="tgRouteMode === 'proxy'" cols="12">
          <v-text-field
            v-model="tgForm.proxy_url"
            label="HTTP / HTTPS Proxy URL"
            placeholder="http://user:password@78.17.180.196:3128"
            hint="Поддерживаются http:// и https:// прокси (с авторизацией или без)"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>

        <v-col v-if="tgRouteMode === 'reverse_proxy'" cols="12">
          <v-text-field
            v-model="tgForm.api_base_url"
            label="URL обратного прокси Telegram Bot API"
            placeholder="https://tg-proxy.example.com"
            hint="Панель будет обращаться к <URL>/bot<TOKEN>/sendMessage вместо https://api.telegram.org"
            persistent-hint
            variant="outlined"
            density="comfortable"
          />
        </v-col>
      </v-row>

      <div class="d-flex align-center flex-wrap ga-2 mt-3">
        <v-btn color="primary" prepend-icon="mdi-content-save" :loading="tgSaving" @click="saveTelegram">
          Сохранить Telegram
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
  </v-container>
</template>
