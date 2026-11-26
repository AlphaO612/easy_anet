<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { api } from '@/api/client'
import { GetServers } from '@/api/servers'
import { CreateIngressRoute } from '@/api/devices'
import { GetUsers } from '@/api/users'
import { probeStatus, type ProbeEdge } from '@/api/probes'
import { useAppMessage } from '@/composables/useAppMessage'
import type { Server } from '@/models/server'
import { extractApiError } from '@/utils/format'
import { formatRegionBadge, nodeRoleMeta } from '@/utils/regions'

const show = defineModel<boolean>()
const emit = defineEmits<{ linked: [] }>()

const appMessage = useAppMessage()

const loading = ref(false)
const busy = ref(false)
const error = ref('')
const servers = ref<Server[]>([])
const defaultUser = ref('admin')

const form = reactive({
  entryId: '',
  exitId: '',
  mode: 'anet' as 'anet' | 'vless' | 'wireguard',
  strategy: 'auto' as 'auto' | 'speed' | 'prefer-route',
  entryPort: 443,
  addProbe: true,
  probePort: 22,
  profileName: '',
})

const serverLabel = (s: Server) => {
  const flag = s.region ? `${formatRegionBadge(s.region)} ` : ''
  const role = s.node_role && s.node_role !== 'auto' ? ` [${nodeRoleMeta(s.node_role).short}]` : ''
  const st = s.runtime?.status === 'online' ? '●' : '○'
  const port = s.quic_port ? `:${s.quic_port}` : ''
  return `${st} ${flag}${s.name}${role} (${s.address}${port})`
}

const entryOptions = computed(() =>
  [...servers.value]
    .filter((s) => s.is_active)
    .sort((a, b) => {
      const rank = (s: Server) =>
        s.node_role === 'entry' ? 0 : s.node_role === 'both' ? 1 : s.tags?.toLowerCase().includes('gateway') ? 1 : 2
      return rank(a) - rank(b) || a.name.localeCompare(b.name)
    })
    .map((s) => ({ title: serverLabel(s), value: s.id })),
)

const exitOptions = computed(() =>
  [...servers.value]
    .filter((s) => s.is_active && s.id !== form.entryId)
    .sort((a, b) => {
      const rank = (s: Server) =>
        s.node_role === 'exit' ? 0 : s.node_role === 'both' ? 1 : s.tags?.toLowerCase().includes('exit') ? 1 : 2
      return rank(a) - rank(b) || a.name.localeCompare(b.name)
    })
    .map((s) => ({ title: serverLabel(s), value: s.id })),
)

const entryServer = computed(() => servers.value.find((s) => s.id === form.entryId) ?? null)
const exitServer = computed(() => servers.value.find((s) => s.id === form.exitId) ?? null)

const canSubmit = computed(() => Boolean(entryServer.value && exitServer.value && form.entryId !== form.exitId && !busy.value))

watch(() => form.mode, (m) => {
  if (m === 'vless') form.entryPort = 443
  if (m === 'wireguard') form.entryPort = 51820
})

watch(() => form.exitId, () => {
  if (exitServer.value) {
    form.probePort = exitServer.value.ssh_port || 22
  }
})

async function init() {
  loading.value = true
  error.value = ''
  try {
    const [srvs, usrRes] = await Promise.all([
      GetServers(),
      GetUsers(0, 5).catch(() => ({ total: 0, items: [] })),
    ])
    servers.value = srvs
    defaultUser.value = usrRes.items.find((u) => u.uid)?.uid || 'admin'
    if (!form.entryId && entryOptions.value.length) {
      form.entryId = entryOptions.value[0]!.value
    }
    if (!form.exitId || form.exitId === form.entryId) {
      form.exitId = exitOptions.value[0]?.value || ''
    }
  } catch (err) {
    error.value = extractApiError(err, 'Не удалось загрузить список серверов')
  } finally {
    loading.value = false
  }
}

watch(show, (open) => {
  if (open) {
    error.value = ''
    void init()
  }
})

async function submit() {
  if (!entryServer.value || !exitServer.value) return
  busy.value = true
  error.value = ''
  const entry = entryServer.value
  const exit = exitServer.value
  let probeWarning = ''
  const probeTarget = `${exit.address}:${Number(form.probePort) || exit.ssh_port || 22}`
  const probeEdge: ProbeEdge = {
    target_node: exit.id,
    module: 'tcp_connect',
    target: probeTarget,
  }

  try {
    // 1. Автоматически добавляем пробу связности на входной сервер (если ещё нет)
    if (form.addProbe) {
      try {
        const st = await probeStatus(entry.id)
        const existingEdges = st.manifest?.edges ?? []
        const hasEdge = existingEdges.some(
          (e) => e.target_node === exit.id && e.module === probeEdge.module && e.target === probeEdge.target,
        )
        if (!hasEdge) {
          await api(`/servers/${encodeURIComponent(entry.id)}/probe-revision`, {
            method: 'PUT',
            data: {
              base_revision: st.desired,
              edges: [...existingEdges, probeEdge],
            },
          })
        }
      } catch (probeErr) {
        probeWarning = ` (проба не добавлена: ${extractApiError(probeErr, 'проверьте allowlist')})`
      }
    }

    // 2. Создаём и публикуем маршрут
    if (form.mode === 'anet') {
      const rev = await api<{ revision: number; manifest: string }>('/profile-topology')
      const manifest = JSON.parse(rev.manifest || '{"paths":{},"profiles":{}}') as {
        paths: Record<string, any>
        profiles: Record<string, any>
      }
      manifest.paths = manifest.paths || {}
      manifest.profiles = manifest.profiles || {}

      const pathKey = `${entry.id}/${exit.id}`
      manifest.paths[pathKey] = {
        entry: entry.id,
        exit: exit.id,
        external_ip: exit.address,
        transport: 'quic',
        probe: {
          source_node: entry.id,
          ...probeEdge,
        },
      }

      const existingProfileKey = Object.keys(manifest.profiles).find(
        (k) => manifest.profiles[k]?.entry === entry.id,
      )
      const profName =
        form.profileName.trim() ||
        existingProfileKey ||
        `auto-${entry.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'route'}`

      if (manifest.profiles[profName] && manifest.profiles[profName].entry === entry.id) {
        const currentPaths: string[] = Array.isArray(manifest.profiles[profName].paths)
          ? manifest.profiles[profName].paths
          : []
        manifest.profiles[profName].paths = [...new Set([...currentPaths, pathKey])]
      } else {
        manifest.profiles[profName] = {
          entry: entry.id,
          paths: [pathKey],
          strategy: form.strategy,
          user: defaultUser.value,
          ingress: 'native',
          credential_id: crypto.randomUUID(),
        }
      }

      await api('/profile-topology', {
        method: 'PUT',
        data: {
          revision: rev.revision,
          manifest: JSON.stringify(manifest, null, 2),
        },
      })
      appMessage.success(`Маршрут ${entry.name} → ${exit.name} опубликован${probeWarning}`)
    } else {
      const routeName =
        form.profileName.trim() || `${form.mode.toUpperCase()} ${entry.name} → ${exit.name}`
      await CreateIngressRoute({
        protocol: form.mode,
        name: routeName,
        entry_host: entry.address,
        entry_port: Number(form.entryPort) || (form.mode === 'vless' ? 443 : 51820),
        exit_host: exit.address,
        entry_server_id: entry.id,
        exit_server_id: exit.id,
        sni: form.mode === 'vless' ? entry.address : null,
      })
      appMessage.success(`Входная точка ${routeName} создана${probeWarning}`)
    }

    emit('linked')
    show.value = false
  } catch (err) {
    error.value = extractApiError(err, 'Не удалось связать серверы')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-dialog v-model="show" max-width="680" :persistent="busy">
    <v-card>
      <v-card-title class="d-flex align-center ga-2 pt-4 px-5">
        <v-icon icon="mdi-link-variant-plus" color="primary" />
        <span class="text-h6">Связать два сервера в 1 шаг</span>
      </v-card-title>
      <v-card-subtitle class="px-5 pb-2">
        Выберите входной и выходной сервер — панель сама подставит IP/порты, добавит пробу связности и опубликует маршрут.
      </v-card-subtitle>

      <v-card-text class="pt-3">
        <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-3" />
        <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>

        <v-row dense>
          <v-col cols="12">
            <v-select
              v-model="form.entryId"
              :items="entryOptions"
              item-title="title"
              item-value="value"
              label="1. Входной сервер (Gateway / Entry)"
              prepend-inner-icon="mdi-login-variant"
              variant="outlined"
              density="comfortable"
            />
          </v-col>
          <v-col cols="12">
            <v-select
              v-model="form.exitId"
              :items="exitOptions"
              item-title="title"
              item-value="value"
              label="2. Выходной сервер (Exit)"
              prepend-inner-icon="mdi-logout-variant"
              variant="outlined"
              density="comfortable"
            />
          </v-col>
        </v-row>

        <div class="mb-3 mt-1">
          <div class="text-caption text-medium-emphasis mb-1">3. Тип маршрута / протокол</div>
          <v-btn-toggle v-model="form.mode" mandatory color="primary" variant="outlined" density="comfortable" class="flex-wrap">
            <v-btn value="anet" prepend-icon="mdi-routes">ANet (Топология потоков)</v-btn>
            <v-btn value="vless" prepend-icon="mdi-shield-lock-outline">VLESS</v-btn>
            <v-btn value="wireguard" prepend-icon="mdi-vpn">WireGuard</v-btn>
          </v-btn-toggle>
        </div>

        <v-row dense class="mt-2">
          <v-col v-if="form.mode === 'anet'" cols="12" sm="6">
            <v-select
              v-model="form.strategy"
              :items="[
                { title: 'Авто (баланс задержки и доступности)', value: 'auto' },
                { title: 'Макс. скорость (Speed)', value: 'speed' },
                { title: 'Предпочитать маршрут (Prefer route)', value: 'prefer-route' },
              ]"
              item-title="title"
              item-value="value"
              label="Стратегия переключения"
              variant="outlined"
              density="compact"
            />
          </v-col>
          <v-col v-else cols="12" sm="6">
            <v-text-field
              v-model.number="form.entryPort"
              type="number"
              min="1"
              max="65535"
              label="Входной порт на шлюзе"
              variant="outlined"
              density="compact"
            />
          </v-col>
          <v-col cols="12" sm="6">
            <v-text-field
              v-model="form.profileName"
              label="Имя профиля / маршрута (опционально)"
              placeholder="Автоматически"
              variant="outlined"
              density="compact"
            />
          </v-col>
        </v-row>

        <div class="d-flex align-center flex-wrap ga-3 mt-1">
          <v-switch
            v-model="form.addProbe"
            label="Автоматически добавить TCP-пробу связности"
            color="primary"
            inset
            density="compact"
            hide-details
          />
          <v-text-field
            v-if="form.addProbe"
            v-model.number="form.probePort"
            type="number"
            min="1"
            max="65535"
            label="TCP-порт пробы"
            density="compact"
            variant="outlined"
            hide-details
            style="max-width: 140px"
          />
        </div>

        <v-sheet v-if="entryServer && exitServer" variant="outlined" rounded="lg" class="pa-3 mt-4 preview-route">
          <div class="text-caption text-medium-emphasis mb-1">Будет создана связь:</div>
          <div class="d-flex align-center flex-wrap ga-2 text-body-2 font-weight-bold">
            <v-chip size="small" color="primary" variant="tonal">
              {{ entryServer.region ? `${formatRegionBadge(entryServer.region)} ` : '' }}{{ entryServer.name }}
            </v-chip>
            <v-icon icon="mdi-arrow-right-thick" size="18" color="primary" />
            <v-chip size="small" variant="outlined">{{ form.mode.toUpperCase() }}</v-chip>
            <v-icon icon="mdi-arrow-right-thick" size="18" color="primary" />
            <v-chip size="small" color="success" variant="tonal">
              {{ exitServer.region ? `${formatRegionBadge(exitServer.region)} ` : '' }}{{ exitServer.name }} ({{ exitServer.address }})
            </v-chip>
          </div>
        </v-sheet>
      </v-card-text>

      <v-card-actions class="px-5 pb-4">
        <v-spacer />
        <v-btn variant="text" :disabled="busy" @click="show = false">Отмена</v-btn>
        <v-btn color="primary" variant="flat" prepend-icon="mdi-check" :loading="busy" :disabled="!canSubmit" @click="submit">
          Связать и опубликовать
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.preview-route {
  border: 1px dashed rgba(43, 184, 148, 0.45);
  background: rgba(43, 184, 148, 0.06);
}
</style>
