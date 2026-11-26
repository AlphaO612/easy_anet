<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { GetGroups } from '@/api/groups'
import { GetUserConfig, UpdateUser } from '@/api/users'
import {
  CreateUserDevice,
  CreateUserSubscription,
  DeleteUserDevice,
  GetIngressRoutes,
  GetUserDevices,
  RevokeUserDevice,
  RevokeUserSubscription,
} from '@/api/devices'
import type {
  DeviceProtocol,
  IngressRoute,
  UserDevice,
  UserDevicesResponse,
} from '@/models/device'
import type { UserGroup } from '@/models/group'
import type { RateReqRequest } from '@/models/rate'
import { useAppMessage } from '@/composables/useAppMessage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useRate } from '@/composables/useRate'
import { useUser } from '@/composables/useUser'
import {
  copyText,
  downloadTextFile,
  extractApiError,
  formatBytes,
  formatDateTime,
} from '@/utils/format'
import QrCodeBox from '@/components/QrCodeBox.vue'
import RateCreateForm from '@/components/RateCreateForm.vue'
import RateEditForm from '@/components/RateEditForm.vue'
import UserForm from '@/components/UserForm.vue'

const route = useRoute()
const router = useRouter()
const message = useAppMessage()
const { confirmAction } = useConfirmDialog()

const {
  user,
  loading: userLoading,
  saving: savingUser,
  regenerating,
  loadUser,
  saveUser,
  regenerate,
  removeUser,
  resetTraffic,
} = useUser()
const { saving: savingRate, saveRate, createRate } = useRate(user)

const userId = computed(() => String(route.params.id ?? ''))

const pageError = ref('')
const devicesError = ref('')
const deviceFormError = ref('')
const subscriptionError = ref('')
const configError = ref('')

const devicesLoading = ref(false)
const togglingStatus = ref(false)
const resettingTraffic = ref(false)
const deletingUser = ref(false)
const creatingDevice = ref(false)
const subscriptionBusy = ref(false)
const deviceActionId = ref('')

const devicesData = ref<UserDevicesResponse | null>(null)
const ingressRoutes = ref<IngressRoute[]>([])
const ingressOrigin = ref('')
const groups = ref<UserGroup[]>([])

const subscriptionUrl = ref('')
const userFormValid = ref(true)

const clientToml = ref('')
const configLoading = ref(false)
const showClientQr = ref(false)

const qrDialog = ref({
  open: false,
  value: '',
  title: '',
  subtitle: '',
  filename: 'config.txt',
})

const protocolOptions: { title: string; value: DeviceProtocol }[] = [
  { title: 'VLESS', value: 'vless' },
  { title: 'WireGuard', value: 'wireguard' },
  { title: 'ANet', value: 'anet' },
]

const newDeviceProtocol = ref<DeviceProtocol>('vless')
const newDeviceRouteId = ref('')
const newDeviceName = ref('')

const devices = computed(() => devicesData.value?.devices ?? [])
const subscriptionActive = computed(() => Boolean(devicesData.value?.subscription_active))
const subscriptionOrigin = computed(
  () => devicesData.value?.subscription_origin?.trim() || ingressOrigin.value.trim() || '',
)

const userGroup = computed(() =>
  user.value?.group_id ? groups.value.find((g) => g.id === user.value?.group_id) ?? null : null,
)

const filteredRoutes = computed(() =>
  ingressRoutes.value.filter((r) => r.protocol === newDeviceProtocol.value),
)

const routeSelectItems = computed(() =>
  filteredRoutes.value.map((r) => ({
    title: `${r.name} · ${r.entry_host}:${r.entry_port} → ${r.exit_host}`,
    value: r.id,
  })),
)

const canCreateDevice = computed(() => {
  if (!user.value || !user.value.is_active || creatingDevice.value) return false
  if (!newDeviceName.value.trim()) return false
  if (newDeviceProtocol.value !== 'anet' && !newDeviceRouteId.value) return false
  return true
})

watch(
  filteredRoutes,
  (routes) => {
    if (!routes.some((r) => r.id === newDeviceRouteId.value)) {
      newDeviceRouteId.value = routes[0]?.id ?? ''
    }
  },
  { immediate: true },
)

const sanitizeFilePart = (raw: string, fallback: string) =>
  raw.trim().replace(/[^a-zA-Z0-9._-]+/g, '_') || fallback

const deviceExtension = (protocol: DeviceProtocol) => {
  if (protocol === 'vless') return '.uri'
  if (protocol === 'wireguard') return '.conf'
  return '.toml'
}

const devicePayload = (device: UserDevice) =>
  device.config_uri?.trim() || device.config_text?.trim() || ''

const deviceFilename = (device: UserDevice) => {
  const base = sanitizeFilePart(device.name || user.value?.uid || device.protocol, device.protocol)
  return `${base}${deviceExtension(device.protocol)}`
}

const protocolLabel = (protocol: DeviceProtocol) => {
  if (protocol === 'vless') return 'VLESS'
  if (protocol === 'wireguard') return 'WireGuard'
  return 'ANet'
}

const protocolColor = (protocol: DeviceProtocol) => {
  if (protocol === 'vless') return 'primary'
  if (protocol === 'wireguard') return 'info'
  return 'secondary'
}

const formatRouteLabel = (device: UserDevice) => {
  const entry = device.entry_host
    ? device.entry_port
      ? `${device.entry_host}:${device.entry_port}`
      : device.entry_host
    : ''
  if (entry && device.exit_host) return `${entry} → ${device.exit_host}`
  if (entry) return entry
  if (device.exit_host) return device.exit_host
  return '—'
}

const openQrModal = (payload: { value: string; title: string; subtitle: string; filename: string }) => {
  qrDialog.value = { open: true, ...payload }
}

const loadDevices = async (id = userId.value) => {
  if (!id) return
  devicesLoading.value = true
  devicesError.value = ''
  try {
    devicesData.value = await GetUserDevices(id)
  } catch (e: unknown) {
    devicesError.value = extractApiError(e, 'Не удалось загрузить устройства пользователя')
  } finally {
    devicesLoading.value = false
  }
}

const loadAll = async (id: string) => {
  if (!id) return
  pageError.value = ''
  devicesError.value = ''
  deviceFormError.value = ''
  subscriptionError.value = ''
  configError.value = ''
  subscriptionUrl.value = ''
  clientToml.value = ''
  showClientQr.value = false

  const [userRes, devicesRes, routesRes, groupsRes] = await Promise.allSettled([
    loadUser(id),
    GetUserDevices(id),
    GetIngressRoutes(),
    GetGroups(),
  ])

  if (userRes.status === 'rejected') {
    pageError.value = extractApiError(userRes.reason, 'Не удалось загрузить профиль пользователя')
  }
  if (devicesRes.status === 'fulfilled') {
    devicesData.value = devicesRes.value
  } else {
    devicesError.value = extractApiError(devicesRes.reason, 'Не удалось загрузить устройства')
  }
  if (routesRes.status === 'fulfilled') {
    ingressRoutes.value = routesRes.value.routes ?? []
    ingressOrigin.value = routesRes.value.origin ?? ''
  }
  if (groupsRes.status === 'fulfilled') {
    groups.value = groupsRes.value
  }
}

watch(
  userId,
  (id) => {
    void loadAll(id)
  },
  { immediate: true },
)

const copyFingerprint = async () => {
  if (!user.value?.fingerprint) return
  const ok = await copyText(user.value.fingerprint)
  if (ok) message.success('Fingerprint скопирован')
  else message.error('Не удалось скопировать fingerprint')
}

const handleToggleActive = async (nextValue: unknown) => {
  if (!user.value || togglingStatus.value) return
  const target = Boolean(nextValue)
  const prev = user.value.is_active
  user.value.is_active = target
  togglingStatus.value = true
  try {
    const trimmedIp = user.value.static_ip?.trim() || ''
    await UpdateUser(user.value.id, {
      uid: user.value.uid.trim(),
      is_active: target,
      static_ip: trimmedIp || undefined,
      clear_static_ip: !trimmedIp,
      server_ids: user.value.server_ids || [],
      pool_ids: user.value.pool_ids || [],
      route_map_id: user.value.route_map_id || undefined,
      clear_route_map: !user.value.route_map_id,
      group_id: user.value.group_id || undefined,
      clear_group: !user.value.group_id,
    })
    message.success(target ? 'Пользователь разблокирован' : 'Пользователь забанен')
  } catch (e: unknown) {
    user.value.is_active = prev
    message.error(extractApiError(e, 'Не удалось обновить статус пользователя'))
  } finally {
    togglingStatus.value = false
  }
}

const handleRegenerateKeys = async () => {
  if (!user.value) return
  const accepted = await confirmAction({
    title: 'Перегенерировать ключи ANet?',
    message: `Старые ключи ANet пользователя «${user.value.uid}» перестанут работать. Клиенту потребуется новый client.toml.`,
    confirmText: 'Перегенерировать',
    confirmColor: 'warning',
  })
  if (!accepted) return
  try {
    await regenerate()
    clientToml.value = ''
    showClientQr.value = false
    message.success('Ключи ANet перегенерированы')
    await loadDevices()
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось перегенерировать ключи ANet'))
  }
}

const handleResetTraffic = async () => {
  if (!user.value) return
  const accepted = await confirmAction({
    title: 'Сбросить трафик пользователя?',
    message: `Счётчики трафика пользователя «${user.value.uid}» будут обнулены.`,
    confirmText: 'Сбросить трафик',
    confirmColor: 'warning',
  })
  if (!accepted) return
  resettingTraffic.value = true
  try {
    await resetTraffic()
    message.success('Трафик пользователя сброшен')
    await loadDevices()
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось сбросить трафик'))
  } finally {
    resettingTraffic.value = false
  }
}

const handleDeleteUser = async () => {
  if (!user.value) return
  const accepted = await confirmAction({
    title: 'Удалить пользователя?',
    message: `Пользователь «${user.value.uid}» и все связанные конфиги будут удалены без возможности восстановления.`,
    confirmText: 'Удалить пользователя',
    confirmColor: 'error',
  })
  if (!accepted) return
  deletingUser.value = true
  try {
    await removeUser()
    message.success(`Пользователь «${user.value.uid}» удалён`)
    await router.push('/users')
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось удалить пользователя'))
  } finally {
    deletingUser.value = false
  }
}

const handleCreateSubscription = async () => {
  if (!user.value || subscriptionBusy.value) return
  if (subscriptionActive.value) {
    const accepted = await confirmAction({
      title: 'Обновить ссылку подписки?',
      message:
        'Прежняя ссылка подписки перестанет обновлять конфиги на устройствах. Новую ссылку нужно будет передать пользователю.',
      confirmText: 'Обновить ссылку',
      confirmColor: 'warning',
    })
    if (!accepted) return
  }

  subscriptionBusy.value = true
  subscriptionError.value = ''
  try {
    const res = await CreateUserSubscription(user.value.id)
    const origin = subscriptionOrigin.value.replace(/\/+$/, '')
    subscriptionUrl.value = res.url || (origin && res.path ? `${origin}${res.path}` : res.path)
    if (devicesData.value) {
      devicesData.value.subscription_active = true
    } else {
      await loadDevices()
    }
    message.success('Ссылка подписки создана. Сохраните или передайте её пользователю.')
  } catch (e: unknown) {
    subscriptionError.value = extractApiError(e, 'Не удалось создать ссылку подписки')
  } finally {
    subscriptionBusy.value = false
  }
}

const handleCopySubscription = async () => {
  if (!subscriptionUrl.value) return
  const ok = await copyText(subscriptionUrl.value)
  if (ok) message.success('Ссылка подписки скопирована')
  else message.error('Не удалось скопировать ссылку подписки')
}

const handleRevokeSubscription = async () => {
  if (!user.value || subscriptionBusy.value) return
  const accepted = await confirmAction({
    title: 'Отозвать ссылку подписки?',
    message:
      'Отзыв ссылки прекращает получение обновлений подписки, но не блокирует уже импортированные устройства. Чтобы заблокировать доступ на входе, отзовите конкретное устройство в таблице ниже.',
    confirmText: 'Отозвать ссылку',
    confirmColor: 'warning',
  })
  if (!accepted) return

  subscriptionBusy.value = true
  subscriptionError.value = ''
  try {
    await RevokeUserSubscription(user.value.id)
    subscriptionUrl.value = ''
    if (devicesData.value) {
      devicesData.value.subscription_active = false
    }
    message.success('Ссылка подписки отозвана')
  } catch (e: unknown) {
    subscriptionError.value = extractApiError(e, 'Не удалось отозвать ссылку подписки')
  } finally {
    subscriptionBusy.value = false
  }
}

const handleCreateDevice = async () => {
  if (!user.value || !canCreateDevice.value) return
  creatingDevice.value = true
  deviceFormError.value = ''
  try {
    await CreateUserDevice(user.value.id, {
      protocol: newDeviceProtocol.value,
      name: newDeviceName.value.trim(),
      route_id: newDeviceRouteId.value || undefined,
    })
    newDeviceName.value = ''
    message.success('Конфигурация устройства создана')
    await loadDevices()
  } catch (e: unknown) {
    deviceFormError.value = extractApiError(e, 'Не удалось создать конфиг устройства')
  } finally {
    creatingDevice.value = false
  }
}

const handleCopyDevice = async (device: UserDevice) => {
  const payload = devicePayload(device)
  if (!payload) {
    message.warning('Конфигурация для этого устройства недоступна')
    return
  }
  const ok = await copyText(payload)
  if (ok) message.success(`Конфиг «${device.name}» скопирован`)
  else message.error('Не удалось скопировать конфиг')
}

const handleDownloadDevice = (device: UserDevice) => {
  const payload = devicePayload(device)
  if (!payload) {
    message.warning('Конфигурация для этого устройства недоступна')
    return
  }
  const filename = deviceFilename(device)
  downloadTextFile(filename, payload)
  message.success(`Файл ${filename} скачан`)
}

const handleShowDeviceQr = (device: UserDevice) => {
  const payload = devicePayload(device)
  if (!payload) {
    message.warning('Конфигурация для этого устройства недоступна')
    return
  }
  openQrModal({
    value: payload,
    title: `${protocolLabel(device.protocol)} · ${device.name}`,
    subtitle: formatRouteLabel(device),
    filename: deviceFilename(device),
  })
}

const handleRevokeDevice = async (device: UserDevice) => {
  if (!user.value || deviceActionId.value) return
  const accepted = await confirmAction({
    title: 'Отозвать устройство?',
    message: `Доступ устройства «${device.name}» (${protocolLabel(device.protocol)}) на входном узле будет заблокирован.`,
    confirmText: 'Отозвать',
    confirmColor: 'warning',
  })
  if (!accepted) return
  deviceActionId.value = device.id
  try {
    await RevokeUserDevice(user.value.id, device.id)
    message.success(`Устройство «${device.name}» отозвано`)
    await loadDevices()
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось отозвать устройство'))
  } finally {
    deviceActionId.value = ''
  }
}

const handleDeleteDevice = async (device: UserDevice) => {
  if (!user.value || deviceActionId.value) return
  const accepted = await confirmAction({
    title: 'Удалить устройство?',
    message: `Запись устройства «${device.name}» будет удалена без возможности восстановления.`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return
  deviceActionId.value = device.id
  try {
    await DeleteUserDevice(user.value.id, device.id)
    message.success(`Устройство «${device.name}» удалено`)
    await loadDevices()
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось удалить устройство'))
  } finally {
    deviceActionId.value = ''
  }
}

const handleSaveUser = async () => {
  if (!user.value || !userFormValid.value) return
  try {
    await saveUser()
    message.success('Настройки пользователя сохранены')
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось сохранить настройки пользователя'))
  }
}

const handleSaveRate = async (payload: RateReqRequest) => {
  try {
    await saveRate(payload)
    message.success('Тариф обновлён')
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось обновить тариф'))
  }
}

const handleCreateRate = async (payload: RateReqRequest) => {
  try {
    await createRate(payload)
    message.success('Тариф назначен')
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось назначить тариф'))
  }
}

const fetchClientToml = async (): Promise<string | null> => {
  if (!user.value) return null
  if (clientToml.value) return clientToml.value
  configLoading.value = true
  configError.value = ''
  try {
    clientToml.value = await GetUserConfig(user.value.id)
    return clientToml.value
  } catch (e: unknown) {
    configError.value = extractApiError(e, 'Не удалось загрузить client.toml')
    return null
  } finally {
    configLoading.value = false
  }
}

const handleDownloadClientToml = async () => {
  const content = await fetchClientToml()
  if (!content || !user.value) return
  const filename = `${sanitizeFilePart(user.value.uid, 'client')}.toml`
  downloadTextFile(filename, content, 'application/toml;charset=utf-8')
  message.success(`Файл ${filename} скачан`)
}

const handleToggleClientQr = async () => {
  if (showClientQr.value) {
    showClientQr.value = false
    return
  }
  const content = await fetchClientToml()
  if (content) showClientQr.value = true
}
</script>

<template>
  <v-container fluid class="user-detail-page">
    <div class="mb-3">
      <v-btn variant="text" prepend-icon="mdi-arrow-left" to="/users">
        К списку пользователей
      </v-btn>
    </div>

    <div v-if="userLoading && !user" class="d-flex justify-center py-12">
      <v-progress-circular indeterminate color="primary" size="40" />
    </div>

    <v-alert v-else-if="pageError && !user" type="error" variant="tonal" class="mb-4">
      <div class="d-flex align-center justify-space-between flex-wrap ga-2">
        <span>{{ pageError }}</span>
        <v-btn variant="outlined" size="small" @click="loadAll(userId)">Повторить</v-btn>
      </div>
    </v-alert>

    <template v-else-if="user">
      <!-- Header Card -->
      <v-card variant="outlined" class="mb-5 pa-5">
        <div class="d-flex justify-space-between align-start flex-wrap ga-4">
          <div>
            <div class="d-flex align-center flex-wrap ga-2 mb-2">
              <h1 class="text-h5 font-weight-bold">{{ user.uid }}</h1>
              <v-chip
                size="small"
                variant="tonal"
                :color="userGroup ? 'secondary' : 'default'"
                prepend-icon="mdi-account-group-outline"
              >
                {{ userGroup ? userGroup.name : 'Без группы' }}
              </v-chip>
              <v-chip size="small" :color="user.is_active ? 'success' : 'error'">
                {{ user.is_active ? 'Активен' : 'Забанен' }}
              </v-chip>
            </div>

            <div class="d-flex align-center flex-wrap ga-3 text-caption text-medium-emphasis mb-2">
              <span>ID: <code class="mono">{{ user.id }}</code></span>
              <span>Создан: {{ formatDateTime(user.created_at) }}</span>
            </div>

            <div class="d-flex align-center flex-wrap ga-2">
              <span class="text-caption text-medium-emphasis">Fingerprint:</span>
              <code class="mono text-caption fingerprint-chip">{{ user.fingerprint || '—' }}</code>
              <v-btn
                v-if="user.fingerprint"
                icon="mdi-content-copy"
                size="x-small"
                variant="text"
                title="Скопировать fingerprint"
                @click="copyFingerprint"
              />
            </div>
          </div>

          <div class="d-flex flex-column align-md-end ga-3">
            <v-switch
              :model-value="user.is_active"
              :label="user.is_active ? 'Доступ активен' : 'Пользователь забанен'"
              color="success"
              inset
              density="compact"
              hide-details
              :loading="togglingStatus"
              :disabled="togglingStatus"
              @update:model-value="handleToggleActive"
            />

            <div class="d-flex flex-wrap ga-2">
              <v-btn
                color="warning"
                variant="tonal"
                size="small"
                prepend-icon="mdi-key-change"
                :loading="regenerating"
                @click="handleRegenerateKeys"
              >
                Перегенерировать ключи ANet
              </v-btn>
              <v-btn
                color="secondary"
                variant="tonal"
                size="small"
                prepend-icon="mdi-counter"
                :loading="resettingTraffic"
                @click="handleResetTraffic"
              >
                Сбросить трафик
              </v-btn>
              <v-btn
                color="error"
                variant="tonal"
                size="small"
                prepend-icon="mdi-delete-outline"
                :loading="deletingUser"
                @click="handleDeleteUser"
              >
                Удалить пользователя
              </v-btn>
            </div>
          </div>
        </div>
      </v-card>

      <!-- Subscription Card -->
      <v-card variant="outlined" class="mb-5 pa-5">
        <div class="d-flex justify-space-between align-center flex-wrap ga-3 mb-3">
          <div class="d-flex align-center ga-2">
            <h2 class="text-h6 font-weight-bold">Подписка</h2>
            <v-chip
              size="small"
              :color="subscriptionActive ? 'success' : 'default'"
              :variant="subscriptionActive ? 'flat' : 'tonal'"
            >
              {{ subscriptionActive ? 'Активна' : 'Не создана' }}
            </v-chip>
          </div>

          <div class="d-flex flex-wrap ga-2">
            <v-btn
              color="primary"
              variant="flat"
              size="small"
              prepend-icon="mdi-link-plus"
              :loading="subscriptionBusy"
              :disabled="!user.is_active"
              @click="handleCreateSubscription"
            >
              Создать/обновить подписку
            </v-btn>
            <v-btn
              variant="tonal"
              size="small"
              prepend-icon="mdi-content-copy"
              :disabled="!subscriptionUrl"
              @click="handleCopySubscription"
            >
              Копировать URL
            </v-btn>
            <v-btn
              variant="tonal"
              size="small"
              prepend-icon="mdi-qrcode"
              :disabled="!subscriptionUrl"
              @click="
                openQrModal({
                  value: subscriptionUrl,
                  title: `Подписка · ${user.uid}`,
                  subtitle: 'Локальный QR-код ссылки подписки',
                  filename: `${sanitizeFilePart(user.uid, 'user')}-subscription.txt`,
                })
              "
            >
              QR-код
            </v-btn>
            <v-btn
              v-if="subscriptionActive"
              color="warning"
              variant="text"
              size="small"
              prepend-icon="mdi-link-off"
              :loading="subscriptionBusy"
              @click="handleRevokeSubscription"
            >
              Отозвать ссылку
            </v-btn>
          </div>
        </div>

        <v-alert
          v-if="subscriptionError"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-3"
          closable
          @click:close="subscriptionError = ''"
        >
          {{ subscriptionError }}
        </v-alert>

        <div v-if="subscriptionOrigin" class="text-body-2 mb-3">
          Адрес выдачи подписок: <code class="mono">{{ subscriptionOrigin }}</code>
        </div>
        <v-alert v-else type="info" variant="tonal" density="compact" class="mb-3">
          Публичный адрес подписок не настроен. В 4-адресной схеме различаются: 1) HTTPS-домен панели/подписок,
          2) адрес входа (Entry), 3) адрес выхода (Exit), 4) туннельный адрес клиента. Настройте публичный HTTPS-домен
          панели, чтобы получать полные ссылки подписок.
        </v-alert>

        <v-text-field
          v-if="subscriptionUrl"
          :model-value="subscriptionUrl"
          label="Текущая ссылка подписки (показывается один раз после выпуска)"
          variant="outlined"
          density="compact"
          readonly
          hide-details
          class="mb-3 mono-field"
        />
        <div v-else-if="subscriptionActive" class="text-caption text-medium-emphasis mb-2">
          Подписка активна. Для показа новой ссылки нажмите «Создать/обновить подписку» (прежняя ссылка станет недействительной).
        </div>

        <div class="text-caption text-medium-emphasis">
          Отзыв ссылки подписки только останавливает обновления списка конфигов в приложении; чтобы заблокировать доступ
          на входном узле, отзовите нужное устройство в таблице ниже.
        </div>
      </v-card>

      <!-- Devices Card -->
      <v-card variant="outlined" class="mb-5 pa-5">
        <div class="d-flex justify-space-between align-center flex-wrap ga-3 mb-4">
          <div>
            <h2 class="text-h6 font-weight-bold">Устройства и конфиги (VLESS / WireGuard / ANet)</h2>
            <div class="text-caption text-medium-emphasis">
              Выпуск, синхронизация и учёт трафика по отдельным устройствам пользователя
            </div>
          </div>
          <v-btn
            variant="tonal"
            size="small"
            prepend-icon="mdi-refresh"
            :loading="devicesLoading"
            @click="loadDevices()"
          >
            Обновить
          </v-btn>
        </div>

        <v-alert
          v-if="!user.is_active"
          type="warning"
          variant="tonal"
          density="compact"
          class="mb-4"
        >
          Пользователь забанен — создание новых конфигов приостановлено.
        </v-alert>

        <v-alert
          v-if="deviceFormError"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-4"
          closable
          @click:close="deviceFormError = ''"
        >
          {{ deviceFormError }}
        </v-alert>

        <v-form class="mb-4" @submit.prevent="handleCreateDevice">
          <v-row dense align="center">
            <v-col cols="12" sm="6" md="2">
              <v-select
                v-model="newDeviceProtocol"
                :items="protocolOptions"
                item-title="title"
                item-value="value"
                label="Протокол"
                variant="outlined"
                density="compact"
                hide-details
              />
            </v-col>
            <v-col cols="12" sm="6" md="5">
              <v-select
                v-model="newDeviceRouteId"
                :items="routeSelectItems"
                item-title="title"
                item-value="value"
                label="Маршрут"
                :placeholder="
                  filteredRoutes.length
                    ? 'Выберите маршрут'
                    : newDeviceProtocol === 'anet'
                      ? 'По умолчанию (ANet)'
                      : 'Нет настроенных маршрутов'
                "
                :clearable="newDeviceProtocol === 'anet'"
                variant="outlined"
                density="compact"
                hide-details
              />
            </v-col>
            <v-col cols="12" sm="8" md="3">
              <v-text-field
                v-model="newDeviceName"
                label="Название устройства"
                placeholder="Например, iPhone или Ноутбук"
                maxlength="80"
                variant="outlined"
                density="compact"
                hide-details
              />
            </v-col>
            <v-col cols="12" sm="4" md="2">
              <v-btn
                type="submit"
                color="primary"
                variant="flat"
                block
                prepend-icon="mdi-plus"
                :loading="creatingDevice"
                :disabled="!canCreateDevice"
              >
                Создать конфиг
              </v-btn>
            </v-col>
          </v-row>
        </v-form>

        <v-alert
          v-if="devicesError"
          type="error"
          variant="tonal"
          density="compact"
          class="mb-3"
        >
          {{ devicesError }}
        </v-alert>

        <v-table density="comfortable" class="border rounded-lg">
          <thead>
            <tr>
              <th>Протокол</th>
              <th>Устройство</th>
              <th>Маршрут</th>
              <th>Статус</th>
              <th>Синхронизация</th>
              <th>Трафик</th>
              <th class="text-right">Действия</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!devices.length">
              <td colspan="7" class="text-center text-medium-emphasis py-6">
                {{ devicesLoading ? 'Загрузка устройств…' : 'У пользователя пока нет созданных устройств' }}
              </td>
            </tr>
            <tr v-for="device in devices" :key="device.id">
              <td>
                <v-chip size="small" variant="tonal" :color="protocolColor(device.protocol)">
                  {{ protocolLabel(device.protocol) }}
                </v-chip>
              </td>
              <td>
                <div class="font-weight-medium">{{ device.name }}</div>
                <div class="text-caption text-medium-emphasis">{{ formatDateTime(device.created_at) }}</div>
              </td>
              <td>
                <div class="mono text-body-2">{{ formatRouteLabel(device) }}</div>
                <div v-if="device.assigned_ip" class="text-caption text-medium-emphasis mono">
                  WG IP: {{ device.assigned_ip }}
                </div>
              </td>
              <td>
                <v-chip
                  size="small"
                  :color="device.status === 'active' ? 'success' : 'error'"
                >
                  {{ device.status === 'active' ? 'Активен' : 'Отозван' }}
                </v-chip>
              </td>
              <td>
                <v-chip
                  size="x-small"
                  variant="tonal"
                  :color="device.sync_mode === 'live' ? 'teal' : 'warning'"
                >
                  {{ device.sync_mode === 'live' ? 'Live-sync' : 'Legacy-пул' }}
                </v-chip>
                <div v-if="device.synced_at" class="text-caption text-medium-emphasis">
                  {{ formatDateTime(device.synced_at) }}
                </div>
              </td>
              <td>
                <span v-if="device.traffic_state === 'no_reports'" class="text-caption text-medium-emphasis">
                  Отчёт ещё не поступал
                </span>
                <div v-else-if="device.traffic_state === 'stale'">
                  <v-chip size="x-small" color="warning" variant="tonal">Отчёт устарел</v-chip>
                  <div class="text-caption text-medium-emphasis mt-1">
                    ↑ {{ formatBytes(device.upload_bytes) }} / ↓ {{ formatBytes(device.download_bytes) }}
                  </div>
                </div>
                <div v-else class="text-body-2 mono">
                  ↑ {{ formatBytes(device.upload_bytes) }} / ↓ {{ formatBytes(device.download_bytes) }}
                </div>
              </td>
              <td class="text-right">
                <div class="d-inline-flex align-center ga-1 flex-nowrap">
                  <v-btn
                    icon="mdi-content-copy"
                    size="small"
                    variant="text"
                    title="Копировать конфиг"
                    :disabled="!devicePayload(device)"
                    @click="handleCopyDevice(device)"
                  />
                  <v-btn
                    icon="mdi-download"
                    size="small"
                    variant="text"
                    :title="`Скачать ${deviceFilename(device)}`"
                    :disabled="!devicePayload(device)"
                    @click="handleDownloadDevice(device)"
                  />
                  <v-btn
                    icon="mdi-qrcode"
                    size="small"
                    variant="text"
                    title="Показать QR-код"
                    :disabled="!devicePayload(device)"
                    @click="handleShowDeviceQr(device)"
                  />
                  <v-btn
                    icon="mdi-cancel"
                    size="small"
                    variant="text"
                    color="warning"
                    title="Отозвать устройство"
                    :loading="deviceActionId === device.id"
                    :disabled="device.status !== 'active' || Boolean(deviceActionId)"
                    @click="handleRevokeDevice(device)"
                  />
                  <v-btn
                    icon="mdi-delete-outline"
                    size="small"
                    variant="text"
                    color="error"
                    title="Удалить устройство"
                    :loading="deviceActionId === device.id"
                    :disabled="Boolean(deviceActionId)"
                    @click="handleDeleteDevice(device)"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-card>

      <!-- Settings & ANet Section -->
      <v-row>
        <v-col cols="12" lg="7">
          <v-card variant="outlined" class="pa-5 h-100">
            <h2 class="text-h6 font-weight-bold mb-4">Настройки профиля и маршрутизации ANet</h2>
            <UserForm
              v-model="user"
              v-model:valid="userFormValid"
              hide-status
              hide-readonly
              @submit="handleSaveUser"
            />
            <div class="d-flex justify-end mt-4">
              <v-btn
                color="primary"
                variant="flat"
                prepend-icon="mdi-content-save"
                :loading="savingUser"
                :disabled="!userFormValid"
                @click="handleSaveUser"
              >
                Сохранить настройки
              </v-btn>
            </div>
          </v-card>
        </v-col>

        <v-col cols="12" lg="5" class="d-flex flex-column ga-5">
          <v-card variant="outlined" class="pa-5">
            <RateEditForm
              v-if="user.rate"
              v-model="user"
              :saving="savingRate"
              @save="handleSaveRate"
            />
            <RateCreateForm
              v-else
              :loading="savingRate"
              @create="handleCreateRate"
            />
          </v-card>

          <v-card variant="outlined" class="pa-5">
            <h2 class="text-subtitle-1 font-weight-bold mb-1">Конфигурация ANet (client.toml)</h2>
            <div class="text-caption text-medium-emphasis mb-3">
              Загружается с авторизацией администратора; QR-код строится локально в браузере.
            </div>

            <v-alert
              v-if="configError"
              type="error"
              variant="tonal"
              density="compact"
              class="mb-3"
              closable
              @click:close="configError = ''"
            >
              {{ configError }}
            </v-alert>

            <div class="d-flex flex-wrap ga-2">
              <v-btn
                color="success"
                variant="flat"
                size="small"
                prepend-icon="mdi-download"
                :loading="configLoading"
                @click="handleDownloadClientToml"
              >
                Скачать client.toml
              </v-btn>
              <v-btn
                color="primary"
                variant="tonal"
                size="small"
                prepend-icon="mdi-qrcode"
                :loading="configLoading"
                @click="handleToggleClientQr"
              >
                {{ showClientQr ? 'Скрыть QR-код' : 'Показать QR-код' }}
              </v-btn>
            </div>

            <div v-if="showClientQr && clientToml" class="mt-4 d-flex justify-center">
              <QrCodeBox
                :value="clientToml"
                :title="`ANet · ${user.uid}`"
                subtitle="Локальный QR-код client.toml"
                :filename="`${sanitizeFilePart(user.uid, 'client')}.toml`"
              />
            </div>
          </v-card>
        </v-col>
      </v-row>
    </template>

    <!-- Local QR Dialog -->
    <v-dialog v-model="qrDialog.open" max-width="440">
      <v-card>
        <v-card-title class="d-flex align-center justify-space-between px-5 pt-4 pb-2">
          <span class="text-subtitle-1 font-weight-bold">QR-код</span>
          <v-btn icon="mdi-close" variant="text" size="small" @click="qrDialog.open = false" />
        </v-card-title>
        <v-card-text class="px-5 pb-5 pt-2">
          <QrCodeBox
            :value="qrDialog.value"
            :title="qrDialog.title"
            :subtitle="qrDialog.subtitle"
            :filename="qrDialog.filename"
          />
        </v-card-text>
      </v-card>
    </v-dialog>
  </v-container>
</template>

<style scoped>
.user-detail-page {
  padding: 24px;
}

.mono,
.mono-field :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.fingerprint-chip {
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.14);
  word-break: break-all;
}
</style>
