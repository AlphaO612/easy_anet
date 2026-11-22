<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getStarterProfiles, type StarterProfile } from '@/api/starter-profiles'
import { api } from '@/api/client'
import type { ProbeEdge } from '@/api/probes'
import type { Server } from '@/models/server'

type Link = {
  source_node: string
  edge: ProbeEdge
  desired: number
  applied: number
  last_seen?: number | null
  available?: boolean | null
  mbps?: number | null
  probe_bytes_per_second?: number | null
}
type Snapshot = { monitoring: 'disabled' | 'configured' | 'unreachable'; links: Link[] }
type Path = { entry: string; exit: string; transport: string; external_ip: string; probe?: ProbeEdge }
const paths = ref<Record<string, Path>>({})
const topologyError = ref('')
async function loadPaths() {
  try {
    const published = await api<{manifest: string}>('/profile-topology', { timeout: 8000 })
    paths.value = JSON.parse(published.manifest).paths
    topologyError.value = ''
  } catch { topologyError.value = 'Не удалось обновить маршруты.' }
}
const starterProfiles = ref<StarterProfile[]>([])
const starterError = ref(false)
async function loadStarterPaths() {
  try { starterProfiles.value = await getStarterProfiles(); starterError.value = false }
  catch { starterError.value = true }
}
const starterRoutes = computed(() => {
  const grouped = new Map<string, StarterProfile[]>()
  for (const profile of starterProfiles.value) {
    const id = JSON.stringify([profile.entry, profile.exit])
    const group = grouped.get(id)
    if (group) group.push(profile)
    else grouped.set(id, [profile])
  }
  return [...grouped].map(([id, profiles]) => ({ id, entry: profiles[0]!.entry, exit: profiles[0]!.exit, profiles }))
})
const protocolNames = { anet: 'ANet', vless: 'VLESS', wireguard: 'WireGuard' }
const emit = defineEmits<{ selectNode: [id: string] }>()
const props = defineProps<{ servers: Server[] }>()
const names = computed(() => new Map(props.servers.map(node => [node.id, node.name])))
const snapshot = ref<Snapshot>()
const failed = ref(false)
const now = ref(Date.now())
const grafanaUrl = import.meta.env.VITE_ANET_GRAFANA_URL
let timer: number
let clock: number
let loading = false
const key = (link: Link) => JSON.stringify([link.source_node, link.edge.target_node, link.edge.module, link.edge.target])
const load = async () => {
  if (loading) return
  loading = true
  try {
    const [next] = await Promise.all([api<Snapshot>('/probe-links', { timeout: 8000 }), loadPaths(), loadStarterPaths()])
    if (next.monitoring === 'unreachable') {
      const previous = new Map(snapshot.value?.links.map(link => [key(link), link.last_seen]))
      for (const link of next.links) link.last_seen = previous.get(key(link))
    }
    snapshot.value = next
    failed.value = false
  } catch {
    failed.value = true
  } finally {
    loading = false
  }
}
const fresh = (link: Link) => !failed.value && snapshot.value?.monitoring === 'configured'
  && link.last_seen != null && now.value / 1000 - link.last_seen <= 45
const state = (link: Link) => {
  if (!fresh(link)) return 'Нет свежих измерений'
  if (link.available == null) return 'Недостаточно проб'
  return link.available ? 'Доступна' : 'Отказ'
}
const routes = computed(() => Object.entries(paths.value).map(([id, path]) => ({
  id, path, link: snapshot.value?.links.find(link => link.source_node === path.entry &&
    link.edge.target_node === path.exit && link.edge.module === path.probe?.module && link.edge.target === path.probe?.target),
})))
const selected = ref('')
function loadLabel(id: string) {
  const runtime = props.servers.find(s => s.id === id)?.runtime
  return runtime ? 'Соединений: ' + runtime.active_connections : 'Нет отчёта'
}
function history(link: Link) {
  if (!grafanaUrl) return undefined
  const url = new URL(grafanaUrl, window.location.origin)
  for (const [field, value] of Object.entries({ source_node: link.source_node, target_node: link.edge.target_node,
    module: link.edge.module, instance: link.edge.target })) {
    url.searchParams.set('var-' + field, value)
  }
  return url.href
}
onMounted(() => {
  load()
  timer = window.setInterval(load, 10_000)
  clock = window.setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => { clearInterval(timer); clearInterval(clock) })
</script>

<template>
  <v-card title="Входы и выходы" class="graph">
    <v-card-text>
      <p v-if="topologyError" role="alert">{{ topologyError }}</p>
      <p v-if="failed || snapshot?.monitoring === 'unreachable'" role="alert">Источник телеметрии недоступен.</p>
      <p v-else-if="snapshot?.monitoring === 'disabled'">Подключите Prometheus для просмотра измерений.</p>
      <p v-if="snapshot && !routes.length && !starterRoutes.length && !starterError && !topologyError">
        Маршрутов пока нет. <router-link to="/profile-topology">Назначить вход и выход</router-link>
      </p>
      <p v-if="starterError" role="alert">Не удалось обновить маршруты мастера установки.</p>
      <article v-for="route in starterRoutes" :key="route.id">
        <div class="route">
          <div class="station"><small>Вход</small><b>{{ route.entry }}</b><small>{{ [...new Set(route.profiles.map(p => protocolNames[p.protocol]))].join(' · ') }}</small></div>
          <button class="segment" :aria-expanded="selected === route.id" @click="selected = selected === route.id ? '' : route.id">
            <b>ANet QUIC →</b><span>Нет измерений</span><small>Скорость неизвестна</small>
          </button>
          <div class="station"><small>Выход</small><b>{{ route.exit }}</b><small>Нет отчёта о нагрузке</small></div>
        </div>
        <div v-if="selected === route.id" class="mt-3">
          <p v-for="p in route.profiles" :key="p.id">{{ p.id }} · {{ protocolNames[p.protocol] }} · вход {{ p.entry }}:{{ p.port }}</p>
          <router-link to="/profile-topology">Получить клиентский профиль</router-link>
        </div>
      </article>
      <article v-for="{ id, path, link } in routes" :key="id">
        <div class="route">
          <button class="station" @click="emit('selectNode', path.entry)">
            <small>Вход</small><b>{{ names.get(path.entry) ?? path.entry }}</b><small>{{ loadLabel(path.entry) }}</small>
          </button>
          <button class="segment" :aria-expanded="selected === id" @click="selected = selected === id ? '' : id">
            <b>{{ path.transport.toUpperCase() }} →</b>
            <span :class="{ good: link && fresh(link) && link.available, bad: link && fresh(link) && link.available === false }">
              {{ link ? state(link) : 'Нет измерений' }}
            </span>
            <small>{{ link && fresh(link) && link.available && link.mbps != null ? link.mbps.toFixed(2) + ' Мбит/с' : 'Скорость не измерена' }}</small>
          </button>
          <button class="station" @click="emit('selectNode', path.exit)">
            <small>Выход</small><b>{{ names.get(path.exit) ?? path.exit }}</b><small>{{ loadLabel(path.exit) }}</small>
          </button>
        </div>
        <p v-if="selected === id" class="mt-3">Назначенный IP: {{ path.external_ip }} · {{ link?.edge.target || 'Проба отсутствует' }}
          <span v-if="link?.last_seen"> · Последнее измерение: {{ new Date(link.last_seen * 1000).toLocaleString() }}</span>
        </p>
      </article>
      <details v-if="snapshot?.links.length" class="mt-4">
        <summary>Все межсерверные проверки ({{ snapshot.links.length }})</summary>
        <article v-for="link in snapshot.links" :key="key(link)">
          <b>{{ names.get(link.source_node) ?? link.source_node }} → {{ names.get(link.edge.target_node) ?? link.edge.target_node }}</b>
          <div>{{ state(link) }} · {{ link.edge.module }} · {{ link.edge.target }}</div>
          <div>Ревизия проверок: опубликована {{ link.desired }}, применена {{ link.applied }}</div>
          <div v-if="fresh(link) && link.available && link.mbps != null">Загрузка контрольного файла (64 КиБ): {{ link.mbps.toFixed(2) }} Мбит/с</div>
          <a v-if="grafanaUrl" :href="history(link)" target="_blank" rel="noopener noreferrer">История в Grafana</a>
        </article>
      </details>
    </v-card-text>
  </v-card>
</template>

<style scoped>
article { padding: 12px 0; overflow-wrap: anywhere; }
article + article { border-top: 1px solid rgba(128,128,128,.3); }
.route { display: grid; grid-template-columns: 1fr minmax(8rem, .8fr) 1fr; gap: .5rem; align-items: stretch; }
.station, .segment { display: grid; align-content: center; padding: .65rem; color: inherit; border-radius: .45rem; }
.station { background: rgba(125,150,148,.12); text-align: start; }
.segment { border-block: 1px dashed rgba(128,128,128,.5); }
small { opacity: .75; }
.good { color: #47ba9c; }
.bad { color: #ef8582; }
@media(max-width: 500px) { .route { grid-template-columns: 1fr; } }
</style>
