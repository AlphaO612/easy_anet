<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import type { Server } from '@/models/server'

import { probeStatus, type ProbeEdge as Edge, type ProbeRevision as Manifest, type ProbeStatus as Status } from '@/api/probes'

const props = defineProps<{ server: Server; servers: Server[] }>()
const emit = defineEmits<{ close: [] }>()
const path = `/servers/${encodeURIComponent(props.server.id)}/probe-revision`
const status = ref<Status>()
const base = ref<number>()
const draft = ref<Edge[]>([])
const busy = ref(false)
const error = ref('')
const preview = ref(false)
const targetNodes = computed(() => props.servers.filter(server => server.id !== props.server.id))
const conflict = computed(() => status.value && base.value !== status.value.desired)
const label = (edge: Edge) => JSON.stringify([edge.target_node, edge.module, edge.target])
const published = computed(() => status.value?.manifest?.edges || [])
const additions = computed(() => difference(draft.value, published.value))
const removals = computed(() => difference(published.value, draft.value))
const changed = computed(() => additions.value.length > 0 || removals.value.length > 0)
const complete = computed(() => draft.value.every(edge => edge.target.trim() && edge.target_node)
  && new Set(draft.value.map(label)).size === draft.value.length)

function difference(a: Edge[], b: Edge[]) {
  const previous = new Set(b.map(label))
  return a.filter(edge => !previous.has(label(edge)))
}

function resetDraft() {
  if (!status.value) return
  base.value = status.value.desired
  draft.value = published.value.map(edge => ({ ...edge }))
  preview.value = false
}

async function refresh() {
  status.value = await probeStatus(props.server.id)
  if (base.value === undefined) resetDraft()
}

async function load() {
  busy.value = true
  error.value = ''
  try { await refresh() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : 'Ошибка чтения ревизии' }
  finally { busy.value = false }
}

async function publish() {
  if (!preview.value || conflict.value || !complete.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const manifest = await api<Manifest>(path, {
      method: 'PUT', data: { base_revision: base.value, edges: draft.value },
    })
    base.value = manifest.revision
    status.value = { desired: manifest.revision, applied: status.value?.applied ?? 0, manifest }
    preview.value = false
    await refresh()
  } catch (cause) {
    if (axios.isAxiosError(cause) && cause.response?.status === 409) {
      error.value = 'Во время публикации ревизия изменилась. Черновик сохранён.'
      try { await refresh() } catch { error.value += ' Обновите статус.' }
    } else {
      error.value = axios.isAxiosError(cause) && typeof cause.response?.data === 'string'
        ? cause.response.data : cause instanceof Error ? cause.message : 'Ошибка публикации'
    }
  } finally { busy.value = false }
}
onMounted(load)
</script>

<template>
  <v-dialog :model-value="true" :persistent="busy || changed" max-width="850" @update:model-value="emit('close')">
    <v-card :title="`Проверки связности · ${server.name}`">
      <v-card-text>
        <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
        <div v-if="status" class="mb-3">
          Опубликована ревизия {{ status.desired }} · применена {{ status.applied }}
          <v-alert v-if="status.error" type="warning">{{ status.error }}</v-alert>
        </div>
        <v-alert v-if="conflict" type="warning" class="mb-3">
          Черновик основан на ревизии {{ base }}. Для продолжения загрузите опубликованную версию.
          <v-btn variant="text" @click="resetDraft">Заменить черновик опубликованной версией</v-btn>
        </v-alert>
        <template v-if="status && !preview">
          <div v-for="(edge, i) in draft" :key="i" class="d-flex flex-wrap ga-2 align-start mb-3">
            <v-select v-model="edge.target_node" :items="targetNodes" item-title="name" item-value="id" label="Целевой узел" density="compact" />
            <v-select v-model="edge.module" :items="['tcp_connect', 'https_ready', 'https_capacity']" label="Проверка" density="compact" />
            <v-text-field v-model.trim="edge.target" :label="edge.module !== 'tcp_connect' ? 'HTTPS URL' : 'Хост:порт'" density="compact" />
            <v-btn icon="mdi-delete" variant="text" title="Удалить проверку" @click="draft.splice(i, 1)" />
          </div>
          <v-btn :disabled="!targetNodes.length || draft.length >= 256" @click="draft.push({ target_node: '', module: 'tcp_connect', target: '' })">Добавить проверку</v-btn>
        </template>
        <template v-if="preview">
          <h3>Изменения к ревизии {{ base }}</h3>
          <p v-for="edge in additions" :key="'add'+label(edge)">Добавить · {{ servers.find(n => n.id === edge.target_node)?.name ?? edge.target_node }} · {{ edge.module }} · {{ edge.target }}</p>
          <p v-for="edge in removals" :key="'remove'+label(edge)">Удалить · {{ servers.find(n => n.id === edge.target_node)?.name ?? edge.target_node }} · {{ edge.module }} · {{ edge.target }}</p>
        </template>
      </v-card-text>
      <v-card-actions class="flex-wrap">
        <v-btn :disabled="busy" @click="emit('close')">{{ changed ? 'Закрыть без сохранения' : 'Закрыть' }}</v-btn>
        <v-spacer />
        <v-btn :loading="busy" @click="load">Обновить статус</v-btn>
        <v-btn v-if="preview" :disabled="busy" @click="preview = false">Назад</v-btn>
        <v-btn v-if="preview" color="primary" :disabled="conflict || busy" @click="publish">Опубликовать</v-btn>
        <v-btn v-else color="primary" :disabled="!complete || !changed || conflict || busy" @click="preview = true">Предпросмотр</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
