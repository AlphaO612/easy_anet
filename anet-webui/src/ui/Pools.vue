<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { DeletePool, GetPoolPreview, GetPools } from '@/api/pools'
import { GetServers } from '@/api/servers'
import { GetUsers } from '@/api/users'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError } from '@/utils/format'
import type { NodePool, PoolPreviewResponse } from '@/models/pool'
import type { Server } from '@/models/server'
import type { User } from '@/models/user'
import PoolModal from '@/components/PoolModal.vue'

const { confirmAction } = useConfirmDialog()
const appMessage = useAppMessage()

const pools = ref<NodePool[]>([])
const servers = ref<Server[]>([])
const loading = ref(false)
const searchQuery = ref('')

const showEditor = ref(false)
const selectedPool = ref<NodePool | null>(null)

const serverById = computed(() => new Map(servers.value.map(server => [server.id, server])))

const headers = [
  { title: 'Название пула', key: 'name', sortable: true },
  { title: 'Стратегия балансировки', key: 'strategy', sortable: true },
  { title: 'Статус', key: 'is_active', sortable: true, align: 'center' as const },
  { title: 'Узлы пула (Вес)', key: 'members', sortable: false },
  { title: 'Действия', key: 'actions', sortable: false, align: 'center' as const },
]

const STRATEGY_LABELS: Record<string, string> = {
  weighted: 'Weighted rendezvous (По весам)',
  least_connections: 'Least connections (Мин. подключений)',
  latency: 'Latency (Мин. задержка)',
  speed: 'Speed (Макс. скорость)',
}

const strategyLabel = (strategy: string): string => STRATEGY_LABELS[strategy] ?? strategy

const load = async () => {
  loading.value = true
  try {
    const [poolsData, serversData] = await Promise.all([GetPools(), GetServers()])
    pools.value = poolsData
    servers.value = serversData
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось загрузить пулы'))
  } finally {
    loading.value = false
  }
}

const openCreate = () => {
  selectedPool.value = null
  showEditor.value = true
}

const openEdit = (pool: NodePool) => {
  selectedPool.value = pool
  showEditor.value = true
}

const removePool = async (pool: NodePool) => {
  const accepted = await confirmAction({
    title: 'Удалить пул?',
    message: `Вы уверены, что хотите удалить пул «${pool.name}»? Все назначения пользователей будут сброшены.`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return
  try {
    await DeletePool(pool.id)
    appMessage.success(`Пул «${pool.name}» удалён`)
    await load()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось удалить пул'))
  }
}

// === Диагностика балансировщика ===
const showDiagnostics = ref(false)
const diagnosticsPool = ref<NodePool | null>(null)
const diagnosticsLoading = ref(false)
const diagnosticsError = ref('')
const preview = ref<PoolPreviewResponse | null>(null)
const users = ref<User[]>([])
const usersLoading = ref(false)
const selectedUserId = ref<string | null>(null)

const userSelectItems = computed(() =>
  users.value.map(u => ({
    title: u.uid ? `${u.uid} (${u.id.slice(0, 8)}…)` : u.id,
    value: u.id,
  })),
)

const healthTierMeta = (tier: string): { color: string; label: string } => {
  switch (tier) {
    case 'healthy':
      return { color: 'success', label: 'Здоров' }
    case 'degraded':
      return { color: 'warning', label: 'Деградация' }
    case 'draining':
      return { color: 'orange', label: 'Drain' }
    case 'offline':
      return { color: 'error', label: 'Оффлайн' }
    case 'inactive':
      return { color: 'default', label: 'Отключён' }
    default:
      return { color: 'default', label: tier }
  }
}

const statusLabel = (status: string): { color: string; label: string } => {
  switch (status) {
    case 'online':
      return { color: 'success', label: 'Онлайн' }
    case 'offline':
      return { color: 'error', label: 'Оффлайн' }
    case 'inactive':
      return { color: 'default', label: 'Выкл' }
    default:
      return { color: 'default', label: 'Нет данных' }
  }
}

const reasonExplanation = (reason: string): string => {
  switch (reason) {
    case 'online_healthy':
      return 'Нода в сети, принимает подключения, потери пакетов в норме (< 25%)'
    case 'online_high_loss':
      return 'Нода в сети, но зафиксированы высокие потери пакетов (≥ 25%)'
    case 'no_heartbeat_yet':
      return 'Телеметрия ещё не поступала (пониженный приоритет до первого heartbeat)'
    case 'draining':
      return 'Нода в режиме drain — приём новых подключений закрыт'
    case 'offline':
      return 'Нода оффлайн — heartbeat устарел'
    case 'server_inactive':
      return 'Сервер отключён в настройках (is_active = false)'
    default:
      return reason
  }
}

const formatMetrics = (latency?: number | null, loss?: number | null, mbps?: number | null): string => {
  const rtt = latency != null ? `${latency.toFixed(1)} мс` : '—'
  const lossStr = loss != null ? `${loss.toFixed(1)}%` : '—'
  const speedStr = mbps != null ? `${mbps.toFixed(1)} Мбит/с` : '—'
  return `${rtt} / ${lossStr} / ${speedStr}`
}

const loadPreview = async () => {
  if (!diagnosticsPool.value) return
  diagnosticsLoading.value = true
  diagnosticsError.value = ''
  try {
    preview.value = await GetPoolPreview(diagnosticsPool.value.id, selectedUserId.value || undefined)
  } catch (err) {
    diagnosticsError.value = extractApiError(err, 'Не удалось получить диагностику балансировщика')
    preview.value = null
  } finally {
    diagnosticsLoading.value = false
  }
}

const ensureUsersLoaded = async () => {
  if (users.value.length || usersLoading.value) return
  usersLoading.value = true
  try {
    const res = await GetUsers(0, 100)
    users.value = res.items
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось загрузить список пользователей'))
  } finally {
    usersLoading.value = false
  }
}

const openDiagnostics = async (pool: NodePool) => {
  diagnosticsPool.value = pool
  selectedUserId.value = null
  preview.value = null
  diagnosticsError.value = ''
  showDiagnostics.value = true
  await Promise.all([ensureUsersLoaded(), loadPreview()])
}

watch(selectedUserId, () => {
  if (showDiagnostics.value && diagnosticsPool.value) {
    loadPreview()
  }
})

const route = useRoute()
const initialMember = ref('')
onMounted(async () => {
  await load()
  const member = servers.value.find(node => node.id === route.query.member)
  if (member) {
    initialMember.value = member.id
    openCreate()
  }
})
</script>

<template>
  <v-container fluid class="pools-page">
    <div class="d-flex justify-space-between align-center flex-wrap ga-4 mb-4">
      <v-list-item
        class="px-0"
        subtitle="Группы узлов и стратегии выбора entry point (Weighted / Least connections / Latency / Speed)"
      >
        <template #title>
          <h1 class="text-h5 font-weight-bold">Node Pools (Пулы серверов)</h1>
        </template>
      </v-list-item>
    </div>

    <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-4">
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        label="Поиск по пулам..."
        variant="outlined"
        density="compact"
        hide-details
        single-line
        style="max-width: 280px"
      />
      <v-btn color="primary" prepend-icon="mdi-plus" @click="openCreate">
        + ДОБАВИТЬ ПУЛ
      </v-btn>
    </div>

    <v-data-table
      :headers="headers"
      :items="pools"
      :search="searchQuery"
      :loading="loading"
      :items-per-page="10"
      :items-per-page-options="[10, 20, 50]"
      items-per-page-text="Items per page:"
      loading-text="Загрузка пулов балансировки…"
      no-data-text="Пулов пока нет — создайте первый!"
      density="comfortable"
      class="border rounded-lg"
      hover
      @click:row="(_: unknown, data: { item: NodePool }) => openEdit(data.item)"
    >
      <template #item.name="{ item }">
        <span class="font-weight-bold">{{ item.name }}</span>
      </template>

      <template #item.strategy="{ item }">
        <v-chip size="small" variant="outlined">
          {{ strategyLabel(item.strategy) }}
        </v-chip>
      </template>

      <template #item.is_active="{ item }">
        <v-chip :color="item.is_active ? 'success' : 'default'" size="small">
          {{ item.is_active ? 'ACTIVE' : 'OFF' }}
        </v-chip>
      </template>

      <template #item.members="{ item }">
        <div class="d-flex flex-wrap ga-1">
          <v-chip
            v-for="(member, idx) in item.members"
            :key="`${member.server_id}-${idx}`"
            size="small"
            variant="tonal"
            color="secondary"
          >
            {{ serverById.get(member.server_id)?.name || member.server_id }}
            <span v-if="member.protocol" class="ml-1 font-weight-bold text-uppercase">[{{ member.protocol }}<template v-if="member.wrtc_mode">: {{ member.wrtc_mode }}</template>]</span>
            <span v-if="member.port_or_url" class="ml-1 text-medium-emphasis">({{ member.port_or_url }})</span>
            <strong class="ml-1 text-primary">w{{ member.weight }}</strong>
          </v-chip>
          <span v-if="!item.members.length" class="text-caption text-medium-emphasis">—</span>
        </div>
      </template>

      <template #item.actions="{ item }">
        <div class="d-flex ga-1 justify-center flex-nowrap" @click.stop>
          <v-btn
            icon="mdi-tune-variant"
            size="small"
            variant="text"
            color="primary"
            title="Диагностика балансировщика"
            @click="openDiagnostics(item)"
          />
          <v-btn
            icon="mdi-pencil"
            size="small"
            variant="text"
            title="Редактировать пул"
            @click="openEdit(item)"
          />
          <v-btn
            icon="mdi-delete"
            size="small"
            variant="text"
            color="error"
            title="Удалить пул"
            @click="removePool(item)"
          />
        </div>
      </template>
    </v-data-table>

    <PoolModal
      v-model="showEditor"
      :pool="selectedPool"
      :initial-member="initialMember"
      @update:model-value="value => { if (!value) initialMember = '' }"
      :servers="servers"
      @saved="load"
    />

    <!-- Диалог диагностики балансировщика -->
    <v-dialog v-model="showDiagnostics" max-width="1100px">
      <v-card>
        <v-card-title class="d-flex align-center justify-space-between flex-wrap ga-2">
          <div>
            <div class="text-h6">Диагностика балансировщика</div>
            <div v-if="diagnosticsPool" class="text-caption text-medium-emphasis">
              Пул: <strong>{{ preview?.pool_name || diagnosticsPool.name }}</strong> ·
              Стратегия: <strong>{{ strategyLabel(preview?.strategy || diagnosticsPool.strategy) }}</strong>
            </div>
          </div>
          <v-btn icon="mdi-close" variant="text" size="small" @click="showDiagnostics = false" />
        </v-card-title>

        <v-card-text>
          <div class="d-flex align-center flex-wrap ga-3 mb-4">
            <v-autocomplete
              v-model="selectedUserId"
              :items="userSelectItems"
              :loading="usersLoading"
              item-title="title"
              item-value="value"
              label="Пользователь (опционально для расчёта rendezvous-хэша)"
              placeholder="Общий порядок без привязки к пользователю"
              variant="outlined"
              density="compact"
              clearable
              hide-details
              style="min-width: 320px; flex: 1"
            />
            <v-btn
              variant="outlined"
              prepend-icon="mdi-refresh"
              :loading="diagnosticsLoading"
              @click="loadPreview"
            >
              Пересчитать
            </v-btn>
          </div>

          <v-alert v-if="diagnosticsError" type="error" variant="tonal" class="mb-3">
            {{ diagnosticsError }}
          </v-alert>

          <v-progress-linear v-if="diagnosticsLoading" indeterminate color="primary" class="mb-2" />

          <v-table density="compact" class="border rounded-lg">
            <thead>
              <tr>
                <th>#</th>
                <th>Сервер</th>
                <th>Протокол / Порт</th>
                <th class="text-end">Вес</th>
                <th class="text-center">Health tier</th>
                <th class="text-center">Статус</th>
                <th class="text-center">Приём</th>
                <th class="text-end">Подкл.</th>
                <th>RTT / Потери / Мбит/с</th>
                <th>Пояснение</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!diagnosticsLoading && (!preview || !preview.candidates.length)">
                <td colspan="10" class="text-center text-medium-emphasis py-6">
                  В пуле нет кандидатов для расчёта.
                </td>
              </tr>
              <tr v-for="c in preview?.candidates ?? []" :key="`${c.rank}-${c.server_id}-${c.protocol}`">
                <td class="font-weight-bold">#{{ c.rank }}</td>
                <td>
                  <div class="font-weight-medium">{{ c.server_name }}</div>
                  <div class="text-caption text-medium-emphasis"><code>{{ c.address }}</code></div>
                </td>
                <td>
                  <span class="font-weight-bold text-uppercase">{{ c.protocol }}</span>
                  <div v-if="c.port_or_url" class="text-caption text-medium-emphasis">{{ c.port_or_url }}</div>
                </td>
                <td class="text-end"><strong>w{{ c.weight }}</strong></td>
                <td class="text-center">
                  <v-chip size="x-small" :color="healthTierMeta(c.health_tier).color" variant="tonal">
                    {{ healthTierMeta(c.health_tier).label }}
                  </v-chip>
                </td>
                <td class="text-center">
                  <v-chip size="x-small" :color="statusLabel(c.status).color" variant="outlined">
                    {{ statusLabel(c.status).label }}
                  </v-chip>
                </td>
                <td class="text-center">
                  <v-chip size="x-small" :color="c.accepting_connections ? 'success' : 'warning'" variant="tonal">
                    {{ c.accepting_connections ? 'OPEN' : 'CLOSED' }}
                  </v-chip>
                </td>
                <td class="text-end">{{ c.active_connections }}</td>
                <td class="text-caption text-no-wrap">
                  {{ formatMetrics(c.latency_ms, c.loss_pct, c.throughput_mbps) }}
                </td>
                <td class="text-caption">{{ reasonExplanation(c.reason) }}</td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>

        <v-card-actions class="px-6 pb-4">
          <v-spacer />
          <v-btn variant="flat" color="primary" @click="showDiagnostics = false">Закрыть</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-container>
</template>

<style scoped>
.pools-page {
  padding: 24px;
}
</style>
