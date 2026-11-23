<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getStarterProfiles, type StarterProfile } from '@/api/starter-profiles'
import { api } from '@/api/client'
import VlessUsage, { type Traffic } from './VlessUsage.vue'


const clients = {
  wireguard: { name: 'WireGuard', extension: 'conf', hint: 'В WireGuard выберите импорт туннеля из файла.' },
  anet: { name: 'ANet', extension: 'toml', hint: 'Откройте файл конфигурации в клиенте ANet.' },
  vless: { name: 'VLESS', extension: 'uri', hint: 'Скопируйте ссылку из файла и импортируйте её в клиент VLESS с поддержкой TLS.' },
}
const profiles = ref<StarterProfile[]>([])
const error = ref('')
const usage = ref<Record<string, Traffic | null>>({})
async function refreshTraffic() {
  try {
    const data = await api<{ routes: { id: string; starter_traffic: Traffic | null }[] }>('/vless-devices')
    usage.value = Object.fromEntries(data.routes.map(r => [r.id, r.starter_traffic]))
    error.value = ''
  } catch { error.value = 'Не удалось получить счётчики VLESS.' }
}
onMounted(async () => {
  try {
    profiles.value = await getStarterProfiles()
    if (profiles.value.some(p => p.protocol === 'vless')) {
      await refreshTraffic()
    }
  } catch {
    error.value = 'Не удалось загрузить список профилей мастера.'
  }
})

async function download(profile: StarterProfile) {
  error.value = ''
  const name = profile.id + '.' + clients[profile.protocol].extension
  try {
    const file = await api<Blob>('/starter-profiles/files/' + encodeURIComponent(name), { responseType: 'blob' })
    const url = URL.createObjectURL(file)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    error.value = 'Не удалось скачать профиль. Проверьте сеанс и повторите загрузку.'
  }
}
</script>

<template>
  <v-alert v-if="error" type="warning" variant="tonal">{{ error }}</v-alert>
  <v-card v-if="profiles.length" title="Профили мастера установки" class="mb-4">
    <v-list>
      <v-list-item v-for="p in profiles" :key="p.id" :title="clients[p.protocol].name + ' · ' + p.id">
        <template #subtitle>
          Вход {{ p.entry }}:{{ p.port }} → выход {{ p.exit }}<br>{{ clients[p.protocol].hint }}
          <VlessUsage v-if="p.protocol === 'vless'" :traffic="usage[p.id]" />
        </template>
        <template #append>
          <v-btn variant="tonal" @click="download(p)">Скачать профиль</v-btn>
        </template>
      </v-list-item>
    </v-list>
    <v-card-actions v-if="profiles.some(p => p.protocol === 'vless')">
      <v-btn variant="text" @click="refreshTraffic">Обновить трафик</v-btn>
    </v-card-actions>
  </v-card>
</template>
