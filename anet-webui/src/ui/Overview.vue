<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { GetServers } from '@/api/servers'
import type { Server } from '@/models/server'
import ProbeLinks from '@/components/ProbeLinks.vue'

const servers = ref<Server[]>([])
const error = ref('')
const loaded = ref(false)
const selected = ref('')
const node = computed(() => servers.value.find(s => s.id === selected.value))
const online = computed(() => servers.value.filter(s => s.runtime?.status === 'online').length)
let timer: number
let pending = false
async function load() {
  if (pending) return
  pending = true
  try {
    servers.value = await GetServers()
    loaded.value = true
    error.value = ''
  } catch { error.value = 'Не удалось обновить список серверов.' }
  finally { pending = false }
}
onMounted(() => { load(); timer = window.setInterval(load, 15000) })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <main class="overview">
    <header class="d-flex flex-wrap justify-space-between align-center ga-4 mb-5">
      <div><h1>Сеть VPN</h1><p v-if="loaded">На связи {{ online }} из {{ servers.length }} серверов</p></div>
      <div class="d-flex flex-wrap ga-2">
        <v-btn to="/profile-topology" variant="tonal">Настроить маршруты</v-btn>
        <v-btn to="/servers?add=1" prepend-icon="mdi-plus" color="primary">Добавить сервер</v-btn>
      </div>
    </header>
    <v-alert v-if="error" type="warning">{{ error }}</v-alert>
    <details class="mb-4">
      <summary>Как обновить панель</summary>
      <p class="my-2">Откройте SSH на VPS с панелью под той же учётной записью, с которой запускали мастер. Выполните команду, выберите установку и подтвердите обновление.</p>
      <pre class="pa-3 overflow-x-auto"><code>f=$(mktemp) &amp;&amp; curl -fL --retry 2 -o "$f" https://github.com/AlphaO612/easy_anet/releases/latest/download/panel-update.sh &amp;&amp; bash "$f"</code></pre>
      <p class="my-2">После сообщения «Панель обновлена» обновите страницу браузера.</p>
    </details>
    <ProbeLinks :servers="servers" @select-node="selected = $event" />
    <v-card title="Серверы" class="mt-5">
      <v-card-text>
        <p v-if="loaded && !servers.length">Добавьте первый сервер, чтобы зарегистрировать узел в панели.</p>
        <div class="d-flex ga-2 flex-wrap">
          <v-btn v-for="s in servers" :key="s.id" :variant="s.id === selected ? 'flat' : 'tonal'" @click="selected = s.id">
            {{ s.name }}
          </v-btn>
        </div>
        <section v-if="node" class="mt-4" aria-live="polite">
          <h3>{{ node.name }}</h3>
          <p>{{ node.address }} · {{ !node.runtime ? 'Нет отчёта узла' : node.runtime.status === 'online' ? 'На связи' : 'Связь потеряна' }}</p>
          <p v-if="node.runtime">Соединений по последнему отчёту: {{ node.runtime.active_connections }} · {{ new Date(node.runtime.last_seen_at).toLocaleString() }}</p>
          <v-btn :to="{ path: '/servers', query: { edit: node.id } }" variant="text">Настроить сервер</v-btn>
        </section>
      </v-card-text>
    </v-card>
  </main>
</template>
<style scoped>
.overview { max-width: 1200px; margin: auto; padding: clamp(8px, 2vw, 28px); }
h1 { font-size: 1.9rem; font-weight: 650; }
header p { opacity: .72; }
</style>
