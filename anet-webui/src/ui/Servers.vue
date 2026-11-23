<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { advanced } from '@/composables/operatorMode'
import { getStarterProfiles, type StarterProfile } from '@/api/starter-profiles'
import { GetServers } from '@/api/servers'
import type { Server } from '@/models/server'
import ServerModal from '@/components/ServerModal.vue'
import { exportNodeInventory } from '@/api/probes'
import ProbeRevisionDialog from '@/components/ProbeRevisionDialog.vue'
import CreateServerModal from '@/components/CreateServerModal.vue'

const exporting = ref(false)
const exportError = ref('')
async function downloadInventory() {
  exporting.value = true
  exportError.value = ''
  try { await exportNodeInventory(await GetServers()) }
  catch (cause) { exportError.value = cause instanceof Error ? cause.message : 'Ошибка экспорта' }
  finally { exporting.value = false }
}

const probeNode = ref<Server | null>(null)
const data = ref<Server[]>([])
const loading = ref(false)
const searchQuery = ref('')
const starter = ref<StarterProfile[]>([])
const loadError = ref('')
const inventoryError = ref('')

function matchesExit(server: Server, profile: StarterProfile) {
  return profile.exit_public_key ? server.public_key === profile.exit_public_key
    : server.address.toLowerCase() === profile.exit.toLowerCase() && server.quic_port === profile.port
}
const exitIds = computed(() => new Set(data.value.filter(server =>
  starter.value.some(profile => matchesExit(server, profile))).map(server => server.id)))
const plannedExits = computed(() => starter.value.filter(profile =>
  !data.value.some(server => matchesExit(server, profile))))
function endpoint(host: string, port: number) {
  return (host.includes(':') ? `[${host}]` : host) + ':' + port
}

// Управление модалками
const showCreateModal = ref(false)
const showEditModal = ref(false)
const selectedServer = ref<Server | null>(null)

let refreshTimer: number | undefined

const headers = [
  { title: 'Название', key: 'name', sortable: true },
  { title: 'Адрес / Домен', key: 'address', sortable: true }, // Заменили dsn на address
  { title: 'Configured', key: 'is_active', sortable: true, align: 'center' as const },
  { title: 'Состояние', key: 'runtime.status', sortable: true, align: 'center' as const },
  { title: 'Control plane', key: 'has_control_credential', sortable: true, align: 'center' as const },
  { title: 'Connections', key: 'runtime.active_connections', sortable: true, align: 'end' as const },
  { title: 'Admission', key: 'runtime.accepting_connections', sortable: true, align: 'center' as const },
  { title: 'Uptime', key: 'runtime.uptime_seconds', sortable: true, align: 'end' as const },
  { title: 'Действия', key: 'actions', sortable: false, align: 'center' as const },
]

const formatUptime = (seconds?: number) => {
  if (seconds === undefined) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return days > 0 ? `${days}d ${hours}h` : `${hours}h ${minutes}m`
}

const loadServers = async () => {
  loading.value = true
  try {
    const [servers, profiles] = await Promise.allSettled([GetServers(), getStarterProfiles()])
    if (servers.status === 'fulfilled') data.value = servers.value
    loadError.value = servers.status === 'rejected' ? 'Не удалось обновить список серверов.' : ''
    if (profiles.status === 'fulfilled') starter.value = profiles.value
    inventoryError.value = profiles.status === 'rejected' ? 'Не удалось прочитать список выходов из плана установки.' : ''
  } finally {
    loading.value = false
  }
}

const openEdit = (server: Server) => {
  selectedServer.value = server
  showEditModal.value = true
}

const closeEditModal = () => {
  showEditModal.value = false
  selectedServer.value = null
}

const route = useRoute()
onMounted(async () => {
  showCreateModal.value = route.query.add === '1'
  await loadServers()
  const target = data.value.find(s => s.id === route.query.edit)
  if (target) openEdit(target)
  refreshTimer = window.setInterval(loadServers, 15_000)
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
})
</script>

<template>
  <v-container fluid class="servers-page">
    <div class="d-flex justify-space-between align-center flex-wrap ga-4 mb-4">
      <v-list-item
        class="px-0"
        subtitle="Управление физическими серверами, токенами управления и состоянием нод доступа"
      >
        <template #title>
          <h1 class="text-h5 font-weight-bold">Серверы VPN</h1>
        </template>
      </v-list-item>
    </div>

    <div class="d-flex justify-space-between align-center mb-4">
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        label="Поиск по названию или IP..."
        variant="outlined"
        density="compact"
        hide-details
        single-line
        style="width: 280px"
      />
      <v-btn v-if="advanced" :loading="exporting" variant="text" @click="downloadInventory">Экспорт inventory</v-btn>
      <v-btn color="primary" prepend-icon="mdi-plus" @click="showCreateModal = true">
        Добавить сервер
      </v-btn>
    </div>

    <v-alert v-if="loadError || inventoryError" type="warning" class="mb-3">{{ loadError || inventoryError }}</v-alert>
    <v-alert v-if="exportError" type="error">{{ exportError }}</v-alert>
    <v-data-table
      :headers="advanced ? headers : headers.filter(h => ['name', 'address', 'runtime.status', 'actions'].includes(h.key))"
      :items="data"
      :search="searchQuery"
      :loading="loading"
      :items-per-page="10"
      :items-per-page-options="[10, 20, 50]"
      items-per-page-text="Items per page:"
      loading-text="Загрузка нод…"
      no-data-text="Серверов пока нет — добавьте первую ноду!"
      density="comfortable"
      class="border rounded-lg"
      hover
      @click:row="(_: unknown, data: { item: Server }) => openEdit(data.item)"
    >
      <template #item.name="{ item }">
        <span class="font-weight-bold">{{ item.name }}</span>
        <div v-if="exitIds.has(item.id)" class="text-caption">Выход по плану установки</div>
      </template>

      <template #item.address="{ item }">
        <code class="font-weight-medium text-primary">{{ item.quic_port ? endpoint(item.address, item.quic_port) : item.address }}</code>
        <div v-if="item.quic_port" class="text-caption">ANet · UDP</div>
      </template>

      <template #item.is_active="{ item }">
        <v-chip :color="item.is_active ? 'success' : 'default'" size="small">
          {{ item.is_active ? 'ACTIVE' : 'OFF' }}
        </v-chip>
      </template>

      <template #item.runtime.status="{ item }">
        <div class="d-flex flex-column align-center ga-1">
          <v-chip :color="!item.runtime ? 'default' : item.runtime.status === 'online' ? 'success' : 'error'" size="small">
            {{ !item.runtime ? 'Нет телеметрии' : item.runtime.status === 'online' ? 'На связи' : 'Связь потеряна' }}
          </v-chip>
          <div v-if="item.runtime" class="text-caption text-medium-emphasis">v{{ item.runtime.version }}</div>
        </div>
      </template>

      <template #item.has_control_credential="{ item }">
        <v-chip :color="item.has_control_credential ? 'success' : 'warning'" variant="tonal" size="small">
          {{ item.has_control_credential ? 'READY' : 'REQUIRED' }}
        </v-chip>
      </template>

      <template #item.runtime.active_connections="{ item }">
        <v-chip size="small" variant="tonal" color="success" prepend-icon="mdi-account-multiple">
          {{ item.runtime?.active_connections ?? '—' }}
        </v-chip>
      </template>

      <template #item.runtime.accepting_connections="{ item }">
        <v-chip
          :color="item.runtime ? (item.runtime.accepting_connections ? 'success' : 'warning') : 'default'"
          size="small"
        >
          {{ !item.runtime ? 'UNKNOWN' : item.runtime.accepting_connections ? 'OPEN' : 'CLOSED' }}
        </v-chip>
      </template>

      <template #item.runtime.uptime_seconds="{ item }">
        <span class="text-caption text-medium-emphasis">{{ formatUptime(item.runtime?.uptime_seconds) }}</span>
      </template>

      <template #item.actions="{ item }">
        <div class="d-flex ga-1 justify-center flex-nowrap" @click.stop>
          <v-btn icon="mdi-lan-connect" variant="text" size="small" title="Проверки связности" @click="probeNode = item" />
          <v-btn
            icon="mdi-pencil"
            size="small"
            variant="text"
            title="Редактировать сервер"
            @click="openEdit(item)"
          />
        </div>
      </template>
    </v-data-table>
    <v-card v-if="plannedExits.length && !loadError" title="Выходы из сохранённого плана" variant="outlined" class="mt-4">
      <v-card-text>
        <p class="mb-2">Эти маршруты записаны мастером. Повторно примените сохранённый план в обновлённом мастере, чтобы добавить выходы в список серверов по их публичным ключам.</p>
        <v-table density="compact">
          <thead><tr><th>Вход</th><th>Выход · UDP</th><th>Профиль</th></tr></thead>
          <tbody><tr v-for="profile in plannedExits" :key="profile.id">
            <td>{{ profile.entry }}</td><td>{{ endpoint(profile.exit, profile.port) }}</td><td>{{ profile.id }} · {{ profile.protocol }}</td>
          </tr></tbody>
        </v-table>
      </v-card-text>
    </v-card>

    <ProbeRevisionDialog v-if="probeNode" :key="probeNode.id" :server="probeNode" :servers="data" @close="probeNode = null" />
    <CreateServerModal
      v-model="showCreateModal"
      @created="loadServers"
    />

    <ServerModal
      v-model="showEditModal"
      :server="selectedServer"
      @updated="loadServers"
      @close="closeEditModal"
    />
  </v-container>
</template>

<style scoped>
.servers-page {
  padding: 24px;
}
</style>
