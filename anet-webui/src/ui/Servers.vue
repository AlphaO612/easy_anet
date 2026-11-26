<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { advanced } from '@/composables/operatorMode'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { getStarterProfiles, type StarterProfile } from '@/api/starter-profiles'
import { DeleteServer, GetServers, ResetServerState, SetNodeAdmission } from '@/api/servers'
import { CreateIngressRoute, DeleteIngressRoute, GetIngressRoutes } from '@/api/devices'
import { exportNodeInventory } from '@/api/probes'
import type { Server } from '@/models/server'
import type { CreateIngressRouteRequest, CreateIngressRouteResponse, IngressRoute } from '@/models/device'
import { copyText, extractApiError, formatDateTime, formatUptime } from '@/utils/format'
import { formatRegionBadge, formatRegionFull, NODE_ROLE_OPTIONS, nodeRoleMeta, parseTags } from '@/utils/regions'
import ServerModal from '@/components/ServerModal.vue'
import ProbeRevisionDialog from '@/components/ProbeRevisionDialog.vue'
import CreateServerModal from '@/components/CreateServerModal.vue'
import SitesManager from '@/components/SitesManager.vue'

const { confirmAction } = useConfirmDialog()
const appMessage = useAppMessage()
const route = useRoute()

const activeTab = ref<'nodes' | 'ingress' | 'sites'>('nodes')

// === Ноды ANet ===
const exporting = ref(false)
const exportError = ref('')
async function downloadInventory() {
  exporting.value = true
  exportError.value = ''
  try {
    await exportNodeInventory(await GetServers())
  } catch (cause) {
    exportError.value = extractApiError(cause, 'Ошибка экспорта')
  } finally {
    exporting.value = false
  }
}

const probeNode = ref<Server | null>(null)
const data = ref<Server[]>([])
const loading = ref(false)
const searchQuery = ref('')
const selectedRegion = ref<string>('ALL')
const selectedTag = ref<string>('ALL')
const selectedRole = ref<string>('ALL')
const groupByRegion = ref(false)
const starter = ref<StarterProfile[]>([])
const loadError = ref('')
const inventoryError = ref('')
const admissionBusy = reactive<Record<string, boolean>>({})
const actionBusy = reactive<Record<string, boolean>>({})

const regionFilterOptions = computed(() => {
  const counts = new Map<string, number>()
  for (const s of data.value) {
    const code = (s.region || '').trim().toUpperCase() || 'UNSET'
    counts.set(code, (counts.get(code) ?? 0) + 1)
  }
  return Array.from(counts.entries())
    .sort((a, b) => (a[0] === 'UNSET' ? 1 : b[0] === 'UNSET' ? -1 : a[0].localeCompare(b[0])))
    .map(([code, count]) => ({
      code,
      label: code === 'UNSET' ? 'Без страны' : formatRegionBadge(code),
      count,
    }))
})

const tagFilterOptions = computed(() => {
  const counts = new Map<string, number>()
  for (const s of data.value) {
    for (const t of parseTags(s.tags)) {
      counts.set(t, (counts.get(t) ?? 0) + 1)
    }
  }
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([tag, count]) => ({ tag, count }))
})

const filteredServers = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  return data.value.filter((s) => {
    const code = (s.region || '').trim().toUpperCase() || 'UNSET'
    if (selectedRegion.value !== 'ALL' && code !== selectedRegion.value) return false
    if (selectedRole.value !== 'ALL' && (s.node_role || 'auto') !== selectedRole.value) return false
    if (selectedTag.value !== 'ALL' && !parseTags(s.tags).includes(selectedTag.value)) return false
    if (q) {
      const hay = `${s.name} ${s.address} ${s.region || ''} ${s.tags || ''}`.toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
})

const groupedServers = computed(() => {
  const groups = new Map<string, Server[]>()
  for (const s of filteredServers.value) {
    const code = (s.region || '').trim().toUpperCase() || 'UNSET'
    const list = groups.get(code) ?? []
    list.push(s)
    groups.set(code, list)
  }
  return Array.from(groups.entries())
    .sort((a, b) => (a[0] === 'UNSET' ? 1 : b[0] === 'UNSET' ? -1 : a[0].localeCompare(b[0])))
    .map(([code, items]) => ({
      code,
      title: code === 'UNSET' ? '🌐 Без страны / группы' : formatRegionFull(code),
      items,
      onlineCount: items.filter((i) => i.runtime?.status === 'online').length,
    }))
})

const addressCounts = computed(() => {
  const counts = new Map<string, number>()
  for (const s of data.value) {
    const key = s.address.trim().toLowerCase()
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return counts
})

function sharedHostCount(server: Server): number {
  return addressCounts.value.get(server.address.trim().toLowerCase()) ?? 1
}

function shortPubKey(key: string): string {
  const trimmed = key.trim()
  return trimmed.length > 10 ? `${trimmed.slice(0, 8)}…` : trimmed || '—'
}

function primaryPort(server: Server): number | null {
  return server.quic_port ?? server.ssh_port ?? server.vnc_port ?? null
}

function displayEndpoint(server: Server): string {
  const port = primaryPort(server)
  return port ? endpoint(server.address, port) : server.address
}

function matchesExit(server: Server, profile: StarterProfile) {
  return profile.exit_public_key
    ? server.public_key === profile.exit_public_key
    : server.address.toLowerCase() === profile.exit.toLowerCase() && server.quic_port === profile.port
}
const exitIds = computed(() => new Set(data.value.filter(server =>
  starter.value.some(profile => matchesExit(server, profile))).map(server => server.id)))
const plannedExits = computed(() => starter.value.filter(profile =>
  !data.value.some(server => matchesExit(server, profile))))

function endpoint(host: string, port: number) {
  return (host.includes(':') ? `[${host}]` : host) + ':' + port
}

const showCreateModal = ref(false)
const showEditModal = ref(false)
const selectedServer = ref<Server | null>(null)

let refreshTimer: number | undefined

const headers = [
  { title: 'Название', key: 'name', sortable: true },
  { title: 'Адрес / Домен', key: 'address', sortable: true },
  { title: 'Configured', key: 'is_active', sortable: true, align: 'center' as const },
  { title: 'Состояние', key: 'runtime.status', sortable: true, align: 'center' as const },
  { title: 'Control plane', key: 'has_control_credential', sortable: true, align: 'center' as const },
  { title: 'Connections', key: 'runtime.active_connections', sortable: true, align: 'end' as const },
  { title: 'Admission', key: 'runtime.accepting_connections', sortable: true, align: 'center' as const },
  { title: 'Uptime', key: 'runtime.uptime_seconds', sortable: true, align: 'end' as const },
  { title: 'Действия', key: 'actions', sortable: false, align: 'center' as const },
]

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

const toggleAdmission = async (server: Server) => {
  if (!server.runtime || server.runtime.status !== 'online') return
  const nextAccepting = !server.runtime.accepting_connections
  admissionBusy[server.id] = true
  try {
    await SetNodeAdmission(server.id, nextAccepting)
    appMessage.success(
      nextAccepting
        ? `Нода «${server.name}»: команда на открытие приёма подключений отправлена`
        : `Нода «${server.name}»: команда на закрытие приёма (drain) отправлена`,
    )
    await loadServers()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось изменить режим приёма подключений'))
  } finally {
    admissionBusy[server.id] = false
  }
}

const resetState = async (server: Server) => {
  const accepted = await confirmAction({
    title: 'Сбросить состояние ноды?',
    message: `Сбросить runtime-состояние и кэш телеметрии ноды «${server.name}» (${server.address})?`,
    confirmText: 'Сбросить состояние',
    confirmColor: 'warning',
  })
  if (!accepted) return
  actionBusy[server.id] = true
  try {
    await ResetServerState(server.id)
    appMessage.success(`Состояние ноды «${server.name}» сброшено`)
    await loadServers()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось сбросить состояние ноды'))
  } finally {
    actionBusy[server.id] = false
  }
}

const deleteNode = async (server: Server) => {
  const accepted = await confirmAction({
    title: 'Удалить ноду?',
    message: `Удалить ноду «${server.name}» (${server.address})? Она будет исключена из пулов и маршрутов.`,
    confirmText: 'Удалить ноду',
    confirmColor: 'error',
  })
  if (!accepted) return
  actionBusy[server.id] = true
  try {
    await DeleteServer(server.id)
    appMessage.success(`Нода «${server.name}» удалена`)
    await loadServers()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось удалить ноду'))
  } finally {
    actionBusy[server.id] = false
  }
}

// === Входные точки VLESS / WireGuard ===
const ingressRoutes = ref<IngressRoute[]>([])
const ingressOrigin = ref('')
const ingressLoading = ref(false)
const ingressError = ref('')
const deletingRouteId = ref<string | null>(null)

const showIngressModal = ref(false)
const ingressSaving = ref(false)
const createdIngress = ref<CreateIngressRouteResponse | null>(null)

const blankIngressForm = () => ({
  protocol: 'vless' as 'vless' | 'wireguard',
  name: '',
  entry_host: '',
  entry_port: 443,
  exit_host: '',
  entry_server_id: null as string | null,
  exit_server_id: null as string | null,
  sni: '',
  wg_server_public_key: '',
  wg_subnet: '',
})
const ingressForm = reactive(blankIngressForm())

const serverSelectItems = computed(() =>
  data.value.map(s => ({
    title: `${s.name} (${s.address})`,
    value: s.id,
  })),
)

const ingressReady = computed(() =>
  Boolean(
    ingressForm.name.trim()
    && ingressForm.entry_host.trim()
    && ingressForm.exit_host.trim()
    && Number.isInteger(Number(ingressForm.entry_port))
    && Number(ingressForm.entry_port) >= 1
    && Number(ingressForm.entry_port) <= 65535,
  ),
)

watch(() => ingressForm.protocol, (proto) => {
  if (proto === 'vless' && ingressForm.entry_port === 51820) ingressForm.entry_port = 443
  if (proto === 'wireguard' && ingressForm.entry_port === 443) ingressForm.entry_port = 51820
})

watch(() => ingressForm.entry_server_id, (sid) => {
  const srv = data.value.find(s => s.id === sid)
  if (!srv) return
  ingressForm.entry_host = srv.address
  if (ingressForm.protocol === 'vless' && !ingressForm.sni.trim()) {
    ingressForm.sni = srv.address
  }
  const exitSrv = data.value.find(s => s.id === ingressForm.exit_server_id)
  if (!ingressForm.name.trim() && exitSrv) {
    ingressForm.name = `${ingressForm.protocol.toUpperCase()} ${srv.name} → ${exitSrv.name}`
  }
})

watch(() => ingressForm.exit_server_id, (sid) => {
  const srv = data.value.find(s => s.id === sid)
  if (!srv) return
  ingressForm.exit_host = srv.address
  const entrySrv = data.value.find(s => s.id === ingressForm.entry_server_id)
  if (!ingressForm.name.trim() && entrySrv) {
    ingressForm.name = `${ingressForm.protocol.toUpperCase()} ${entrySrv.name} → ${srv.name}`
  }
})

const loadIngressRoutes = async () => {
  ingressLoading.value = true
  ingressError.value = ''
  try {
    const res = await GetIngressRoutes()
    ingressOrigin.value = res.origin
    ingressRoutes.value = res.routes
  } catch (err) {
    ingressError.value = extractApiError(err, 'Не удалось загрузить входные точки')
  } finally {
    ingressLoading.value = false
  }
}

const openIngressModal = () => {
  Object.assign(ingressForm, blankIngressForm())
  createdIngress.value = null
  showIngressModal.value = true
}

const closeIngressModal = () => {
  showIngressModal.value = false
  createdIngress.value = null
}

const submitIngressRoute = async () => {
  if (!ingressReady.value) return
  ingressSaving.value = true
  try {
    const payload: CreateIngressRouteRequest = {
      protocol: ingressForm.protocol,
      name: ingressForm.name.trim(),
      entry_host: ingressForm.entry_host.trim(),
      entry_port: Number(ingressForm.entry_port),
      exit_host: ingressForm.exit_host.trim(),
      entry_server_id: ingressForm.entry_server_id || null,
      exit_server_id: ingressForm.exit_server_id || null,
      sni: ingressForm.protocol === 'vless' ? (ingressForm.sni.trim() || null) : null,
      wg_server_public_key: ingressForm.protocol === 'wireguard' ? (ingressForm.wg_server_public_key.trim() || null) : null,
      wg_subnet: ingressForm.protocol === 'wireguard' ? (ingressForm.wg_subnet.trim() || null) : null,
    }
    createdIngress.value = await CreateIngressRoute(payload)
    appMessage.success(`Входная точка «${createdIngress.value.route.name}» создана`)
    await loadIngressRoutes()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось создать входную точку'))
  } finally {
    ingressSaving.value = false
  }
}

const removeIngressRoute = async (routeItem: IngressRoute) => {
  const accepted = await confirmAction({
    title: 'Удалить входную точку?',
    message: `Удалить маршрут «${routeItem.name}» (${routeItem.protocol.toUpperCase()} ${endpoint(routeItem.entry_host, routeItem.entry_port)} → ${routeItem.exit_host})?`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return
  deletingRouteId.value = routeItem.id
  try {
    await DeleteIngressRoute(routeItem.id)
    appMessage.success(`Входная точка «${routeItem.name}» удалена`)
    await loadIngressRoutes()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось удалить входную точку'))
  } finally {
    deletingRouteId.value = null
  }
}

const copySyncToken = async () => {
  if (!createdIngress.value?.sync_token) return
  const ok = await copyText(createdIngress.value.sync_token)
  if (ok) appMessage.success('sync_token скопирован в буфер обмена')
  else appMessage.error('Не удалось скопировать токен')
}

const copyOrigin = async () => {
  if (!ingressOrigin.value) return
  const ok = await copyText(ingressOrigin.value)
  if (ok) appMessage.success('Origin скопирован')
  else appMessage.error('Не удалось скопировать origin')
}

watch(activeTab, (tab) => {
  if (tab === 'ingress' && !ingressRoutes.value.length && !ingressLoading.value) {
    loadIngressRoutes()
  }
})

onMounted(async () => {
  showCreateModal.value = route.query.add === '1'
  await loadServers()
  const target = data.value.find(s => s.id === route.query.edit)
  if (target) openEdit(target)
  refreshTimer = window.setInterval(() => {
    if (activeTab.value === 'nodes') loadServers()
  }, 15_000)
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
})
</script>

<template>
  <v-container fluid class="servers-page">
    <div class="d-flex justify-space-between align-center flex-wrap ga-4 mb-3">
      <v-list-item
        class="px-0"
        subtitle="Управление физическими серверами, токенами управления, состоянием нод и входными точками VLESS / WireGuard"
      >
        <template #title>
          <h1 class="text-h5 font-weight-bold">Серверы VPN</h1>
        </template>
      </v-list-item>
    </div>

    <v-tabs v-model="activeTab" color="primary" class="mb-4 border-b">
      <v-tab value="nodes" prepend-icon="mdi-server-network">Ноды ANet</v-tab>
      <v-tab value="ingress" prepend-icon="mdi-transit-connection-variant">Входные точки VLESS / WireGuard</v-tab>
      <v-tab value="sites" prepend-icon="mdi-web">Сайты маскировки (HTTPS)</v-tab>
    </v-tabs>

    <!-- Вкладка 1: Ноды ANet -->
    <div v-if="activeTab === 'nodes'">
      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-3">
        <div class="d-flex align-center flex-wrap ga-2">
          <v-text-field
            v-model="searchQuery"
            prepend-inner-icon="mdi-magnify"
            label="Поиск по названию, IP, стране, тегу..."
            variant="outlined"
            density="compact"
            hide-details
            single-line
            style="min-width: 260px; max-width: 320px"
          />
          <v-switch
            v-model="groupByRegion"
            label="Группировать по странам"
            color="primary"
            density="compact"
            hide-details
            inset
            class="ms-1"
          />
        </div>
        <div class="d-flex align-center ga-2">
          <v-btn v-if="advanced" :loading="exporting" variant="text" @click="downloadInventory">Экспорт inventory</v-btn>
          <v-btn color="primary" prepend-icon="mdi-plus" @click="showCreateModal = true">
            Добавить сервер
          </v-btn>
        </div>
      </div>

      <!-- Фильтры по странам и тегам -->
      <div v-if="regionFilterOptions.length || tagFilterOptions.length" class="d-flex flex-wrap align-center ga-2 mb-4">
        <v-chip
          size="small"
          :color="selectedRegion === 'ALL' ? 'primary' : undefined"
          :variant="selectedRegion === 'ALL' ? 'flat' : 'outlined'"
          @click="selectedRegion = 'ALL'"
        >
          Все страны ({{ data.length }})
        </v-chip>
        <v-chip
          v-for="r in regionFilterOptions"
          :key="r.code"
          size="small"
          :color="selectedRegion === r.code ? 'primary' : undefined"
          :variant="selectedRegion === r.code ? 'flat' : 'outlined'"
          @click="selectedRegion = selectedRegion === r.code ? 'ALL' : r.code"
        >
          {{ r.label }} ({{ r.count }})
        </v-chip>

        <v-divider vertical class="mx-1" />

        <v-chip
          v-for="rOpt in NODE_ROLE_OPTIONS.filter(x => x.value !== 'auto')"
          :key="rOpt.value"
          size="small"
          :prepend-icon="rOpt.icon"
          :color="selectedRole === rOpt.value ? rOpt.color : undefined"
          :variant="selectedRole === rOpt.value ? 'flat' : 'outlined'"
          @click="selectedRole = selectedRole === rOpt.value ? 'ALL' : rOpt.value"
        >
          {{ rOpt.short }}
        </v-chip>

        <v-divider v-if="tagFilterOptions.length" vertical class="mx-1" />

        <v-chip
          v-for="t in tagFilterOptions"
          :key="t.tag"
          size="small"
          prepend-icon="mdi-tag-outline"
          :color="selectedTag === t.tag ? 'secondary' : undefined"
          :variant="selectedTag === t.tag ? 'flat' : 'tonal'"
          @click="selectedTag = selectedTag === t.tag ? 'ALL' : t.tag"
        >
          {{ t.tag }} ({{ t.count }})
        </v-chip>
      </div>

      <v-alert v-if="loadError || inventoryError" type="warning" variant="tonal" class="mb-3">{{ loadError || inventoryError }}</v-alert>
      <v-alert v-if="exportError" type="error" variant="tonal" class="mb-3">{{ exportError }}</v-alert>

      <!-- Сгруппированный вид по странам -->
      <div v-if="groupByRegion" class="d-flex flex-column ga-4">
        <v-card v-for="grp in groupedServers" :key="grp.code" variant="outlined" class="rounded-lg">
          <v-card-title class="d-flex align-center justify-space-between py-3 px-4 border-b">
            <span class="text-subtitle-1 font-weight-bold">{{ grp.title }}</span>
            <div class="d-flex align-center ga-2">
              <v-chip size="x-small" color="success" variant="tonal">
                Онлайн: {{ grp.onlineCount }} / {{ grp.items.length }}
              </v-chip>
            </div>
          </v-card-title>
          <v-table density="comfortable" hover>
            <thead>
              <tr>
                <th>Название и теги</th>
                <th>Адрес</th>
                <th class="text-center">Состояние</th>
                <th class="text-end">Сессии</th>
                <th class="text-end">Uptime</th>
                <th class="text-center">Действия</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in grp.items" :key="item.id" class="cursor-pointer" @click="openEdit(item)">
                <td>
                  <div class="font-weight-bold">{{ item.name }}</div>
                  <div v-if="parseTags(item.tags).length" class="d-flex flex-wrap ga-1 mt-1">
                    <v-chip v-for="tg in parseTags(item.tags)" :key="tg" size="x-small" variant="tonal" color="secondary">
                      {{ tg }}
                    </v-chip>
                  </div>
                </td>
                <td>
                  <code class="text-primary">{{ displayEndpoint(item) }}</code>
                </td>
                <td class="text-center">
                  <v-chip :color="!item.runtime ? 'default' : item.runtime.status === 'online' ? 'success' : 'error'" size="small">
                    {{ !item.runtime ? 'Нет телеметрии' : item.runtime.status === 'online' ? 'На связи' : 'Связь потеряна' }}
                  </v-chip>
                </td>
                <td class="text-end">{{ item.runtime?.active_connections ?? '—' }}</td>
                <td class="text-end text-caption text-medium-emphasis">{{ formatUptime(item.runtime?.uptime_seconds) }}</td>
                <td class="text-center" @click.stop>
                  <v-btn icon="mdi-lan-connect" variant="text" size="small" title="Проверки связности" @click="probeNode = item" />
                  <v-btn icon="mdi-pencil" size="small" variant="text" title="Редактировать" @click="openEdit(item)" />
                  <v-btn icon="mdi-delete" size="small" variant="text" color="error" title="Удалить" @click="deleteNode(item)" />
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </div>

      <v-data-table
        v-else
        :headers="headers"
        :items="filteredServers"
        :loading="loading"
        :items-per-page="20"
        :items-per-page-options="[10, 20, 50]"
        items-per-page-text="Строк на странице:"
        loading-text="Загрузка нод…"
        no-data-text="Серверы не найдены"
        density="comfortable"
        class="border rounded-lg"
        hover
        @click:row="(_: unknown, row: { item: Server }) => openEdit(row.item)"
      >
        <template #item.name="{ item }">
          <div class="d-flex align-center flex-wrap ga-1">
            <v-chip v-if="item.region" size="x-small" variant="tonal" color="primary">
              {{ formatRegionBadge(item.region) }}
            </v-chip>
            <v-chip
              v-if="item.node_role && item.node_role !== 'auto'"
              size="x-small"
              variant="tonal"
              :color="nodeRoleMeta(item.node_role).color"
            >
              {{ nodeRoleMeta(item.node_role).short }}
            </v-chip>
            <span class="font-weight-bold">{{ item.name }}</span>
          </div>
          <div v-if="parseTags(item.tags).length || exitIds.has(item.id)" class="d-flex flex-wrap align-center ga-1 mt-1">
            <v-chip v-for="tg in parseTags(item.tags)" :key="tg" size="x-small" variant="outlined" color="secondary">
              {{ tg }}
            </v-chip>
            <span v-if="exitIds.has(item.id)" class="text-caption text-medium-emphasis">Выход</span>
          </div>
        </template>

        <template #item.address="{ item }">
          <div class="d-flex align-center flex-wrap ga-1">
            <code class="font-weight-medium text-primary">
              {{ displayEndpoint(item) }}
            </code>
            <v-chip
              v-if="sharedHostCount(item) > 1"
              size="x-small"
              color="info"
              variant="tonal"
              prepend-icon="mdi-server"
              :title="`Несколько инстансов на физическом хосте ${item.address}`"
            >
              VPS ×{{ sharedHostCount(item) }}
            </v-chip>
          </div>
          <div class="text-caption text-medium-emphasis d-flex align-center flex-wrap ga-2">
            <span v-if="item.quic_port">ANet · UDP :{{ item.quic_port }}</span>
            <span v-else-if="primaryPort(item)">Порт :{{ primaryPort(item) }}</span>
            <code v-if="sharedHostCount(item) > 1" class="text-caption">pk: {{ shortPubKey(item.public_key) }}</code>
          </div>
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
          <div @click.stop>
            <v-btn
              v-if="item.runtime"
              size="x-small"
              variant="tonal"
              :color="item.runtime.accepting_connections ? 'success' : 'warning'"
              :prepend-icon="item.runtime.accepting_connections ? 'mdi-door-open' : 'mdi-door-closed'"
              :loading="Boolean(admissionBusy[item.id])"
              :disabled="item.runtime.status !== 'online'"
              :title="item.runtime.status !== 'online'
                ? 'Нода не в сети'
                : item.runtime.accepting_connections
                  ? 'Нажмите, чтобы запретить новые подключения (drain)'
                  : 'Нажмите, чтобы разрешить новые подключения'"
              @click="toggleAdmission(item)"
            >
              {{ item.runtime.accepting_connections ? 'OPEN' : 'CLOSED' }}
            </v-btn>
            <v-chip v-else color="default" size="small">UNKNOWN</v-chip>
          </div>
        </template>

        <template #item.runtime.uptime_seconds="{ item }">
          <span class="text-caption text-medium-emphasis">{{ formatUptime(item.runtime?.uptime_seconds) }}</span>
        </template>

        <template #item.actions="{ item }">
          <div class="d-flex ga-1 justify-center flex-nowrap" @click.stop>
            <v-btn icon="mdi-lan-connect" variant="text" size="small" title="Проверки связности" @click="probeNode = item" />
            <v-btn icon="mdi-pencil" size="small" variant="text" title="Редактировать сервер" @click="openEdit(item)" />
            <v-btn
              icon="mdi-restart"
              size="small"
              variant="text"
              color="warning"
              title="Сбросить состояние"
              :loading="Boolean(actionBusy[item.id])"
              @click="resetState(item)"
            />
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="Удалить ноду"
              :loading="Boolean(actionBusy[item.id])"
              @click="deleteNode(item)"
            />
          </div>
        </template>
      </v-data-table>

      <v-card v-if="plannedExits.length && !loadError" title="Выходы из сохранённого плана" variant="outlined" class="mt-4">
        <v-card-text>
          <p class="mb-2">Эти маршруты записаны мастером. Повторно примените сохранённый план в обновлённом мастере, чтобы добавить выходы в список серверов по их публичным ключам.</p>
          <v-table density="compact">
            <thead><tr><th>Вход</th><th>Выход · UDP</th><th>Профиль</th></tr></thead>
            <tbody>
              <tr v-for="profile in plannedExits" :key="profile.id">
                <td>{{ profile.entry }}</td>
                <td>{{ endpoint(profile.exit, profile.port) }}</td>
                <td>{{ profile.id }} · {{ profile.protocol }}</td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
      </v-card>
    </div>

    <!-- Вкладка 2: Входные точки VLESS / WireGuard -->
    <div v-else-if="activeTab === 'ingress'">
      <v-alert v-if="ingressOrigin" type="info" variant="tonal" density="compact" class="mb-4">
        <div class="d-flex align-center justify-space-between flex-wrap ga-2">
          <div>
            <strong>Origin панели:</strong>
            <code class="ml-1">{{ ingressOrigin }}</code>
          </div>
          <v-btn size="x-small" variant="outlined" prepend-icon="mdi-content-copy" @click="copyOrigin">
            Скопировать
          </v-btn>
        </div>
      </v-alert>

      <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-4">
        <v-btn variant="outlined" prepend-icon="mdi-refresh" :loading="ingressLoading" @click="loadIngressRoutes">
          Обновить
        </v-btn>
        <v-btn color="primary" prepend-icon="mdi-plus" @click="openIngressModal">
          Добавить входную точку
        </v-btn>
      </div>

      <v-alert v-if="ingressError" type="error" variant="tonal" class="mb-3">{{ ingressError }}</v-alert>

      <v-card variant="outlined" rounded="lg">
        <v-progress-linear v-if="ingressLoading" indeterminate color="primary" />
        <v-table density="comfortable">
          <thead>
            <tr>
              <th>Протокол</th>
              <th>Название</th>
              <th>Маршрут (вход → выход)</th>
              <th>Режим синхронизации</th>
              <th>Последняя синхронизация</th>
              <th class="text-center">Действия</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!ingressLoading && !ingressRoutes.length">
              <td colspan="6" class="text-center text-medium-emphasis py-6">
                Входных точек VLESS / WireGuard пока нет.
              </td>
            </tr>
            <tr v-for="item in ingressRoutes" :key="item.id">
              <td>
                <v-chip
                  size="small"
                  variant="tonal"
                  :color="item.protocol === 'vless' ? 'primary' : item.protocol === 'wireguard' ? 'teal' : 'secondary'"
                >
                  {{ item.protocol.toUpperCase() }}
                </v-chip>
              </td>
              <td>
                <div class="font-weight-medium">{{ item.name }}</div>
                <div v-if="item.sni" class="text-caption text-medium-emphasis">SNI: {{ item.sni }}</div>
                <div v-if="item.wg_subnet" class="text-caption text-medium-emphasis">Подсеть: {{ item.wg_subnet }}</div>
              </td>
              <td>
                <code>{{ endpoint(item.entry_host, item.entry_port) }}</code>
                <span class="mx-1">→</span>
                <code>{{ item.exit_host }}</code>
              </td>
              <td>
                <div class="d-flex align-center flex-wrap ga-1">
                  <v-chip
                    size="small"
                    variant="tonal"
                    :color="item.uplink_up === false ? 'error' : item.sync_mode === 'live' ? 'success' : 'warning'"
                  >
                    {{ item.uplink_up === false ? 'uplink down' : item.sync_mode === 'live' ? 'live' : 'legacy_pool' }}
                  </v-chip>
                  <v-chip
                    v-if="item.last_latency_ms != null"
                    size="x-small"
                    variant="outlined"
                    :color="(item.last_loss_pct ?? 0) >= 15 ? 'warning' : 'success'"
                  >
                    {{ Math.round(item.last_latency_ms) }} мс{{ item.last_loss_pct != null ? ` · ${item.last_loss_pct.toFixed(0)}%` : '' }}
                  </v-chip>
                </div>
              </td>
              <td>
                <span class="text-caption text-medium-emphasis">
                  {{ item.last_sync_at ? formatDateTime(item.last_sync_at) : 'Нет синхронизации' }}
                </span>
              </td>
              <td class="text-center">
                <v-btn
                  icon="mdi-delete"
                  size="small"
                  variant="text"
                  color="error"
                  title="Удалить входную точку"
                  :loading="deletingRouteId === item.id"
                  @click="removeIngressRoute(item)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-card>
    </div>

    <!-- Вкладка 3: Сайты маскировки (HTTPS) -->
    <div v-if="activeTab === 'sites'">
      <SitesManager />
    </div>

    <!-- Модалка добавления входной точки VLESS / WireGuard -->
    <v-dialog v-model="showIngressModal" max-width="620px" :persistent="Boolean(createdIngress)">
      <v-card>
        <v-card-title class="text-h6">
          {{ createdIngress ? 'Входная точка создана — сохраните sync_token' : 'Добавить входную точку VLESS / WireGuard' }}
        </v-card-title>

        <v-card-text v-if="createdIngress">
          <v-alert type="warning" variant="tonal" class="mb-4">
            Сохраните <code>sync_token</code> сейчас: панель показывает его только один раз.
          </v-alert>

          <div class="text-body-2 mb-3">
            <strong>{{ createdIngress.route.name }}</strong>
            ({{ createdIngress.route.protocol.toUpperCase() }},
            <code>{{ endpoint(createdIngress.route.entry_host, createdIngress.route.entry_port) }} → {{ createdIngress.route.exit_host }}</code>)
          </div>

          <v-text-field
            :model-value="createdIngress.sync_token"
            label="sync_token (одноразовый показ)"
            readonly
            variant="outlined"
            append-inner-icon="mdi-content-copy"
            @click:append-inner="copySyncToken"
          />
        </v-card-text>

        <v-card-text v-else>
          <v-form @submit.prevent="submitIngressRoute">
            <v-select
              v-model="ingressForm.protocol"
              :items="[
                { title: 'VLESS (Reality / XTLS)', value: 'vless' },
                { title: 'WireGuard', value: 'wireguard' },
              ]"
              item-title="title"
              item-value="value"
              label="Протокол"
              variant="outlined"
              density="comfortable"
              class="mb-3"
            />
            <v-text-field
              v-model="ingressForm.name"
              label="Название маршрута"
              placeholder="Напр. RU-Edge → DE-Exit"
              variant="outlined"
              density="comfortable"
              class="mb-3"
            />
            <v-row dense class="mb-2">
              <v-col cols="12" sm="8">
                <v-text-field
                  v-model="ingressForm.entry_host"
                  label="Входной хост (entry_host)"
                  placeholder="203.0.113.10 или edge.example.com"
                  variant="outlined"
                  density="comfortable"
                />
              </v-col>
              <v-col cols="12" sm="4">
                <v-text-field
                  v-model.number="ingressForm.entry_port"
                  type="number"
                  min="1"
                  max="65535"
                  label="Порт входа"
                  variant="outlined"
                  density="comfortable"
                />
              </v-col>
            </v-row>
            <v-text-field
              v-model="ingressForm.exit_host"
              label="Выходной хост (exit_host)"
              placeholder="198.51.100.20 или exit.example.com"
              variant="outlined"
              density="comfortable"
              class="mb-3"
            />

            <v-row v-if="serverSelectItems.length" dense class="mb-2">
              <v-col cols="12" sm="6">
                <v-select
                  v-model="ingressForm.entry_server_id"
                  :items="serverSelectItems"
                  item-title="title"
                  item-value="value"
                  label="Entry сервер (опционально)"
                  variant="outlined"
                  density="comfortable"
                  clearable
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-select
                  v-model="ingressForm.exit_server_id"
                  :items="serverSelectItems"
                  item-title="title"
                  item-value="value"
                  label="Exit сервер (опционально)"
                  variant="outlined"
                  density="comfortable"
                  clearable
                />
              </v-col>
            </v-row>

            <v-text-field
              v-if="ingressForm.protocol === 'vless'"
              v-model="ingressForm.sni"
              label="SNI (опционально)"
              placeholder="www.microsoft.com"
              variant="outlined"
              density="comfortable"
            />

            <template v-else>
              <v-text-field
                v-model="ingressForm.wg_server_public_key"
                label="Публичный ключ WireGuard сервера (опционально)"
                variant="outlined"
                density="comfortable"
                class="mb-3"
              />
              <v-text-field
                v-model="ingressForm.wg_subnet"
                label="Подсеть WireGuard (опционально)"
                placeholder="10.66.66.0/24"
                variant="outlined"
                density="comfortable"
              />
            </template>
          </v-form>
        </v-card-text>

        <v-card-actions class="px-6 pb-4">
          <v-spacer />
          <template v-if="createdIngress">
            <v-btn variant="outlined" prepend-icon="mdi-content-copy" @click="copySyncToken">
              Скопировать токен
            </v-btn>
            <v-btn color="primary" variant="flat" @click="closeIngressModal">Готово</v-btn>
          </template>
          <template v-else>
            <v-btn variant="text" @click="closeIngressModal">Отмена</v-btn>
            <v-btn
              color="primary"
              variant="flat"
              :loading="ingressSaving"
              :disabled="!ingressReady"
              @click="submitIngressRoute"
            >
              Создать
            </v-btn>
          </template>
        </v-card-actions>
      </v-card>
    </v-dialog>

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
