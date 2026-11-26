<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { GetTopologyGraph } from '@/api/topology'
import { DeleteServer, ResetServerState, SetNodeAdmission } from '@/api/servers'
import { useAppMessage } from '@/composables/useAppMessage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import type {
  GraphEdge,
  GraphEdgeStatus,
  GraphNode,
  GraphNodeStatus,
  TopologyGraphResponse,
} from '@/models/topology'
import {
  extractApiError,
  formatBytes,
  formatDateTime,
  formatHeartbeatAge,
  formatUptime,
} from '@/utils/format'

const emit = defineEmits<{ updated: [data: TopologyGraphResponse] }>()

const message = useAppMessage()
const { confirmAction } = useConfirmDialog()

const graph = ref<TopologyGraphResponse | null>(null)
const loading = ref(false)
const error = ref('')
const autoRefresh = ref(true)
const zoom = ref(1)
const selectedUserId = ref<string | null>(null)
const selected = ref<{ kind: 'node'; id: string } | { kind: 'edge'; id: string } | null>(null)
const actionBusy = ref(false)

let timer: number | undefined
let inFlight = false

const NODE_W = 250
const NODE_H = 142
const NODE_GAP = 24
const BUBBLE_W = 152
const BUBBLE_H = 68
const BUBBLE_GAP = 18
const HEADER_H = 52
const SVG_W = 1244

const X_CLIENTS = 24
const X_ENTRY = 264
const X_EXIT = 724
const X_CLOUD = 1068

const NODE_STATUS_META: Record<GraphNodeStatus, { color: string; vuetify: string; label: string }> = {
  online: { color: '#10b981', vuetify: 'success', label: 'Онлайн' },
  draining: { color: '#f59e0b', vuetify: 'warning', label: 'Дренаж' },
  offline: { color: '#ef4444', vuetify: 'error', label: 'Офлайн' },
  unknown: { color: '#64748b', vuetify: 'default', label: 'Неизвестно' },
}

const EDGE_STATUS_META: Record<GraphEdgeStatus, { color: string; vuetify: string; label: string; dashed: boolean }> = {
  up: { color: '#10b981', vuetify: 'success', label: 'Активен', dashed: false },
  degraded: { color: '#f59e0b', vuetify: 'warning', label: 'Деградация', dashed: false },
  down: { color: '#ef4444', vuetify: 'error', label: 'Недоступен', dashed: true },
  unknown: { color: '#64748b', vuetify: 'default', label: 'Нет данных', dashed: true },
}

async function loadGraph(silent = false) {
  if (inFlight) return
  inFlight = true
  if (!silent) loading.value = true
  try {
    const data = await GetTopologyGraph()
    graph.value = data
    error.value = ''
    emit('updated', data)
    if (selectedUserId.value && !data.user_paths.some((u) => u.user_id === selectedUserId.value)) {
      selectedUserId.value = null
    }
    if (selected.value) {
      const current = selected.value
      const exists =
        current.kind === 'node'
          ? data.nodes.some((n) => n.id === current.id)
          : data.edges.some((e) => e.id === current.id)
      if (!exists) selected.value = null
    }
  } catch (err) {
    error.value = extractApiError(err, 'Не удалось загрузить топологию сети')
  } finally {
    inFlight = false
    loading.value = false
  }
}

function syncTimer() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
  if (autoRefresh.value) {
    timer = window.setInterval(() => {
      void loadGraph(true)
    }, 5000)
  }
}

watch(autoRefresh, syncTimer)

onMounted(() => {
  void loadGraph()
  syncTimer()
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})

function zoomIn() {
  zoom.value = Math.min(1.75, Math.round((zoom.value + 0.15) * 100) / 100)
}

function zoomOut() {
  zoom.value = Math.max(0.5, Math.round((zoom.value - 0.15) * 100) / 100)
}

function zoomReset() {
  zoom.value = 1
}

const selectedUserPath = computed(() => {
  if (!selectedUserId.value || !graph.value) return null
  return graph.value.user_paths.find((u) => u.user_id === selectedUserId.value) ?? null
})

const userSelectItems = computed(() =>
  (graph.value?.user_paths ?? []).map((u) => ({
    value: u.user_id,
    title: u.uid,
    subtitle: `${u.protocols.map((p) => p.toUpperCase()).join(', ') || '—'} · ↓${formatBytes(u.rx_bytes)} ↑${formatBytes(u.tx_bytes)}`,
    active: u.is_active,
  })),
)

const selectedNode = computed<GraphNode | null>(() => {
  if (selected.value?.kind !== 'node' || !graph.value) return null
  return graph.value.nodes.find((n) => n.id === selected.value?.id) ?? null
})

const selectedEdge = computed<GraphEdge | null>(() => {
  if (selected.value?.kind !== 'edge' || !graph.value) return null
  return graph.value.edges.find((e) => e.id === selected.value?.id) ?? null
})

const nodeById = computed(() => {
  const map = new Map<string, GraphNode>()
  for (const n of graph.value?.nodes ?? []) map.set(n.id, n)
  return map
})

interface PositionedNode {
  node: GraphNode
  x: number
  y: number
  cx: number
  cy: number
}

interface ClientBubble {
  id: 'vless' | 'wireguard' | 'anet'
  label: string
  count: number
  color: string
  x: number
  y: number
  cy: number
}

interface RenderedEdge {
  edge: GraphEdge
  path: string
  mx: number
  my: number
  pillW: number
  metricsText: string
  color: string
  dashed: boolean
  flowing: boolean
  packets: { begin: string; dur: string }[]
}

interface SyntheticLink {
  id: string
  path: string
  color: string
  dashed: boolean
  flowing: boolean
  dur: string
  dimmed: boolean
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}

function formatEdgeMetrics(edge: GraphEdge): string {
  const parts: string[] = []
  if (edge.latency_ms != null) parts.push(`${Math.round(edge.latency_ms)}мс`)
  if (edge.loss_pct != null) parts.push(`${Number(edge.loss_pct.toFixed(1))}%`)
  if (edge.throughput_mbps != null) parts.push(`${Number(edge.throughput_mbps.toFixed(1))} Мбит/с`)
  return parts.length ? parts.join(' · ') : 'Нет пробы'
}

function buildPacketSchedule(trafficBytes: number, throughputMbps?: number | null) {
  const effectiveTraffic = Math.max(trafficBytes, (throughputMbps ?? 0) * 125_000)
  const count = effectiveTraffic > 200_000_000 ? 3 : effectiveTraffic > 2_000_000 ? 2 : 1
  const logFactor = Math.min(1, Math.log10(effectiveTraffic + 10) / 10)
  const durSec = Number((4.0 - logFactor * 2.5).toFixed(2))
  return Array.from({ length: count }, (_, idx) => ({
    dur: `${durSec}s`,
    begin: `${((idx * durSec) / count).toFixed(2)}s`,
  }))
}

const layout = computed(() => {
  const nodes = graph.value?.nodes ?? []
  const edges = graph.value?.edges ?? []
  const byProto = graph.value?.clients_summary.by_protocol ?? { vless: 0, wireguard: 0, anet: 0 }

  const entryNodes = nodes.filter((n) => n.role === 'entry' || n.role === 'both')
  const exitNodes = nodes.filter((n) => n.role === 'exit')

  const clientsH = 3 * BUBBLE_H + 2 * BUBBLE_GAP
  const entryH = Math.max(1, entryNodes.length) * NODE_H + Math.max(0, entryNodes.length - 1) * NODE_GAP
  const exitH = Math.max(1, exitNodes.length) * NODE_H + Math.max(0, exitNodes.length - 1) * NODE_GAP
  const contentH = Math.max(300, clientsH, entryH, exitH)
  const svgH = HEADER_H + contentH + 34

  const placeColumn = (list: GraphNode[], x: number): PositionedNode[] => {
    const colH = list.length * NODE_H + Math.max(0, list.length - 1) * NODE_GAP
    const startY = HEADER_H + (contentH - colH) / 2
    return list.map((node, i) => {
      const y = startY + i * (NODE_H + NODE_GAP)
      return { node, x, y, cx: x + NODE_W / 2, cy: y + NODE_H / 2 }
    })
  }

  const placedEntry = placeColumn(entryNodes, X_ENTRY)
  const placedExit = placeColumn(exitNodes, X_EXIT)
  const positionedNodes = [...placedEntry, ...placedExit]
  const posMap = new Map(positionedNodes.map((p) => [p.node.id, p]))

  const bubbleStartY = HEADER_H + (contentH - clientsH) / 2
  const bubbleDefs: { id: 'vless' | 'wireguard' | 'anet'; label: string; count: number; color: string }[] = [
    { id: 'vless', label: 'VLESS', count: byProto.vless, color: '#38bdf8' },
    { id: 'wireguard', label: 'WireGuard', count: byProto.wireguard, color: '#a78bfa' },
    { id: 'anet', label: 'ANet', count: byProto.anet, color: '#2bb894' },
  ]
  const bubbles: ClientBubble[] = bubbleDefs.map((b, i) => {
    const y = bubbleStartY + i * (BUBBLE_H + BUBBLE_GAP)
    return { ...b, x: X_CLIENTS, y, cy: y + BUBBLE_H / 2 }
  })

  const cloud = {
    x: X_CLOUD,
    y: HEADER_H + (contentH - 112) / 2,
    w: 152,
    h: 112,
    cy: HEADER_H + contentH / 2,
  }

  // Group edges between same pair so parallel edges bend cleanly apart
  const pairGroups = new Map<string, GraphEdge[]>()
  for (const edge of edges) {
    const pairKey = [edge.source, edge.target].sort().join('::')
    const arr = pairGroups.get(pairKey) ?? []
    arr.push(edge)
    pairGroups.set(pairKey, arr)
  }

  const renderedEdges: RenderedEdge[] = []
  for (const edge of edges) {
    const src = posMap.get(edge.source)
    const dst = posMap.get(edge.target)
    if (!src || !dst) continue

    const pairKey = [edge.source, edge.target].sort().join('::')
    const siblings = pairGroups.get(pairKey) ?? [edge]
    const idx = siblings.indexOf(edge)
    const offset = (idx - (siblings.length - 1) / 2) * 58

    let sx: number
    let sy: number
    let tx: number
    let ty: number
    let cx1: number
    let cy1: number
    let cx2: number
    let cy2: number

    if (src.x < dst.x) {
      sx = src.x + NODE_W
      sy = src.cy
      tx = dst.x
      ty = dst.cy
      const dx = (tx - sx) * 0.45
      cx1 = sx + dx
      cy1 = sy + offset
      cx2 = tx - dx
      cy2 = ty + offset
    } else if (src.x > dst.x) {
      sx = src.x
      sy = src.cy
      tx = dst.x + NODE_W
      ty = dst.cy
      const dx = (sx - tx) * 0.45
      cx1 = sx - dx
      cy1 = sy + offset
      cx2 = tx + dx
      cy2 = ty + offset
    } else {
      const bend = 92 + Math.abs(offset)
      sx = src.x + NODE_W
      sy = src.cy
      tx = dst.x + NODE_W
      ty = dst.cy
      cx1 = sx + bend
      cy1 = sy + offset * 0.5
      cx2 = tx + bend
      cy2 = ty + offset * 0.5
    }

    const path = `M ${sx} ${sy} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${tx} ${ty}`
    const mx = 0.125 * sx + 0.375 * cx1 + 0.375 * cx2 + 0.125 * tx
    const my = 0.125 * sy + 0.375 * cy1 + 0.375 * cy2 + 0.125 * ty

    const meta = EDGE_STATUS_META[edge.status] ?? EDGE_STATUS_META.unknown
    const metricsText = formatEdgeMetrics(edge)
    const topLen = edge.transport.length + 2
    const pillW = Math.max(114, Math.min(194, Math.max(topLen, metricsText.length) * 6.5 + 22))
    const traffic = edge.rx_bytes + edge.tx_bytes
    const endpointsUp = src.node.status !== 'offline' && dst.node.status !== 'offline'
    const flowing =
      endpointsUp &&
      edge.status !== 'down' &&
      (edge.status === 'up' || edge.status === 'degraded' || traffic > 0)

    renderedEdges.push({
      edge,
      path,
      mx,
      my,
      pillW,
      metricsText,
      color: meta.color,
      dashed: meta.dashed,
      flowing,
      packets: flowing ? buildPacketSchedule(traffic, edge.throughput_mbps) : [],
    })
  }

  // Synthetic links: Clients -> Entry/Both
  const userPath = selectedUserPath.value
  const syntheticLinks: SyntheticLink[] = []
  const activeSessions = graph.value?.clients_summary.active_sessions ?? 0

  for (const bubble of bubbles) {
    for (const entry of placedEntry) {
      const sx = bubble.x + BUBBLE_W
      const sy = bubble.cy
      const tx = entry.x
      const ty = entry.cy
      const dx = (tx - sx) * 0.5
      const path = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`
      const protoActive = bubble.count > 0
      const nodeUp = entry.node.status !== 'offline'
      const flowing = protoActive && nodeUp && (activeSessions > 0 || entry.node.active_connections > 0)
      const protoMatch = !userPath || userPath.protocols.some((p) => p.toLowerCase() === bubble.id)
      const nodeMatch = !userPath || userPath.node_ids.includes(entry.node.id)
      syntheticLinks.push({
        id: `c-${bubble.id}-${entry.node.id}`,
        path,
        color: bubble.color,
        dashed: !protoActive || !nodeUp,
        flowing,
        dur: '3.2s',
        dimmed: !(protoMatch && nodeMatch),
      })
    }
  }

  // Synthetic links: Exit (and standalone Both) -> Internet cloud
  const nodesWithExitEdge = new Set(edges.map((e) => e.source))
  const egressNodes =
    placedExit.length > 0
      ? [
          ...placedExit,
          ...placedEntry.filter((p) => p.node.role === 'both' && !nodesWithExitEdge.has(p.node.id)),
        ]
      : placedEntry.filter((p) => p.node.role === 'both' || p.node.role === 'exit')

  for (const egress of egressNodes) {
    const sx = egress.x + NODE_W
    const sy = egress.cy
    const tx = cloud.x
    const ty = cloud.cy
    const dx = (tx - sx) * 0.48
    const path = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`
    const nodeUp = egress.node.status !== 'offline'
    const flowing = nodeUp && (egress.node.active_connections > 0 || egress.node.rx_bytes + egress.node.tx_bytes > 0)
    const nodeMatch = !userPath || userPath.node_ids.includes(egress.node.id)
    syntheticLinks.push({
      id: `egress-${egress.node.id}`,
      path,
      color: nodeUp ? '#10b981' : '#ef4444',
      dashed: !nodeUp,
      flowing,
      dur: '2.8s',
      dimmed: !nodeMatch,
    })
  }

  return {
    svgH,
    bubbles,
    positionedNodes,
    renderedEdges,
    syntheticLinks,
    cloud,
    hasEntry: placedEntry.length > 0,
    hasExit: placedExit.length > 0,
  }
})

function isNodeDimmed(nodeId: string): boolean {
  if (!selectedUserPath.value) return false
  return !selectedUserPath.value.node_ids.includes(nodeId)
}

function isEdgeDimmed(edgeId: string): boolean {
  if (!selectedUserPath.value) return false
  return !selectedUserPath.value.edge_ids.includes(edgeId)
}

function isBubbleDimmed(proto: string): boolean {
  if (!selectedUserPath.value) return false
  return !selectedUserPath.value.protocols.some((p) => p.toLowerCase() === proto.toLowerCase())
}

function isCloudDimmed(): boolean {
  if (!selectedUserPath.value) return false
  return selectedUserPath.value.node_ids.length === 0
}

function roleLabel(role: GraphNode['role']): string {
  if (role === 'entry') return 'ENTRY'
  if (role === 'exit') return 'EXIT'
  return 'BOTH'
}

async function handleToggleAdmission(node: GraphNode) {
  actionBusy.value = true
  try {
    const nextState = !node.accepting_connections
    await SetNodeAdmission(node.id, nextState)
    message.success(
      nextState
        ? `Приём подключений на «${node.name}» возобновлён`
        : `Узел «${node.name}» переведён в режим дренажа`,
    )
    await loadGraph(true)
  } catch (err) {
    message.error(extractApiError(err, 'Не удалось изменить режим приёма подключений'))
  } finally {
    actionBusy.value = false
  }
}

async function handleResetState(node: GraphNode) {
  const confirmed = await confirmAction({
    title: 'Сбросить состояние узла?',
    message: `Сбросить сохранённое runtime-состояние и ревизию проб для «${node.name}»?`,
    confirmText: 'Сбросить',
    confirmColor: 'warning',
  })
  if (!confirmed) return
  actionBusy.value = true
  try {
    await ResetServerState(node.id)
    message.success(`Состояние узла «${node.name}» сброшено`)
    await loadGraph(true)
  } catch (err) {
    message.error(extractApiError(err, 'Не удалось сбросить состояние узла'))
  } finally {
    actionBusy.value = false
  }
}

async function handleDeleteNode(node: GraphNode) {
  const confirmed = await confirmAction({
    title: 'Удалить сервер?',
    message: `Узел «${node.name}» (${node.address}) будет безвозвратно удалён из панели.`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!confirmed) return
  actionBusy.value = true
  try {
    await DeleteServer(node.id)
    message.success(`Сервер «${node.name}» удалён`)
    selected.value = null
    await loadGraph(true)
  } catch (err) {
    message.error(extractApiError(err, 'Не удалось удалить сервер'))
  } finally {
    actionBusy.value = false
  }
}
</script>

<template>
  <v-card class="topology-card" border>
    <div class="topology-toolbar d-flex flex-wrap align-center ga-3 px-4 py-3">
      <div class="d-flex align-center ga-2">
        <v-icon icon="mdi-graph-outline" color="primary" size="20" />
        <span class="text-subtitle-1 font-weight-semibold">Живая топология сети</span>
        <v-chip v-if="graph" size="x-small" variant="tonal" color="primary">
          {{ graph.nodes.length }} узлов · {{ graph.edges.length }} каналов
        </v-chip>
      </div>

      <v-spacer />

      <v-select
        v-model="selectedUserId"
        :items="userSelectItems"
        item-title="title"
        item-value="value"
        label="Подсветить пользователя"
        density="compact"
        variant="outlined"
        clearable
        hide-details
        prepend-inner-icon="mdi-account-search-outline"
        class="user-select"
      >
        <template #item="{ props: itemProps, item }">
          <v-list-item v-bind="itemProps" :subtitle="item.raw.subtitle">
            <template #append>
              <v-chip size="x-small" :color="item.raw.active ? 'success' : 'default'" variant="tonal">
                {{ item.raw.active ? 'активен' : 'неакт.' }}
              </v-chip>
            </template>
          </v-list-item>
        </template>
      </v-select>

      <v-btn-group density="compact" variant="outlined" divided>
        <v-btn icon="mdi-minus" size="small" :disabled="zoom <= 0.5" title="Уменьшить" @click="zoomOut" />
        <v-btn size="small" class="zoom-label" title="Сбросить масштаб" @click="zoomReset">
          {{ Math.round(zoom * 100) }}%
        </v-btn>
        <v-btn icon="mdi-plus" size="small" :disabled="zoom >= 1.75" title="Увеличить" @click="zoomIn" />
      </v-btn-group>

      <v-switch
        v-model="autoRefresh"
        label="Авто 5с"
        color="primary"
        density="compact"
        hide-details
        class="auto-switch"
      />

      <v-btn
        size="small"
        variant="tonal"
        prepend-icon="mdi-refresh"
        :loading="loading"
        @click="loadGraph()"
      >
        Обновить
      </v-btn>
    </div>

    <v-divider />

    <v-alert v-if="error" type="error" variant="tonal" density="compact" class="ma-3 mb-0">
      {{ error }}
    </v-alert>

    <div class="topology-body">
      <div class="canvas-wrap" @click.self="selected = null">
        <div v-if="loading && !graph" class="canvas-empty">
          <v-progress-circular indeterminate color="primary" size="36" />
          <span class="text-body-2 text-medium-emphasis mt-2">Загрузка топологии…</span>
        </div>

        <svg
          v-else
          :viewBox="`0 0 ${SVG_W} ${layout.svgH}`"
          :width="Math.round(SVG_W * zoom)"
          :height="Math.round(layout.svgH * zoom)"
          class="topology-svg"
          role="img"
          aria-label="Граф топологии VPN"
          @click.self="selected = null"
        >
          <defs>
            <pattern id="topo-grid" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="1" fill="rgba(154,165,160,0.12)" />
            </pattern>
            <linearGradient id="cloud-grad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#1e2d29" />
              <stop offset="100%" stop-color="#151b1a" />
            </linearGradient>
          </defs>

          <rect width="100%" height="100%" fill="url(#topo-grid)" @click="selected = null" />

          <!-- Column Headers -->
          <g class="layer-headers">
            <text :x="X_CLIENTS + BUBBLE_W / 2" y="28" text-anchor="middle">КЛИЕНТЫ</text>
            <text :x="X_ENTRY + NODE_W / 2" y="28" text-anchor="middle">ENTRY VPS</text>
            <text :x="X_EXIT + NODE_W / 2" y="28" text-anchor="middle">EXIT VPS</text>
            <text :x="X_CLOUD + layout.cloud.w / 2" y="28" text-anchor="middle">ИНТЕРНЕТ</text>
          </g>

          <!-- Synthetic Links (Clients -> Entry & Exit/Both -> Internet) -->
          <g class="synthetic-links">
            <g
              v-for="link in layout.syntheticLinks"
              :key="link.id"
              :opacity="link.dimmed ? 0.2 : 0.72"
              class="fade-el"
            >
              <path
                :d="link.path"
                fill="none"
                :stroke="link.color"
                stroke-width="1.6"
                :stroke-dasharray="link.dashed ? '4 5' : undefined"
              />
              <circle v-if="link.flowing" r="2.8" :fill="link.color">
                <animateMotion :dur="link.dur" repeatCount="indefinite" :path="link.path" />
              </circle>
            </g>
          </g>

          <!-- Real Inter-Node Bezier Edges -->
          <g class="graph-edges">
            <g
              v-for="item in layout.renderedEdges"
              :key="item.edge.id"
              :opacity="isEdgeDimmed(item.edge.id) ? 0.2 : 1"
              class="edge-group fade-el"
              @click.stop="selected = { kind: 'edge', id: item.edge.id }"
            >
              <!-- Invisible wider hit target -->
              <path :d="item.path" fill="none" stroke="transparent" stroke-width="18" />

              <path
                :d="item.path"
                fill="none"
                :stroke="item.color"
                :stroke-width="selected?.kind === 'edge' && selected.id === item.edge.id ? 3.5 : 2.3"
                :stroke-dasharray="item.dashed ? '6 5' : undefined"
                stroke-linecap="round"
              />

              <!-- Animated traffic packets (strictly stopped when edge down or endpoint offline) -->
              <template v-if="item.flowing">
                <circle
                  v-for="(pkt, pIdx) in item.packets"
                  :key="`${item.edge.id}-${item.path}-${pIdx}`"
                  r="3.6"
                  :fill="item.color"
                  class="packet-dot"
                >
                  <animateMotion
                    :dur="pkt.dur"
                    :begin="pkt.begin"
                    repeatCount="indefinite"
                    :path="item.path"
                  />
                </circle>
              </template>

              <!-- Midpoint Pill -->
              <g :transform="`translate(${item.mx - item.pillW / 2}, ${item.my - 20})`">
                <rect
                  :width="item.pillW"
                  height="40"
                  rx="10"
                  fill="#141817"
                  :stroke="selected?.kind === 'edge' && selected.id === item.edge.id ? '#e8ebea' : item.color"
                  :stroke-width="selected?.kind === 'edge' && selected.id === item.edge.id ? 1.8 : 1.1"
                />
                <text
                  :x="item.pillW / 2"
                  y="15"
                  text-anchor="middle"
                  font-size="10"
                  font-weight="700"
                  :fill="item.color"
                >
                  {{ item.edge.transport.toUpperCase() }}
                </text>
                <text
                  :x="item.pillW / 2"
                  y="31"
                  text-anchor="middle"
                  font-size="10.5"
                  fill="#d5dcd8"
                >
                  {{ item.metricsText }}
                </text>
              </g>
            </g>
          </g>

          <!-- Layer 1: Client Protocol Bubbles -->
          <g class="client-bubbles">
            <g
              v-for="b in layout.bubbles"
              :key="b.id"
              :transform="`translate(${b.x}, ${b.y})`"
              :opacity="isBubbleDimmed(b.id) ? 0.2 : 1"
              class="fade-el"
            >
              <rect
                :width="BUBBLE_W"
                :height="BUBBLE_H"
                rx="16"
                fill="#181d1c"
                :stroke="b.count > 0 ? b.color : '#2e3533'"
                stroke-width="1.3"
              />
              <circle cx="22" cy="24" r="5" :fill="b.count > 0 ? b.color : '#64748b'" />
              <text x="35" y="28" font-size="13" font-weight="650" fill="#e8ebea">{{ b.label }}</text>
              <text x="22" y="50" font-size="11.5" fill="#9aa5a0">
                Устройств:
                <tspan font-weight="700" fill="#e8ebea">{{ b.count }}</tspan>
              </text>
            </g>
          </g>

          <!-- Empty Column Hints -->
          <g v-if="!layout.hasEntry" :transform="`translate(${X_ENTRY}, ${HEADER_H + 80})`">
            <rect :width="NODE_W" height="90" rx="12" fill="none" stroke="#333b39" stroke-dasharray="5 5" />
            <text :x="NODE_W / 2" y="50" text-anchor="middle" font-size="12" fill="#7c8782">
              Нет Entry-узлов
            </text>
          </g>

          <g v-if="!layout.hasExit" :transform="`translate(${X_EXIT}, ${HEADER_H + 80})`">
            <rect :width="NODE_W" height="90" rx="12" fill="none" stroke="#333b39" stroke-dasharray="5 5" />
            <text :x="NODE_W / 2" y="44" text-anchor="middle" font-size="12" fill="#7c8782">
              Нет выделенных Exit-узлов
            </text>
            <text :x="NODE_W / 2" y="63" text-anchor="middle" font-size="10.5" fill="#636d68">
              Выход через узлы BOTH
            </text>
          </g>

          <!-- Layer 2 & 3: VPS Node Cards -->
          <g class="vps-nodes">
            <g
              v-for="item in layout.positionedNodes"
              :key="item.node.id"
              :transform="`translate(${item.x}, ${item.y})`"
              :opacity="isNodeDimmed(item.node.id) ? 0.2 : 1"
              class="node-card fade-el"
              @click.stop="selected = { kind: 'node', id: item.node.id }"
            >
              <rect
                :width="NODE_W"
                :height="NODE_H"
                rx="12"
                fill="#1a1f1e"
                :stroke="
                  selected?.kind === 'node' && selected.id === item.node.id
                    ? '#2bb894'
                    : item.node.probe_error
                      ? '#ef4444'
                      : '#343b39'
                "
                :stroke-width="selected?.kind === 'node' && selected.id === item.node.id ? 2.2 : 1.2"
              />

              <!-- Live status dot (+ pulse when online) -->
              <circle
                v-if="item.node.status === 'online'"
                cx="20"
                cy="22"
                r="5"
                fill="#10b981"
                class="pulse-ring"
              />
              <circle
                cx="20"
                cy="22"
                r="5"
                :fill="NODE_STATUS_META[item.node.status].color"
              />

              <!-- Name & Role -->
              <text x="33" y="26" font-size="13.5" font-weight="650" fill="#e8ebea">
                {{ truncate(item.node.name, 17) }}
              </text>
              <rect x="192" y="12" width="46" height="19" rx="6" fill="#252c2a" stroke="#39423f" />
              <text x="215" y="25" text-anchor="middle" font-size="9.5" font-weight="700" fill="#9aa5a0">
                {{ roleLabel(item.node.role) }}
              </text>

              <!-- Address & admission flag -->
              <text x="15" y="45" font-size="11" fill="#9aa5a0" font-family="monospace">
                {{ truncate(item.node.address, 24) }}
              </text>
              <text
                v-if="!item.node.accepting_connections"
                x="236"
                y="45"
                text-anchor="end"
                font-size="9.5"
                font-weight="700"
                fill="#f59e0b"
              >
                DRAIN
              </text>

              <line x1="14" y1="55" :x2="NODE_W - 14" y2="55" stroke="#282f2d" stroke-width="1" />

              <!-- Heartbeat & Uptime -->
              <text x="15" y="72" font-size="10.5" fill="#9aa5a0">
                Отклик:
                <tspan fill="#d9e0dc">{{ formatHeartbeatAge(item.node.heartbeat_age_sec) }}</tspan>
              </text>
              <text x="236" y="72" text-anchor="end" font-size="10.5" fill="#9aa5a0">
                Аптайм:
                <tspan fill="#d9e0dc">{{ formatUptime(item.node.uptime_seconds) }}</tspan>
              </text>

              <!-- Active connections & RX/TX -->
              <text x="15" y="92" font-size="10.5" fill="#9aa5a0">
                Сессии:
                <tspan font-weight="700" fill="#e8ebea">{{ item.node.active_connections }}</tspan>
              </text>
              <text x="236" y="92" text-anchor="end" font-size="10.5" fill="#b7c2bd">
                ↓{{ formatBytes(item.node.rx_bytes) }} · ↑{{ formatBytes(item.node.tx_bytes) }}
              </text>

              <!-- Transports chips -->
              <g transform="translate(15, 108)">
                <template v-if="item.node.transports.length">
                  <g
                    v-for="(tr, tIdx) in item.node.transports.slice(0, 3)"
                    :key="tr"
                    :transform="`translate(${tIdx * 66}, 0)`"
                  >
                    <rect width="60" height="20" rx="6" fill="#232c2a" stroke="#32403c" />
                    <text x="30" y="13.5" text-anchor="middle" font-size="9.5" fill="#7dd3bc">
                      {{ truncate(tr.toUpperCase(), 7) }}
                    </text>
                  </g>
                  <text
                    v-if="item.node.transports.length > 3"
                    x="202"
                    y="13.5"
                    font-size="9.5"
                    fill="#9aa5a0"
                  >
                    +{{ item.node.transports.length - 3 }}
                  </text>
                </template>
                <text v-else x="0" y="13.5" font-size="10" fill="#6f7a75">Нет транспортов</text>
              </g>
            </g>
          </g>

          <!-- Layer 4: Internet Egress Cloud -->
          <g
            :transform="`translate(${layout.cloud.x}, ${layout.cloud.y})`"
            :opacity="isCloudDimmed() ? 0.2 : 1"
            class="fade-el"
          >
            <rect
              :width="layout.cloud.w"
              :height="layout.cloud.h"
              rx="28"
              fill="url(#cloud-grad)"
              stroke="#2bb894"
              stroke-opacity="0.55"
              stroke-width="1.5"
            />
            <path
              d="M57 52h38a11 11 0 0 0 1-22 15 15 0 0 0-29-4 12 12 0 0 0-10 26z"
              fill="none"
              stroke="#2bb894"
              stroke-width="2"
              stroke-linejoin="round"
            />
            <text :x="layout.cloud.w / 2" y="75" text-anchor="middle" font-size="13" font-weight="650" fill="#e8ebea">
              Internet
            </text>
            <text :x="layout.cloud.w / 2" y="93" text-anchor="middle" font-size="10.5" fill="#9aa5a0">
              Egress Cloud
            </text>
          </g>
        </svg>
      </div>

      <!-- Side Inspector Panel -->
      <aside v-if="selectedNode || selectedEdge" class="inspector-panel">
        <template v-if="selectedNode">
          <div class="d-flex align-center justify-space-between ga-2 mb-3">
            <div class="min-w-0">
              <div class="text-subtitle-1 font-weight-bold text-truncate">{{ selectedNode.name }}</div>
              <div class="text-caption text-medium-emphasis">{{ selectedNode.address }}</div>
            </div>
            <v-btn icon="mdi-close" size="x-small" variant="text" @click="selected = null" />
          </div>

          <div class="d-flex flex-wrap ga-2 mb-3">
            <v-chip
              size="small"
              :color="NODE_STATUS_META[selectedNode.status].vuetify"
              variant="tonal"
            >
              {{ NODE_STATUS_META[selectedNode.status].label }}
            </v-chip>
            <v-chip size="small" variant="outlined">{{ roleLabel(selectedNode.role) }}</v-chip>
            <v-chip
              size="small"
              :color="selectedNode.accepting_connections ? 'success' : 'warning'"
              variant="tonal"
            >
              {{ selectedNode.accepting_connections ? 'Приём открыт' : 'Дренаж' }}
            </v-chip>
          </div>

          <v-alert
            v-if="selectedNode.probe_error"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-3 text-caption"
          >
            <strong>Ошибка пробы:</strong> {{ selectedNode.probe_error }}
          </v-alert>

          <dl class="metric-list mb-4">
            <div><dt>Отклик</dt><dd>{{ formatHeartbeatAge(selectedNode.heartbeat_age_sec) }}</dd></div>
            <div><dt>Аптайм</dt><dd>{{ formatUptime(selectedNode.uptime_seconds) }}</dd></div>
            <div><dt>Активные сессии</dt><dd>{{ selectedNode.active_connections }}</dd></div>
            <div><dt>Устройств</dt><dd>{{ selectedNode.device_count }}</dd></div>
            <div><dt>Трафик RX</dt><dd>{{ formatBytes(selectedNode.rx_bytes) }}</dd></div>
            <div><dt>Трафик TX</dt><dd>{{ formatBytes(selectedNode.tx_bytes) }}</dd></div>
            <div><dt>Ревизия проб</dt><dd>{{ selectedNode.probe_applied }} / {{ selectedNode.probe_revision }}</dd></div>
            <div><dt>Версия агента</dt><dd>{{ selectedNode.version || '—' }}</dd></div>
          </dl>

          <div class="mb-4">
            <div class="text-caption text-medium-emphasis mb-1">Транспорты</div>
            <div v-if="selectedNode.transports.length" class="d-flex flex-wrap ga-1">
              <v-chip
                v-for="tr in selectedNode.transports"
                :key="tr"
                size="x-small"
                color="primary"
                variant="tonal"
              >
                {{ tr.toUpperCase() }}
              </v-chip>
            </div>
            <span v-else class="text-caption text-medium-emphasis">—</span>
          </div>

          <v-divider class="mb-3" />

          <div class="d-flex flex-column ga-2">
            <v-btn
              size="small"
              variant="tonal"
              :color="selectedNode.accepting_connections ? 'warning' : 'success'"
              :prepend-icon="selectedNode.accepting_connections ? 'mdi-pause-circle-outline' : 'mdi-play-circle-outline'"
              :loading="actionBusy"
              block
              @click="handleToggleAdmission(selectedNode)"
            >
              {{ selectedNode.accepting_connections ? 'Перевести в дренаж' : 'Возобновить приём' }}
            </v-btn>

            <v-btn
              size="small"
              variant="tonal"
              prepend-icon="mdi-restart"
              :loading="actionBusy"
              block
              @click="handleResetState(selectedNode)"
            >
              Сбросить состояние
            </v-btn>

            <v-btn
              size="small"
              variant="tonal"
              color="error"
              prepend-icon="mdi-delete-outline"
              :loading="actionBusy"
              block
              @click="handleDeleteNode(selectedNode)"
            >
              Удалить узел
            </v-btn>

            <v-btn
              size="small"
              variant="outlined"
              prepend-icon="mdi-server-outline"
              :to="{ path: '/servers', query: { edit: selectedNode.id } }"
              block
            >
              Открыть в «Серверы»
            </v-btn>
          </div>
        </template>

        <template v-else-if="selectedEdge">
          <div class="d-flex align-center justify-space-between ga-2 mb-3">
            <div>
              <div class="text-subtitle-1 font-weight-bold">
                Канал {{ selectedEdge.transport.toUpperCase() }}
              </div>
              <div class="text-caption text-medium-emphasis">Межсерверное соединение</div>
            </div>
            <v-btn icon="mdi-close" size="x-small" variant="text" @click="selected = null" />
          </div>

          <div class="d-flex flex-wrap ga-2 mb-3">
            <v-chip
              size="small"
              :color="EDGE_STATUS_META[selectedEdge.status].vuetify"
              variant="tonal"
            >
              {{ EDGE_STATUS_META[selectedEdge.status].label }}
            </v-chip>
            <v-chip size="small" variant="outlined">{{ selectedEdge.transport.toUpperCase() }}</v-chip>
          </div>

          <div class="route-box pa-3 mb-4">
            <div class="text-caption text-medium-emphasis mb-1">Маршрут</div>
            <div class="d-flex align-center flex-wrap ga-1 text-body-2 font-weight-medium">
              <button
                type="button"
                class="node-link"
                @click="selected = { kind: 'node', id: selectedEdge.source }"
              >
                {{ nodeById.get(selectedEdge.source)?.name ?? selectedEdge.source }}
              </button>
              <v-icon icon="mdi-arrow-right" size="16" />
              <button
                type="button"
                class="node-link"
                @click="selected = { kind: 'node', id: selectedEdge.target }"
              >
                {{ nodeById.get(selectedEdge.target)?.name ?? selectedEdge.target }}
              </button>
            </div>
            <div v-if="selectedEdge.route_id" class="text-caption text-medium-emphasis mt-1">
              ID маршрута: <code>{{ selectedEdge.route_id }}</code>
            </div>
          </div>

          <dl class="metric-list">
            <div>
              <dt>Задержка (RTT)</dt>
              <dd>{{ selectedEdge.latency_ms != null ? `${Math.round(selectedEdge.latency_ms)} мс` : 'Нет пробы' }}</dd>
            </div>
            <div>
              <dt>Потери пакетов</dt>
              <dd>{{ selectedEdge.loss_pct != null ? `${Number(selectedEdge.loss_pct.toFixed(2))}%` : 'Нет пробы' }}</dd>
            </div>
            <div>
              <dt>Скорость</dt>
              <dd>
                {{
                  selectedEdge.throughput_mbps != null
                    ? `${Number(selectedEdge.throughput_mbps.toFixed(2))} Мбит/с`
                    : 'Нет пробы'
                }}
              </dd>
            </div>
            <div>
              <dt>Последний замер</dt>
              <dd>{{ formatDateTime(selectedEdge.last_measured_at) }}</dd>
            </div>
            <div><dt>Трафик RX</dt><dd>{{ formatBytes(selectedEdge.rx_bytes) }}</dd></div>
            <div><dt>Трафик TX</dt><dd>{{ formatBytes(selectedEdge.tx_bytes) }}</dd></div>
            <div>
              <dt>Суммарный трафик</dt>
              <dd>{{ formatBytes(selectedEdge.rx_bytes + selectedEdge.tx_bytes) }}</dd>
            </div>
          </dl>
        </template>
      </aside>
    </div>
  </v-card>
</template>

<style scoped>
.topology-card {
  overflow: hidden;
}

.user-select {
  min-width: 220px;
  max-width: 280px;
}

.zoom-label {
  min-width: 56px;
  font-variant-numeric: tabular-nums;
}

.auto-switch {
  flex: 0 0 auto;
}

.topology-body {
  display: flex;
  min-height: 400px;
  background: #121615;
}

.canvas-wrap {
  flex: 1 1 auto;
  overflow: auto;
  padding: 12px;
  display: flex;
  align-items: center;
  justify-content: flex-start;
}

.canvas-empty {
  width: 100%;
  min-height: 360px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.topology-svg {
  display: block;
  margin: 0 auto;
  user-select: none;
}

.layer-headers text {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  fill: #7f8c86;
}

.fade-el {
  transition: opacity 0.2s ease;
}

.node-card,
.edge-group {
  cursor: pointer;
}

.node-card:hover rect:first-of-type {
  filter: brightness(1.08);
}

.packet-dot {
  pointer-events: none;
}

.pulse-ring {
  transform-box: fill-box;
  transform-origin: center;
  animation: node-pulse 2s ease-out infinite;
}

@keyframes node-pulse {
  0% {
    transform: scale(1);
    opacity: 0.7;
  }
  100% {
    transform: scale(2.6);
    opacity: 0;
  }
}

.inspector-panel {
  width: 330px;
  flex: 0 0 330px;
  padding: 16px;
  background: #1a1f1e;
  border-left: 1px solid rgba(154, 165, 160, 0.16);
  overflow-y: auto;
}

.metric-list {
  display: grid;
  gap: 8px;
  font-size: 0.84rem;
}

.metric-list > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px dashed rgba(154, 165, 160, 0.12);
  padding-bottom: 4px;
}

.metric-list dt {
  color: #9aa5a0;
}

.metric-list dd {
  font-weight: 500;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.route-box {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(154, 165, 160, 0.16);
  border-radius: 8px;
}

.node-link {
  background: none;
  border: none;
  padding: 0;
  color: #2bb894;
  font: inherit;
  cursor: pointer;
}

.node-link:hover {
  text-decoration: underline;
}

@media (max-width: 960px) {
  .topology-body {
    flex-direction: column;
  }

  .inspector-panel {
    width: 100%;
    flex: none;
    border-left: none;
    border-top: 1px solid rgba(154, 165, 160, 0.16);
  }
}
</style>
