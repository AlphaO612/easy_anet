<script setup lang="ts">
import { computed, ref } from 'vue'
import TopologyGraph from '@/components/TopologyGraph.vue'
import QuickLinkModal from '@/components/QuickLinkModal.vue'
import type { GraphNode, TopologyGraphResponse } from '@/models/topology'
import { formatBytes, formatHeartbeatAge } from '@/utils/format'

const topology = ref<TopologyGraphResponse | null>(null)
const graphRef = ref<{ reload: () => Promise<void> } | null>(null)
const showQuickLink = ref(false)

function onTopologyUpdated(data: TopologyGraphResponse) {
  topology.value = data
}

function onServersLinked() {
  void graphRef.value?.reload()
}

const onlineNodes = computed(
  () => topology.value?.nodes.filter((n) => n.status === 'online').length ?? 0,
)
const totalNodes = computed(() => topology.value?.nodes.length ?? 0)
const activeSessions = computed(() => topology.value?.clients_summary.active_sessions ?? 0)
const activeUsers = computed(() => topology.value?.clients_summary.active_users ?? 0)
const totalUsers = computed(() => topology.value?.clients_summary.total_users ?? 0)

const devicesByProtocol = computed(
  () => topology.value?.clients_summary.by_protocol ?? { vless: 0, wireguard: 0, anet: 0 },
)
const totalDevices = computed(
  () => devicesByProtocol.value.vless + devicesByProtocol.value.wireguard + devicesByProtocol.value.anet,
)

const totalRx = computed(
  () => topology.value?.nodes.reduce((sum, n) => sum + n.rx_bytes, 0) ?? 0,
)
const totalTx = computed(
  () => topology.value?.nodes.reduce((sum, n) => sum + n.tx_bytes, 0) ?? 0,
)

interface NodeAlert {
  node: GraphNode
  severity: 'error' | 'warning'
  reasons: string[]
}

const nodeAlerts = computed<NodeAlert[]>(() => {
  const alerts: NodeAlert[] = []
  for (const node of topology.value?.nodes ?? []) {
    const reasons: string[] = []
    if (node.status === 'offline') reasons.push('Узел офлайн')
    else if (node.status === 'draining') reasons.push('Режим дренажа (приём закрыт)')
    if (node.probe_error) reasons.push(`Ошибка пробы: ${node.probe_error}`)
    if (!reasons.length) continue
    alerts.push({
      node,
      severity: node.status === 'offline' || Boolean(node.probe_error) ? 'error' : 'warning',
      reasons,
    })
  }
  return alerts
})
</script>

<template>
  <main class="overview">
    <header class="d-flex flex-wrap justify-space-between align-center ga-3 mb-4">
      <div>
        <h1 class="text-h5 font-weight-bold">Топология и обзор сети</h1>
        <p class="text-body-2 text-medium-emphasis mb-0">
          Состояние серверов, межсерверных каналов и клиентских подключений в реальном времени
        </p>
      </div>
      <div class="d-flex flex-wrap ga-2">
        <v-btn color="success" variant="tonal" prepend-icon="mdi-link-variant-plus" @click="showQuickLink = true">
          Связать серверы
        </v-btn>
        <v-btn to="/profile-topology" variant="tonal" prepend-icon="mdi-routes">
          Маршруты
        </v-btn>
        <v-btn to="/servers?add=1" color="primary" prepend-icon="mdi-plus">
          Добавить сервер
        </v-btn>
      </div>
    </header>

    <!-- Top KPI Strip -->
    <section class="kpi-grid mb-4" aria-label="Ключевые показатели">
      <v-card class="kpi-card pa-3" border>
        <div class="kpi-label">Онлайн ноды</div>
        <div class="kpi-value">
          <span :class="onlineNodes === totalNodes && totalNodes > 0 ? 'text-success' : ''">
            {{ onlineNodes }}
          </span>
          <span class="kpi-sub"> / {{ totalNodes }}</span>
        </div>
        <div class="kpi-hint">активных узлов</div>
      </v-card>

      <v-card class="kpi-card pa-3" border>
        <div class="kpi-label">Активные сессии</div>
        <div class="kpi-value">{{ activeSessions }}</div>
        <div class="kpi-hint">текущих соединений</div>
      </v-card>

      <v-card class="kpi-card pa-3" border>
        <div class="kpi-label">Пользователи</div>
        <div class="kpi-value">
          {{ activeUsers }}<span class="kpi-sub"> / {{ totalUsers }}</span>
        </div>
        <div class="kpi-hint">активных / всего</div>
      </v-card>

      <v-card class="kpi-card pa-3" border>
        <div class="kpi-label">Устройства ({{ totalDevices }})</div>
        <div class="d-flex flex-wrap ga-1 mt-1">
          <v-chip size="x-small" color="light-blue" variant="tonal">
            VLESS {{ devicesByProtocol.vless }}
          </v-chip>
          <v-chip size="x-small" color="deep-purple-lighten-2" variant="tonal">
            WG {{ devicesByProtocol.wireguard }}
          </v-chip>
          <v-chip size="x-small" color="primary" variant="tonal">
            ANet {{ devicesByProtocol.anet }}
          </v-chip>
        </div>
      </v-card>

      <v-card class="kpi-card pa-3" border>
        <div class="kpi-label">Трафик RX / TX</div>
        <div class="kpi-traffic mt-1">
          <span>↓ {{ formatBytes(totalRx) }}</span>
          <span>↑ {{ formatBytes(totalTx) }}</span>
        </div>
        <div class="kpi-hint">суммарно по узлам</div>
      </v-card>
    </section>

    <!-- Main Topology Graph Centerpiece -->
    <TopologyGraph ref="graphRef" @updated="onTopologyUpdated" />

    <QuickLinkModal v-model="showQuickLink" @linked="onServersLinked" />

    <!-- Compact Node Alerts List -->
    <v-card v-if="nodeAlerts.length" class="mt-4" border>
      <div class="d-flex align-center ga-2 px-4 py-2 border-b">
        <v-icon icon="mdi-alert-circle-outline" color="warning" size="18" />
        <span class="text-subtitle-2 font-weight-semibold">
          Требуют внимания ({{ nodeAlerts.length }})
        </span>
      </div>
      <v-list density="compact" class="py-0">
        <v-list-item
          v-for="item in nodeAlerts"
          :key="item.node.id"
          :to="{ path: '/servers', query: { edit: item.node.id } }"
          class="alert-row"
        >
          <template #prepend>
            <v-icon
              :icon="item.severity === 'error' ? 'mdi-close-circle-outline' : 'mdi-alert-outline'"
              :color="item.severity"
              size="18"
            />
          </template>
          <v-list-item-title class="text-body-2 font-weight-medium">
            {{ item.node.name }}
            <span class="text-caption text-medium-emphasis ml-1">{{ item.node.address }}</span>
          </v-list-item-title>
          <v-list-item-subtitle class="text-caption">
            {{ item.reasons.join(' · ') }}
          </v-list-item-subtitle>
          <template #append>
            <span class="text-caption text-medium-emphasis mr-2">
              {{ formatHeartbeatAge(item.node.heartbeat_age_sec) }}
            </span>
            <v-icon icon="mdi-chevron-right" size="16" />
          </template>
        </v-list-item>
      </v-list>
    </v-card>
  </main>
</template>

<style scoped>
.overview {
  max-width: 1520px;
  margin: 0 auto;
  padding: clamp(6px, 1.5vw, 20px);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.kpi-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.kpi-label {
  font-size: 0.78rem;
  color: #9aa5a0;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.kpi-value {
  font-size: 1.55rem;
  font-weight: 700;
  line-height: 1.25;
  font-variant-numeric: tabular-nums;
}

.kpi-sub {
  font-size: 1rem;
  font-weight: 500;
  color: #9aa5a0;
}

.kpi-hint {
  font-size: 0.75rem;
  color: #7c8782;
}

.kpi-traffic {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 0.92rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.alert-row + .alert-row {
  border-top: 1px solid rgba(154, 165, 160, 0.12);
}

@media (max-width: 1180px) {
  .kpi-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 680px) {
  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
