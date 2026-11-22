<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { isAxiosError } from 'axios'
import { api } from '@/api/client'

type Recipient = { id: string; name: string; subscribed: boolean }
type Route = { id: string; entry: string; exit: string; port: number }
type Device = { name: string; route: string; uri: string | null }
type Catalog = { groups: Recipient[]; routes: Route[]; origin: string }
const endpoint = '/vless-devices'
const groups = ref<Recipient[]>([])
const routes = ref<Route[]>([])
const devices = ref<Device[]>([])
const group = ref('')
const groupName = ref('')
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
  if (!origin.value) origin.value = data.origin
  if (!route.value) route.value = data.routes[0]?.id ?? ''
  if (!group.value) group.value = data.groups[0]?.id ?? ''
  loaded.value = true
}
watch(group, async id => {
  link.value = copied.value = message.value = ''
  devices.value = []
  if (!id) return
  try {
    const rows = await api<Device[]>(endpoint + '/' + id + '/profiles')
    if (id === group.value) devices.value = rows
  } catch (e) { if (id === group.value) message.value = detail(e) }
})
async function createGroup() {
  await run(async () => {
    const res = await api<{id: string}>(endpoint, { method: 'POST', data: { name: groupName.value } })
    groupName.value = ''
    await load()
    group.value = res.id
  })
}
async function issue() {
  await run(async () => {
    devices.value = await api<Device[]>(endpoint + '/' + group.value + '/profiles', {
      method: 'POST', data: { name: deviceName.value, route: route.value },
    })
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
onMounted(() => run(load))
</script>

<template>
  <v-card title="VLESS для пользователя" variant="outlined" class="mb-4 pa-4">
    <p v-if="loaded && !routes.length" class="mb-3">Для выдачи нужен VLESS-маршрут мастера. На первой VPS откройте обновлённый мастер, выберите сохранённый план с VLESS и примените его, чтобы подготовить входы.</p>
    <template v-if="loaded">
      <v-row dense align="center">
        <v-col cols="12" md="5"><v-select v-model="group" :items="groups" item-title="name" item-value="id" label="Получатель VLESS" hide-details :disabled="busy" /></v-col>
        <v-col cols="12" md="4"><v-text-field v-model="groupName" label="Имя нового получателя" maxlength="80" hide-details /></v-col>
        <v-col><v-btn :disabled="busy || !groupName.trim()" @click="createGroup">Создать получателя</v-btn></v-col>
      </v-row>
      <template v-if="group">
        <h3 class="mt-5">Профили устройств</h3>
        <v-row dense align="center">
          <v-col cols="12" md="5"><v-select v-model="route" :items="routes" :item-title="r => r.entry + ':' + r.port + ' → ' + r.exit" item-value="id" label="Маршрут" hide-details /></v-col>
          <v-col cols="12" md="4"><v-text-field v-model="deviceName" label="Название устройства" maxlength="80" placeholder="Телефон" hide-details /></v-col>
          <v-col><v-btn color="primary" :disabled="busy || !route || !deviceName.trim()" @click="issue">Создать конфиг</v-btn></v-col>
        </v-row>
        <div v-for="d in devices" :key="d.name + '/' + d.route" class="mt-2">
          <v-text-field v-if="d.uri" :model-value="d.uri" :label="d.name" readonly hide-details>
            <template #append><v-btn variant="tonal" @click="copy(d.uri!)">Копировать VLESS</v-btn></template>
          </v-text-field>
          <p v-else>{{ d.name }} · маршрут недоступен</p>
        </div>
        <h3 class="mt-5">Подписка получателя</h3>
        <p class="mb-2">Одна ссылка включает все действующие профили этого получателя. На устройстве откройте раздел подписок VLESS-клиента, вставьте ссылку и нажмите «Обновить».</p>
        <v-text-field v-model="origin" label="Публичный HTTPS-адрес для подписок" placeholder="https://vpn.example.org" hint="Домен должен обслуживать /api/v1/vless-subscriptions/. Мастер настраивает этот путь на HTTPS-сайте первой VPS." persistent-hint />
        <v-btn :disabled="busy || !validOrigin || !devices.some(d => d.uri)" @click="token()">{{ subscribed ? 'Заменить ссылку' : 'Получить ссылку' }}</v-btn>
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
