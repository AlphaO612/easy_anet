<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { GetUser, GetUsers } from '@/api/users'
import CreateUserModal from '@/components/CreateUserModal.vue'
import type { User } from '@/models/user'
import { isAxiosError } from 'axios'
import { api } from '@/api/client'
import VlessUsage, { type Traffic } from './VlessUsage.vue'

type Recipient = { id: string; name: string; user_id: string | null; subscribed: boolean }
type Route = { id: string; entry: string; exit: string; port: number }
type Device = { name: string; route: string; uri: string | null; traffic?: Traffic | null }
type Catalog = { groups: Recipient[]; routes: Route[]; origin: string }
const endpoint = '/vless-devices'
const groups = ref<Recipient[]>([])
const routes = ref<Route[]>([])
const devices = ref<Device[]>([])
const page = useRoute()
type Account = Pick<User, 'id' | 'uid' | 'is_active'>
const accounts = ref<Account[]>([])
const selected = ref<Account>()
const accountId = ref('')
const showCreate = ref(false)
const legacyId = ref('')
const configuredOrigin = ref('')
const userError = ref('')
const group = computed(() => groups.value.find(g => g.user_id === accountId.value)?.id ?? '')
const legacy = computed(() => groups.value.filter(g => !g.user_id))
let searchId = 0
async function searchUsers(value = '') {
  const request = ++searchId
  try {
    const result = await GetUsers(0, 50, value)
    if (request === searchId) { accounts.value = result.items; userError.value = '' }
  } catch { if (request === searchId) userError.value = 'Не удалось загрузить пользователей.' }
}
const options = computed(() => {
  const found = new Map(accounts.value.map(u => [u.id, u]))
  if (selected.value) found.set(selected.value.id, selected.value)
  return [...found.values()].map(u => ({ value: u.id, title: u.uid || u.id, subtitle: u.is_active ? u.id : 'Доступ отключён' }))
})
const deviceName = ref('')
const route = ref('')
const origin = ref('')
const link = ref('')
const message = ref('')
const busy = ref(false)
const loaded = ref(false)
const copied = ref('')
const subscribed = computed(() => groups.value.find(g => g.id === group.value)?.subscribed)
const validOrigin = computed(() => {
  try {
    const url = new URL(origin.value)
    if (url.protocol === 'https:' && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash &&
        !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) return url.origin
  } catch { /* incomplete address */ }
  return ''
})
function detail(e: unknown) {
  return isAxiosError(e) && typeof e.response?.data?.error === 'string' ? e.response.data.error : 'Не удалось выполнить запрос. Проверьте подключение к панели.'
}
async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  message.value = copied.value = ''
  try { await action() } catch (e) { message.value = detail(e) }
  finally { busy.value = false }
}
async function load() {
  const data = await api<Catalog>(endpoint)
  groups.value = data.groups
  routes.value = data.routes
  configuredOrigin.value = data.origin
  if (data.origin) origin.value = data.origin
  if (!route.value) route.value = data.routes[0]?.id ?? ''
  loaded.value = true
}
let devicesRequest = 0
async function refreshDevices(id: string) {
  const request = ++devicesRequest
  if (!id) return
  try {
    const rows = await api<Device[]>(endpoint + '/' + id + '/profiles')
    if (id === group.value && request === devicesRequest) devices.value = rows
  } catch (e) { if (id === group.value && request === devicesRequest) message.value = detail(e) }
}
watch(group, id => {
  link.value = copied.value = message.value = ''
  devices.value = []
  refreshDevices(id)
})
async function selectAccount(id: string) {
  accountId.value = id
  selected.value = undefined
  legacyId.value = ''
  deviceName.value = ''
  if (!id) return
  try {
    const account = await GetUser(id)
    if (id === accountId.value) selected.value = account
  } catch { if (id === accountId.value) userError.value = 'Пользователь недоступен. Обновите список.' }
}
async function created(user: Pick<User, 'id' | 'uid'>) {
  await selectAccount(user.id)
  await searchUsers(user.uid)
}
async function connect(legacyRecipient?: string) {
  const id = accountId.value
  const res = await api<{id: string}>(endpoint, { method: 'POST', data: { user_id: id, recipient: legacyRecipient } })
  await load()
  return res.id
}
async function attachLegacy() {
  if (!window.confirm('Передать все устройства и действующую подписку прежнего получателя выбранному пользователю?')) return
  await run(async () => { await connect(legacyId.value); legacyId.value = ''; message.value = 'Прежние профили привязаны к пользователю.' })
}
async function issue() {
  await run(async () => {
    const target = group.value || await connect()
    const rows = await api<Device[]>(endpoint + '/' + target + '/profiles', {
      method: 'POST', data: { name: deviceName.value, route: route.value },
    })
    if (target !== group.value) return
    devicesRequest++
    devices.value = rows
    deviceName.value = ''
    message.value = 'Профиль готов. Его можно импортировать отдельно или через подписку получателя.'
  })
}
async function token(revoke = false) {
  const base = validOrigin.value
  if (subscribed.value && !window.confirm(revoke ? 'Отозвать ссылку подписки этого получателя?' : 'Заменить прежнюю ссылку этого получателя?')) return
  await run(async () => {
    const res = await api<{path?: string}>(endpoint + '/' + group.value + '/token', { method: revoke ? 'DELETE' : 'POST' })
    link.value = res.path ? base + res.path : ''
    const recipient = groups.value.find(g => g.id === group.value)
    if (recipient) recipient.subscribed = !revoke
    message.value = revoke ? 'Ссылка отозвана. Уже импортированные профили сохраняют доступ к VPN.' : 'Скопируйте ссылку сейчас. После закрытия панели можно выпустить новую.'
  })
}
async function copy(value: string) {
  try { await navigator.clipboard.writeText(value); copied.value = 'Скопировано' }
  catch { copied.value = 'Выделите поле и скопируйте вручную.' }
}
watch(() => page.query.user, id => { if (typeof id === 'string') selectAccount(id) })
onMounted(() => run(async () => {
  await Promise.all([load(), searchUsers()])
  const id = typeof page.query.user === 'string' ? page.query.user : accounts.value[0]?.id
  if (id) await selectAccount(id)
}))
</script>

<template>
  <v-card title="VLESS для пользователя" variant="outlined" class="mb-4 pa-4">
    <p v-if="loaded && !routes.length" class="mb-3">Для выдачи нужен VLESS-маршрут мастера. На первой VPS откройте обновлённый мастер, выберите сохранённый план с VLESS и примените его, чтобы подготовить входы.</p>
    <template v-if="loaded">
      <v-row dense align="center">
        <v-col cols="12" md="7">
          <v-autocomplete :model-value="accountId" :items="options" :item-props="item => ({ subtitle: item.subtitle })" :disabled="busy" :error-messages="userError"
            label="Пользователь панели" placeholder="Найдите по имени" no-filter
            no-data-text="Пользователь не найден" @update:search="searchUsers" @update:model-value="selectAccount($event ?? '')" />
        </v-col>
        <v-col><v-btn variant="tonal" :disabled="busy" @click="showCreate = true">Создать пользователя</v-btn></v-col>
      </v-row>
      <CreateUserModal v-model="showCreate" @created="created" />
      <p v-if="!accountId">Выберите учётную запись или создайте первого пользователя. Здесь появятся его профили устройств и подписка.</p>
      <template v-if="selected">
        <v-alert v-if="!selected.is_active" type="warning" variant="tonal">Пользователь отключён. Выдача профилей и обновление подписки приостановлены.</v-alert>
        <details v-if="legacy.length && !group && selected.is_active" class="mb-3">
          <summary>Привязать профили прежнего получателя</summary>
          <v-select v-model="legacyId" :items="legacy" item-title="name" item-value="id" label="Получатель из предыдущей версии" />
          <v-btn :disabled="busy || !legacyId" @click="attachLegacy">Привязать к {{ selected.uid }}</v-btn>
        </details>
        <h3 class="mt-5">Профили устройств</h3>
        <v-row dense align="center">
          <v-col cols="12" md="5"><v-select v-model="route" :items="routes" :item-title="r => r.entry + ':' + r.port + ' → ' + r.exit" item-value="id" label="Маршрут" hide-details /></v-col>
          <v-col cols="12" md="4"><v-text-field v-model="deviceName" label="Название устройства" maxlength="80" placeholder="Телефон" hide-details /></v-col>
          <v-col><v-btn color="primary" :disabled="busy || !selected.is_active || !route || !deviceName.trim()" @click="issue">Создать конфиг</v-btn></v-col>
        </v-row>
        <div v-for="d in devices" :key="d.name + '/' + d.route" class="mt-2">
          <v-text-field v-if="d.uri" :model-value="d.uri" :label="d.name" readonly hide-details>
            <template #append><v-btn variant="tonal" @click="copy(d.uri!)">Копировать VLESS</v-btn></template>
          </v-text-field>
          <p v-else>{{ d.name }} · маршрут недоступен</p>
          <VlessUsage :traffic="d.traffic" />
        </div>
        <v-btn v-if="devices.length" :disabled="busy" variant="text" @click="run(() => refreshDevices(group))">Обновить трафик</v-btn>
        <h3 class="mt-5">Подписка пользователя</h3>
        <p class="mb-2">Одна ссылка включает все действующие профили этого получателя. На устройстве откройте раздел подписок VLESS-клиента, вставьте ссылку и нажмите «Обновить».</p>
        <p v-if="configuredOrigin" class="my-2">Адрес выдачи подписок <strong>{{ configuredOrigin }}</strong></p>
        <template v-else>
          <p class="my-2">Установка пока не сообщила публичный адрес. На VPS панели настройте HTTPS для узлов и подписок. Если домен уже обслуживает этот путь через ваш reverse proxy, введите его ниже. <a href="https://github.com/AlphaO612/easy_anet/blob/master/contrib/deploy/distribution/PANEL.md#1-дайте-новой-vps-адрес-панели" target="_blank" rel="noopener">Команда настройки HTTPS</a></p>
          <v-text-field v-model="origin" label="Публичный домен подписок" placeholder="https://vpn.example.org" hint="Адрес должен открываться с телефона. Используйте HTTPS без /api/v1 и пути." persistent-hint />
        </template>
        <v-btn :disabled="busy || !selected.is_active || !validOrigin || !devices.some(d => d.uri)" @click="token()">{{ subscribed ? 'Заменить ссылку' : 'Получить ссылку' }}</v-btn>
        <v-btn v-if="subscribed" :disabled="busy" variant="text" @click="token(true)">Отозвать ссылку</v-btn>
        <v-text-field v-if="link" :model-value="link" readonly label="Личная ссылка подписки" class="mt-2" hide-details>
          <template #append><v-btn variant="tonal" @click="copy(link)">Копировать</v-btn></template>
        </v-text-field>
      </template>
    </template>
    <v-btn v-if="!loaded && !busy" @click="run(load)">Повторить загрузку</v-btn>
    <p role="status" aria-live="polite">{{ copied || message }}</p>
  </v-card>
</template>
