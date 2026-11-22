<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { advanced } from '@/composables/operatorMode'
import { GetUsers } from '@/api/users'
import ProbeRevisionDialog from '@/components/ProbeRevisionDialog.vue'
import StarterProfiles from '@/components/StarterProfiles.vue'
import VlessRecipients from '@/components/VlessRecipients.vue'
import { api } from '@/api/client'
import type { Server } from '@/models/server'
import type { ProbeEdge } from '@/api/probes'

type Revision = { revision: number; manifest: string }
type Manifest = Record<'paths' | 'profiles', Record<string, unknown>>
const saved = ref<Revision>()
type Report = { node_id: string; reported_at: string; report: { profile: string; revision: number; result: string } }
const reports = ref<Report[]>([])
const histories = ref<Record<string, Report[]>>({})
type Decision = { selected: string | null; strategy: string; eligible: string[] }
type Evidence = { last_seen?: number; available?: boolean; latency_ms?: number; desired: number; applied: number }
function result(row: Report): { status?: string; error?: string; decisions?: Record<string, Decision>; observations?: Record<string, Evidence> } {
  try { return JSON.parse(row.report.result) ?? {} } catch { return {} }
}
async function history(row: Report) {
  try {
    histories.value[row.node_id + '/' + row.report.profile] = await api<Report[]>('/servers/' + row.node_id + '/profile-history', { params: { profile: row.report.profile } })
  } catch { reportStatus.value = 'Не удалось загрузить историю решений.' }
}
const reportStatus = ref('')
async function loadReports() {
  try {
    reports.value = await api<Report[]>('/profile-results')
    reportStatus.value = reports.value.length ? '' : 'Узлы ещё не прислали подтверждения.'
  } catch { reportStatus.value = 'Не удалось обновить подтверждения узлов.' }
}
const draft = ref('')
const status = ref('')
const busy = ref(false)
type Link = { source_node: string; edge: ProbeEdge }
const nodes = ref<Server[]>([])
const links = ref<Link[]>([])
const route = useRoute()
const editProbe = ref(false)
const users = ref<string[]>([])
const userError = ref('')
let searchNumber = 0
async function searchUsers(value = '') {
  const search = ++searchNumber
  try {
    const response = await GetUsers(0, 30, value)
    if (search === searchNumber) { users.value = response.items.map(u => u.uid); userError.value = '' }
  } catch { if (search === searchNumber) userError.value = 'Поиск пользователей недоступен.' }
}
const entry = ref(typeof route.query.entry === 'string' ? route.query.entry : '')
const exit = ref(typeof route.query.exit === 'string' ? route.query.exit : '')
const profileName = ref('')
const owner = ref('')
const ingress = ref('native')
const group = ref('')
const externalIp = ref('')
const strategy = ref('prefer-route')
const probe = ref('')
const availableLinks = computed(() => links.value.filter(link => link.source_node === entry.value && link.edge.target_node === exit.value)
  .map(link => ({ title: link.edge.module + ' · ' + link.edge.target, value: JSON.stringify(link.edge) })))
watch(availableLinks, items => {
  if (!items.some(item => item.value === probe.value)) probe.value = items[0]?.value ?? ''
})
const entryNode = computed(() => nodes.value.find(n => n.id === entry.value))
async function loadNodes() {
  try {
    const [servers, graph] = await Promise.all([api<Server[]>('/servers'), api<{links: Link[]}>('/probe-links')])
    nodes.value = servers.filter(node => node.is_active)
    links.value = graph.links
  } catch { status.value = 'Не удалось загрузить узлы и направления проб.' }
}
function addPath() {
  try {
    const t = JSON.parse(draft.value)
    const name = profileName.value.trim()
    if (!name || ['constructor', '__proto__', 'prototype'].includes(name)) throw Error('Укажите имя профиля.')
    const key = entry.value + '/' + exit.value
    const edge = availableLinks.value.find(link => link.value === probe.value)
    if (!edge) throw Error('Выберите опубликованную пробу для этого направления.')
    const path = { entry: entry.value, exit: exit.value, external_ip: externalIp.value.trim(),
      transport: entry.value === exit.value ? 'direct' : 'quic', probe: { source_node: entry.value, ...JSON.parse(edge.value) } }
    const existing = t.paths[key]
    if (existing && (existing.entry !== path.entry || existing.exit !== path.exit || existing.external_ip !== path.external_ip ||
        existing.transport !== path.transport || existing.probe?.module !== path.probe.module || existing.probe?.target !== path.probe.target))
      throw Error('Направление уже существует. Для изменения общего пути используйте редактор JSON.')
    const current = t.profiles[name]
    if (current && current.entry !== entry.value) throw Error('Выберите вход этого профиля.')
    t.paths[key] = path
    if (current) current.paths = [...new Set([...current.paths, key])]
    else t.profiles[name] = { entry: entry.value, paths: [key], strategy: strategy.value,
      user: owner.value.trim(), ...(group.value.trim() ? { groups: [group.value.trim()] } : {}),
      ...(strategy.value === 'strict-ip' ? { external_ip: path.external_ip } : {}),
      ingress: ingress.value, credential_id: crypto.randomUUID() }
    draft.value = JSON.stringify(t, null, 2)
    status.value = 'Путь добавлен в черновик.'
  } catch (error) { status.value = String(error) }
}
function revoke(name: string) {
  try {
    const t = JSON.parse(draft.value)
    delete t.profiles[name]
    draft.value = JSON.stringify(t, null, 2)
    status.value = 'Отзыв внесён в черновик. Опубликуйте изменение для применения.'
  } catch { status.value = 'Исправьте JSON черновика.' }
}
async function downloadConfig(name: string, node: string) {
  try {
    const record = await api<{ config: string }>('/servers/' + node + '/profile-config', { params: { profile: name } })
    const url = URL.createObjectURL(new Blob([record.config], { type: 'text/plain' }))
    const link = document.createElement('a')
    link.href = url
    const kind = activeProfiles.value[name]?.ingress
    link.download = name + (kind === 'vless' ? '.uri' : kind === 'wireguard' ? '.conf' : '.toml')
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { status.value = 'Конфигурация действующей ревизии пока недоступна.' }
}
type Profile = {entry: string; ingress?: string; paths: string[]}
type Path = {entry: string; exit: string; external_ip: string}
const publication = computed((): {profiles: Record<string, Profile>; paths: Record<string, Path>} =>
  saved.value ? JSON.parse(saved.value.manifest) : { paths: {}, profiles: {} })
const activeProfiles = computed(() => publication.value.profiles)
function exitName(key: string) {
  const path = publication.value.paths[key]
  return path ? (nodes.value.find(n => n.id === path.exit)?.name ?? path.exit) + ' (' + path.external_ip + ')' : key
}
function selectedExit(name: string, entry: string) {
  const row = reports.value.find(r => r.report.profile === name && r.node_id === entry && r.report.revision === saved.value?.revision)
  if (!row) return 'Ожидается отчёт текущей ревизии'
  const decision = result(row).decisions?.['(default)']
  if (!decision) return 'Нет подтверждения'
  return (decision.selected ? exitName(decision.selected) : 'Передача запрещена') + ' · ' + new Date(row.reported_at).toLocaleString()
}
function importHelp(ingress?: string) {
  if (ingress === 'wireguard') return 'В WireGuard выберите «Добавить туннель» и импортируйте скачанный файл .conf.'
  if (ingress === 'vless') return 'Откройте файл .uri, скопируйте ссылку vless:// и импортируйте её из буфера в клиент с поддержкой VLESS/TLS.'
  return 'Откройте файл .toml в клиенте ANet или передайте его ANet CLI параметром -c.'
}
function accept(value: Revision) {
  saved.value = value
  draft.value = JSON.stringify(JSON.parse(value.manifest), null, 2)
}
async function load() {
  busy.value = true
  try {
    accept(await api<Revision>('/profile-topology'))
    status.value = ''
  } catch { status.value = 'Не удалось загрузить топологию.' }
  finally { busy.value = false }
}
const changes = computed(() => {
  if (!saved.value) return []
  try {
    const before = JSON.parse(saved.value.manifest) as Manifest
    const after = JSON.parse(draft.value) as Manifest
    return (['paths', 'profiles'] as const).flatMap(section =>
      [...new Set([...Object.keys(before[section]), ...Object.keys(after[section])])].flatMap(id => {
        const oldValue = JSON.stringify(before[section][id])
        const newValue = JSON.stringify(after[section][id])
        return oldValue === newValue ? [] : [section + '.' + id + '\n− ' + (oldValue ?? '∅') + '\n+ ' + (newValue ?? '∅')]
      }))
  } catch { return [] }
})
async function publish() {
  if (!saved.value) return
  busy.value = true
  try {
    accept(await api<Revision>('/profile-topology', { method: 'PUT', data: { revision: saved.value.revision, manifest: draft.value } }))
    status.value = 'Ревизия опубликована.'
  } catch (err) { status.value = 'Ошибка публикации. ' + String(err) }
  finally { busy.value = false }
}
function download(inventory = false) {
  if (!saved.value) return
  const { revision, manifest } = saved.value
  // YAML treats some valid JSON string characters as line breaks or forbidden controls.
  const json = JSON.stringify(JSON.parse(manifest), null, 2)
    .replace(/[\u007f-\u009f\u2028\u2029\ufffe\uffff]/g, c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
  const data = inventory ? [
    'all:',
    '  children:',
    '    anet_topology:',
    '      hosts:',
    '        localhost:',
    '          ansible_connection: local',
    '      vars:',
    '        anet_base_revision: ' + revision,
    '        anet_topology_manifest: !unsafe |',
    ...json.split('\n').map(line => '          ' + line),
    '',
  ].join('\n') : manifest
  const url = URL.createObjectURL(new Blob([data], { type: inventory ? 'text/yaml' : 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'profile-topology-' + revision + (inventory ? '.yml' : '.json')
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
onMounted(() => { load(); loadNodes(); loadReports(); searchUsers() })
</script>

<template>
  <v-container>
    <h1>Профили и маршруты</h1>
    <VlessRecipients />
    <StarterProfiles />
    <p v-if="saved">Опубликованная ревизия {{ saved.revision }}</p>
    <details :open="!!route.query.entry || !!route.query.exit || !Object.keys(activeProfiles).length">
      <summary>Назначить вход и выход</summary>
      <v-text-field v-model="profileName" label="Имя профиля" />
      <v-select v-model="entry" :items="nodes" item-title="name" item-value="id" label="Входной узел" />
      <v-select v-model="exit" :items="nodes" item-title="name" item-value="id" label="Выходной узел" />
      <v-btn v-if="entryNode" variant="text" @click="editProbe = true">Настроить проверки на входе</v-btn>
      <ProbeRevisionDialog v-if="editProbe && entryNode" :server="entryNode" :servers="nodes" @close="editProbe = false; loadNodes()" />
      <v-select v-model="probe" :items="availableLinks" label="Проверка направления" />
      <v-text-field v-model="externalIp" label="Публичный IP выхода" />
      <v-select v-model="ingress" :items="[{ title: 'ANet', value: 'native' }, { title: 'WireGuard', value: 'wireguard' }, { title: 'VLESS', value: 'vless' }]" label="Протокол нового профиля" />
      <v-combobox v-model="owner" :items="users" :error-messages="userError" label="Пользователь нового профиля" @update:search="searchUsers" />
      <v-text-field v-if="advanced" v-model="group" label="Группа нового профиля (необязательно)" />
      <v-select v-model="strategy" :items="['prefer-route', 'strict-ip', 'speed', 'auto']" label="Стратегия нового профиля" />
      <v-btn :disabled="busy || !saved || !probe || !owner?.trim() || !externalIp.trim()" @click="addPath">Добавить путь в черновик</v-btn>
    </details>
    <v-textarea v-if="advanced" v-model="draft" rows="16" label="Маршруты и политики (JSON)" :disabled="busy || !saved" />
    <p v-if="changes.length">Изменений в черновике: {{ changes.length }}</p>
    <template v-if="advanced"><pre v-for="change in changes" :key="change">{{ change }}</pre></template>
    <v-btn :disabled="busy || !changes.length" @click="publish">Опубликовать</v-btn>
    <v-btn v-if="advanced" :disabled="busy || !saved" @click="download()">Экспорт ревизии</v-btn>
    <v-btn v-if="advanced" :disabled="busy || !saved" @click="download(true)">Экспорт inventory</v-btn>
    <v-btn :disabled="busy" @click="load">Загрузить ревизию</v-btn>
    <p role="status">{{ status }}</p>
    <div v-for="(profile, name) in activeProfiles" :key="name">
      <h3>{{ name }} · {{ profile.ingress === 'wireguard' ? 'WireGuard' : profile.ingress === 'vless' ? 'VLESS' : 'ANet' }}</h3>
      <p>Назначенные выходы: {{ profile.paths.map(exitName).join(', ') }}</p>
      <p>Выход по последнему отчёту (правило по умолчанию): {{ selectedExit(String(name), profile.entry) }}</p>
      <p>{{ importHelp(profile.ingress) }}</p>
      <v-btn @click="downloadConfig(String(name), profile.entry)">Скачать профиль</v-btn>
      <v-btn :disabled="busy" @click="revoke(String(name))">Отозвать</v-btn>
    </div>
    <h2>Применение на входных узлах</h2>
    <v-btn @click="loadReports">Обновить подтверждения</v-btn>
    <p role="status">{{ reportStatus }}</p>
    <details v-for="row in reports" :key="row.node_id + '/' + row.report.profile">
      <summary>{{ row.report.profile }} · {{ nodes.find(n => n.id === row.node_id)?.name ?? row.node_id }} · ревизия {{ row.report.revision }} · {{ row.reported_at }}</summary>
      <p>{{ result(row).status === 'error' ? result(row).error : 'Правила применены' }}</p>
      <p v-for="(d, rule) in result(row).decisions" :key="rule">
        {{ rule }} · {{ d.strategy }} · {{ d.selected ?? 'передача запрещена' }} · доступны {{ d.eligible.join(', ') || '∅' }}
      </p>
      <p v-for="(obs, path) in result(row).observations" :key="path">
        {{ path }} · проба {{ obs.applied }}/{{ obs.desired }} · {{ obs.last_seen ? new Date(obs.last_seen * 1000).toLocaleString() : 'нет измерения' }}
        · {{ obs.available === true ? 'доступен' : 'нет подтверждения' }}
      </p>
      <v-btn @click="history(row)">История решений</v-btn>
      <details v-for="(event, index) in histories[row.node_id + '/' + row.report.profile]" :key="index">
        <summary>{{ event.reported_at }} · ревизия {{ event.report.revision }}</summary>
        <pre>{{ event.report.result }}</pre>
      </details>
    </details>
  </v-container>
</template>
