<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { GetGroups } from '@/api/groups'
import { DeleteUser, GetUsers, ResetUserTraffic, UpdateUser } from '@/api/users'
import type { UserGroup } from '@/models/group'
import type { User, UsersResponse } from '@/models/user'
import { useAppMessage } from '@/composables/useAppMessage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { extractApiError } from '@/utils/format'
import CreateUserModal from '@/components/CreateUserModal.vue'

interface TableSortItem {
  key: string
  order?: boolean | 'asc' | 'desc'
}

interface TableOptions {
  page: number
  itemsPerPage: number
  sortBy: TableSortItem[]
}

interface LegacyRecipient {
  id: string
  name: string
  user_id: string | null
  subscribed?: boolean
}

interface LegacyCatalog {
  groups: LegacyRecipient[]
}

const router = useRouter()
const message = useAppMessage()
const { confirmAction } = useConfirmDialog()

const data = ref<UsersResponse | null>(null)
const loading = ref(false)
const error = ref('')
let request = 0

const searchQuery = ref('')
const selectedGroupIds = ref<string[]>([])
const groups = ref<UserGroup[]>([])
const showCreate = ref(false)
const rowBusyId = ref('')

const options = ref<TableOptions>({
  page: 1,
  itemsPerPage: 10,
  sortBy: [],
})

const headers = [
  { title: 'UID (User Name)', key: 'uid', sortable: true },
  { title: 'Группа', key: 'group_name', sortable: false },
  { title: 'UUID (ID)', key: 'id', sortable: true },
  { title: 'Статус', key: 'is_active', sortable: true, align: 'center' as const },
  { title: 'Действия', key: 'actions', sortable: false, align: 'center' as const },
]

const items = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const groupMap = computed(() => new Map(groups.value.map((g) => [g.id, g])))
const groupOptions = computed(() => groups.value.map((g) => ({ title: g.name, value: g.id })))

// Legacy VLESS recipients migration state
const legacyRecipients = ref<LegacyRecipient[]>([])
const legacyRecipientId = ref('')
const legacyTargetUserId = ref('')
const legacyUserCandidates = ref<User[]>([])
const legacyBinding = ref(false)
const legacyError = ref('')
let legacySearchSeq = 0

const unlinkedLegacy = computed(() => legacyRecipients.value.filter((r) => !r.user_id))
const legacyUserOptions = computed(() => {
  const merged = new Map<string, User>()
  for (const u of items.value) merged.set(u.id, u)
  for (const u of legacyUserCandidates.value) merged.set(u.id, u)
  return [...merged.values()].map((u) => ({
    title: u.uid || u.id,
    subtitle: u.is_active ? u.id : `${u.id} · Забанен`,
    value: u.id,
  }))
})

const openProfile = (id: string) => {
  void router.push(`/users/${encodeURIComponent(id)}`)
}

const loadUsers = async (
  page = options.value.page,
  itemsPerPage = options.value.itemsPerPage,
  search = searchQuery.value,
  groupIds = selectedGroupIds.value,
  sortBy = options.value.sortBy,
) => {
  const current = ++request
  loading.value = true
  error.value = ''
  try {
    const offset = (page - 1) * itemsPerPage
    const sortField = sortBy[0]?.key
    const isDesc = sortBy[0]?.order === 'desc'

    const response = await GetUsers(offset, itemsPerPage, search, groupIds, sortField, isDesc)
    if (current === request) data.value = response
  } catch (e: unknown) {
    if (current === request) error.value = extractApiError(e, 'Не удалось загрузить пользователей.')
  } finally {
    if (current === request) loading.value = false
  }
}

const loadGroupDictionary = async () => {
  try {
    groups.value = await GetGroups()
  } catch {
    // non-blocking dictionary load
  }
}

const loadLegacyRecipients = async () => {
  try {
    const catalog = await api<LegacyCatalog>('/vless-devices', { method: 'GET' })
    legacyRecipients.value = catalog.groups ?? []
    if (!unlinkedLegacy.value.some((r) => r.id === legacyRecipientId.value)) {
      legacyRecipientId.value = unlinkedLegacy.value[0]?.id ?? ''
    }
  } catch {
    legacyRecipients.value = []
  }
}

const searchLegacyUsers = async (query = '') => {
  const seq = ++legacySearchSeq
  try {
    const res = await GetUsers(0, 30, query)
    if (seq === legacySearchSeq) legacyUserCandidates.value = res.items
  } catch {
    // keep current candidates
  }
}

const handleOptions = (opts: TableOptions) => {
  options.value.page = opts.page
  options.value.itemsPerPage = opts.itemsPerPage
  options.value.sortBy = opts.sortBy || []

  void loadUsers(opts.page, opts.itemsPerPage, searchQuery.value, selectedGroupIds.value, opts.sortBy)
}

watch(selectedGroupIds, (newIds) => {
  options.value.page = 1
  void loadUsers(1, options.value.itemsPerPage, searchQuery.value, newIds, options.value.sortBy)
})

watch(searchQuery, (query) => {
  options.value.page = 1
  void loadUsers(1, options.value.itemsPerPage, query, selectedGroupIds.value, options.value.sortBy)
})

const handleCreatedUser = (user: Pick<User, 'id' | 'uid'>) => {
  openProfile(user.id)
}

const handleToggleBan = async (user: User) => {
  const nextActive = !user.is_active
  const accepted = await confirmAction({
    title: nextActive ? 'Разбанить пользователя?' : 'Забанить пользователя?',
    message: nextActive
      ? `Доступ пользователя «${user.uid}» будет восстановлен.`
      : `Доступ пользователя «${user.uid}» будет приостановлен.`,
    confirmText: nextActive ? 'Разбанить' : 'Забанить',
    confirmColor: nextActive ? 'success' : 'warning',
  })
  if (!accepted) return

  rowBusyId.value = user.id
  try {
    const trimmedIp = user.static_ip?.trim() || ''
    await UpdateUser(user.id, {
      uid: user.uid,
      is_active: nextActive,
      static_ip: trimmedIp || undefined,
      clear_static_ip: !trimmedIp,
      server_ids: user.server_ids || [],
      pool_ids: user.pool_ids || [],
      route_map_id: user.route_map_id || undefined,
      clear_route_map: !user.route_map_id,
      group_id: user.group_id || undefined,
      clear_group: !user.group_id,
    })
    user.is_active = nextActive
    message.success(nextActive ? `Пользователь «${user.uid}» разбанен` : `Пользователь «${user.uid}» забанен`)
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось изменить статус пользователя'))
  } finally {
    rowBusyId.value = ''
  }
}

const handleResetTraffic = async (user: User) => {
  const accepted = await confirmAction({
    title: 'Сбросить трафик?',
    message: `Счётчики трафика пользователя «${user.uid}» будут обнулены.`,
    confirmText: 'Сбросить трафик',
    confirmColor: 'warning',
  })
  if (!accepted) return

  rowBusyId.value = user.id
  try {
    await ResetUserTraffic(user.id)
    message.success(`Трафик пользователя «${user.uid}» сброшен`)
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось сбросить трафик'))
  } finally {
    rowBusyId.value = ''
  }
}

const handleDeleteUser = async (user: User) => {
  const accepted = await confirmAction({
    title: 'Удалить пользователя?',
    message: `Пользователь «${user.uid}» и все его устройства будут удалены без возможности восстановления.`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return

  rowBusyId.value = user.id
  try {
    await DeleteUser(user.id)
    message.success(`Пользователь «${user.uid}» удалён`)
    await loadUsers()
  } catch (e: unknown) {
    message.error(extractApiError(e, 'Не удалось удалить пользователя'))
  } finally {
    rowBusyId.value = ''
  }
}

const handleBindLegacy = async () => {
  if (!legacyRecipientId.value || !legacyTargetUserId.value || legacyBinding.value) return
  const recipient = unlinkedLegacy.value.find((r) => r.id === legacyRecipientId.value)
  const targetUser = legacyUserOptions.value.find((u) => u.value === legacyTargetUserId.value)

  const accepted = await confirmAction({
    title: 'Привязать legacy VLESS получателя?',
    message: `Все устройства и подписка получателя «${recipient?.name || legacyRecipientId.value}» будут привязаны к пользователю «${targetUser?.title || legacyTargetUserId.value}».`,
    confirmText: 'Привязать',
    confirmColor: 'primary',
  })
  if (!accepted) return

  legacyBinding.value = true
  legacyError.value = ''
  try {
    await api('/vless-devices', {
      method: 'POST',
      data: {
        user_id: legacyTargetUserId.value,
        recipient: legacyRecipientId.value,
      },
    })
    message.success('Legacy VLESS получатель привязан к пользователю')
    legacyRecipientId.value = ''
    legacyTargetUserId.value = ''
    await loadLegacyRecipients()
  } catch (e: unknown) {
    legacyError.value = extractApiError(e, 'Не удалось привязать legacy VLESS получателя')
  } finally {
    legacyBinding.value = false
  }
}

onMounted(() => {
  void loadGroupDictionary()
  void loadUsers()
  void loadLegacyRecipients()
})
</script>

<template>
  <v-container fluid class="users-page">
    <div class="d-flex justify-space-between align-center flex-wrap ga-4 mb-4">
      <v-list-item
        class="px-0"
        subtitle="Управление учетными записями пользователей, устройствами, подписками и тарифами"
      >
        <template #title>
          <h1 class="text-h5 font-weight-bold">ANet VPN Clients (Пользователи)</h1>
        </template>
      </v-list-item>
    </div>

    <div class="d-flex justify-space-between align-center flex-wrap ga-3 mb-4">
      <div class="d-flex align-center flex-wrap ga-3 flex-grow-1">
        <v-text-field
          v-model="searchQuery"
          prepend-inner-icon="mdi-magnify"
          label="Поиск по UID..."
          variant="outlined"
          density="compact"
          hide-details
          single-line
          style="max-width: 280px"
        />

        <v-select
          v-model="selectedGroupIds"
          multiple
          clearable
          chips
          collapse-chips
          :items="groupOptions"
          item-title="title"
          item-value="value"
          label="Фильтр по группам"
          placeholder="Все группы"
          variant="outlined"
          density="compact"
          hide-details
          style="max-width: 320px"
        />
      </div>

      <v-btn color="primary" prepend-icon="mdi-plus" @click="showCreate = true">
        Добавить клиента
      </v-btn>
    </div>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">
      <div class="d-flex align-center justify-space-between flex-wrap ga-2">
        <span>{{ error }}</span>
        <v-btn variant="text" size="small" @click="loadUsers()">Повторить</v-btn>
      </div>
    </v-alert>

    <v-data-table-server
      v-model:items-per-page="options.itemsPerPage"
      v-model:page="options.page"
      :headers="headers"
      :items="items"
      :items-length="total"
      :loading="loading"
      :items-per-page-options="[10, 20, 50, 100]"
      items-per-page-text="На странице:"
      loading-text="Загрузка пользователей…"
      no-data-text="Пользователи не найдены"
      density="comfortable"
      class="border rounded-lg users-table"
      hover
      @update:options="handleOptions"
      @click:row="(_: unknown, payload: { item: User }) => openProfile(payload.item.id)"
    >
      <template #item.uid="{ item }">
        <span class="font-weight-bold">{{ item.uid || 'No Name' }}</span>
      </template>

      <template #item.group_name="{ item }">
        <v-chip
          v-if="item.group_id && groupMap.has(item.group_id)"
          size="small"
          variant="tonal"
          color="secondary"
        >
          {{ groupMap.get(item.group_id)?.name }}
        </v-chip>
        <span v-else class="text-caption text-medium-emphasis">—</span>
      </template>

      <template #item.id="{ item }">
        <code class="text-caption text-medium-emphasis">{{ item.id }}</code>
      </template>

      <template #item.is_active="{ item }">
        <v-chip :color="item.is_active ? 'success' : 'error'" size="small">
          {{ item.is_active ? 'Активен' : 'Забанен' }}
        </v-chip>
      </template>

      <template #item.actions="{ item }">
        <div class="d-flex ga-1 justify-center align-center flex-nowrap" @click.stop>
          <v-btn
            :to="`/users/${encodeURIComponent(item.id)}`"
            variant="tonal"
            color="primary"
            size="small"
            prepend-icon="mdi-account-details-outline"
          >
            Профиль
          </v-btn>
          <v-btn
            :icon="item.is_active ? 'mdi-account-cancel-outline' : 'mdi-account-check-outline'"
            size="small"
            variant="text"
            :color="item.is_active ? 'warning' : 'success'"
            :title="item.is_active ? 'Забанить пользователя' : 'Разбанить пользователя'"
            :loading="rowBusyId === item.id"
            :disabled="Boolean(rowBusyId && rowBusyId !== item.id)"
            @click="handleToggleBan(item)"
          />
          <v-btn
            icon="mdi-counter"
            size="small"
            variant="text"
            color="secondary"
            title="Сбросить трафик"
            :loading="rowBusyId === item.id"
            :disabled="Boolean(rowBusyId && rowBusyId !== item.id)"
            @click="handleResetTraffic(item)"
          />
          <v-btn
            icon="mdi-delete-outline"
            size="small"
            variant="text"
            color="error"
            title="Удалить пользователя"
            :loading="rowBusyId === item.id"
            :disabled="Boolean(rowBusyId && rowBusyId !== item.id)"
            @click="handleDeleteUser(item)"
          />
        </div>
      </template>
    </v-data-table-server>

    <v-expansion-panels v-if="unlinkedLegacy.length" class="mt-6">
      <v-expansion-panel>
        <v-expansion-panel-title>
          <div class="d-flex align-center ga-2">
            <v-icon icon="mdi-link-variant" size="small" color="warning" />
            <span class="font-weight-medium">
              Непривязанные legacy VLESS-получатели ({{ unlinkedLegacy.length }})
            </span>
          </div>
        </v-expansion-panel-title>
        <v-expansion-panel-text>
          <div class="text-body-2 text-medium-emphasis mb-3">
            Привяжите существующего legacy-получателя к пользователю панели, чтобы перенести его устройства и подписку.
          </div>

          <v-alert
            v-if="legacyError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-3"
            closable
            @click:close="legacyError = ''"
          >
            {{ legacyError }}
          </v-alert>

          <v-row dense align="center">
            <v-col cols="12" md="4">
              <v-select
                v-model="legacyRecipientId"
                :items="unlinkedLegacy"
                item-title="name"
                item-value="id"
                label="Legacy VLESS-получатель"
                variant="outlined"
                density="compact"
                hide-details
              />
            </v-col>
            <v-col cols="12" md="5">
              <v-autocomplete
                v-model="legacyTargetUserId"
                :items="legacyUserOptions"
                :item-props="(item: Record<string, unknown>) => ({ subtitle: String(item.subtitle ?? '') })"
                item-title="title"
                item-value="value"
                label="Пользователь панели"
                placeholder="Найдите пользователя по UID"
                variant="outlined"
                density="compact"
                hide-details
                no-filter
                no-data-text="Пользователи не найдены"
                @update:search="searchLegacyUsers"
              />
            </v-col>
            <v-col cols="12" md="3">
              <v-btn
                color="primary"
                variant="flat"
                block
                prepend-icon="mdi-link-plus"
                :loading="legacyBinding"
                :disabled="!legacyRecipientId || !legacyTargetUserId"
                @click="handleBindLegacy"
              >
                Привязать
              </v-btn>
            </v-col>
          </v-row>
        </v-expansion-panel-text>
      </v-expansion-panel>
    </v-expansion-panels>

    <CreateUserModal v-model="showCreate" @created="handleCreatedUser" />
  </v-container>
</template>

<style scoped>
.users-page {
  padding: 24px;
}

.users-table :deep(tbody tr) {
  cursor: pointer;
}
</style>
