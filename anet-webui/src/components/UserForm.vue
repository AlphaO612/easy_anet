<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import type { User } from '@/models/user'
import { GetServers } from '@/api/servers'
import type { Server } from '@/models/server'
import { GetPools } from '@/api/pools'
import type { NodePool } from '@/models/pool'
import { GetRouteMaps } from '@/api/route-maps'
import type { RouteMap } from '@/models/route-map'
import { GetGroups } from '@/api/groups'
import type { UserGroup } from '@/models/group'
import { extractApiError, formatDateTime } from '@/utils/format'

const user = defineModel<User>('modelValue', {
  required: true,
})

const valid = defineModel<boolean>('valid', { default: false })

const props = defineProps<{
  creating?: boolean
  hideStatus?: boolean
  hideReadonly?: boolean
}>()

const emit = defineEmits<{ (event: 'submit'): void }>()

const availableServers = ref<Server[]>([])
const availablePools = ref<NodePool[]>([])
const availableRouteMaps = ref<RouteMap[]>([])
const availableGroups = ref<UserGroup[]>([])
const dictLoading = ref(false)
const dictError = ref('')

const serverOptions = computed(() =>
  availableServers.value.map((s) => ({
    title: `${s.name} (${s.address})`,
    value: s.id,
  })),
)

const poolOptions = computed(() =>
  availablePools.value.map((pool) => ({
    title: `${pool.name} · ${pool.strategy}`,
    value: pool.id,
  })),
)

const routeMapOptions = computed(() =>
  availableRouteMaps.value.map((map) => ({
    title: `${map.name} · rev ${map.revision}`,
    value: map.id,
  })),
)

const groupOptions = computed(() =>
  availableGroups.value.map((group) => ({
    title: `${group.name} (+${group.duration_days} дн.)`,
    value: group.id,
  })),
)

const uidRules = [
  (v: string) => !!v?.trim() || 'UID обязателен',
  (v: string) => {
    const len = Array.from((v ?? '').trim()).length
    return (len >= 2 && len <= 64) || 'Длина имени должна быть от 2 до 64 символов'
  },
]

const loadDictionaries = async () => {
  dictLoading.value = true
  dictError.value = ''
  try {
    ;[
      availableServers.value,
      availablePools.value,
      availableRouteMaps.value,
      availableGroups.value,
    ] = await Promise.all([
      GetServers(),
      GetPools(),
      GetRouteMaps(),
      GetGroups(),
    ])
  } catch (e: unknown) {
    dictError.value = extractApiError(e, 'Не удалось загрузить справочники серверов и групп')
  } finally {
    dictLoading.value = false
  }
}

onMounted(() => {
  if (!user.value.server_ids) user.value.server_ids = []
  if (!user.value.pool_ids) user.value.pool_ids = []
  void loadDictionaries()
})
</script>

<template>
  <v-form v-model="valid" @submit.prevent="emit('submit')">
    <v-alert
      v-if="dictError"
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-3"
    >
      {{ dictError }}
    </v-alert>

    <v-text-field
      v-model="user.uid"
      label="Имя пользователя (UID)"
      placeholder="например, ivan-petrov"
      maxlength="64"
      :rules="uidRules"
      counter="64"
      variant="outlined"
      density="comfortable"
      class="mb-3"
      autofocus
    />

    <details :open="!props.creating" class="optional-section">
      <summary v-if="props.creating" class="text-body-2 font-weight-medium text-medium-emphasis mb-3 cursor-pointer">
        Группа и маршруты ANet (необязательно)
      </summary>

      <v-select
        v-model="user.group_id"
        clearable
        :loading="dictLoading"
        :items="groupOptions"
        item-title="title"
        item-value="value"
        label="Группа пользователей"
        placeholder="Без группы"
        variant="outlined"
        density="comfortable"
        class="mb-3"
      />

      <v-select
        v-model="user.pool_ids"
        multiple
        chips
        closable-chips
        :loading="dictLoading"
        :items="poolOptions"
        item-title="title"
        item-value="value"
        label="Балансируемые пулы (ANet)"
        placeholder="Выберите пулы для пользователя"
        variant="outlined"
        density="comfortable"
        class="mb-3"
      />

      <v-select
        v-model="user.route_map_id"
        clearable
        :loading="dictLoading"
        :items="routeMapOptions"
        item-title="title"
        item-value="value"
        label="Карта маршрутизации (Route map)"
        placeholder="Политика маршрутизации по умолчанию"
        variant="outlined"
        density="comfortable"
        class="mb-3"
      />

      <template v-if="!props.creating">
        <v-switch
          v-if="!props.hideStatus"
          v-model="user.is_active"
          :label="user.is_active ? 'Статус: Активен' : 'Статус: Забанен'"
          color="success"
          inset
          density="comfortable"
          class="mb-2"
        />

        <v-text-field
          v-model="user.static_ip"
          clearable
          label="Статический IP (ANet)"
          placeholder="например, 10.0.0.10"
          hint="Оставьте пустым для автоматического выделения IP"
          persistent-hint
          variant="outlined"
          density="comfortable"
          class="mb-3"
        />
      </template>

      <v-select
        v-model="user.server_ids"
        multiple
        chips
        closable-chips
        :loading="dictLoading"
        :items="serverOptions"
        item-title="title"
        item-value="value"
        label="Привязанные серверы (Локации ANet)"
        placeholder="Выберите серверы для пользователя"
        variant="outlined"
        density="comfortable"
      />
    </details>

    <template v-if="!props.creating && !props.hideReadonly">
      <v-divider class="my-4" />

      <v-text-field
        :model-value="user.fingerprint"
        label="Fingerprint ключа ANet"
        variant="outlined"
        density="comfortable"
        readonly
        class="mb-3 mono-field"
      />
      <v-text-field
        :model-value="formatDateTime(user.created_at)"
        label="Дата создания"
        variant="outlined"
        density="comfortable"
        readonly
      />
    </template>
  </v-form>
</template>

<style scoped>
.cursor-pointer {
  cursor: pointer;
}

.mono-field :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
}
</style>
