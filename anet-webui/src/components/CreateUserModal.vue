<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CreateUser } from '@/api/users'
import type { CreateUserRequest, User } from '@/models/user'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError } from '@/utils/format'
import UserForm from '@/components/UserForm.vue'

const show = defineModel<boolean>()

const emit = defineEmits<{
  (e: 'created', user: Pick<User, 'id' | 'uid'>): void
}>()

const message = useAppMessage()

const loading = ref(false)
const error = ref('')
const formValidRef = ref(false)

const defaultDraft = (): User => ({
  id: '',
  fingerprint: '',
  uid: '',
  is_active: true,
  created_at: '',
  rate: null,
  static_ip: null,
  server_ids: [],
  pool_ids: [],
  route_map_id: null,
  group_id: null,
})

const draft = ref<User>(defaultDraft())

const uidLength = computed(() => Array.from(draft.value.uid.trim()).length)
const canSubmit = computed(() => uidLength.value >= 2 && uidLength.value <= 64 && !loading.value)

watch(show, (isOpen) => {
  if (isOpen) {
    error.value = ''
  } else {
    draft.value = defaultDraft()
    error.value = ''
  }
})

watch(
  () => draft.value.uid,
  () => {
    if (error.value) error.value = ''
  },
)

const create = async () => {
  if (loading.value) return
  const trimmedUid = draft.value.uid.trim()
  if (Array.from(trimmedUid).length < 2) {
    error.value = 'Укажите имя пользователя (минимум 2 символа).'
    return
  }
  if (Array.from(trimmedUid).length > 64) {
    error.value = 'Имя пользователя не должно превышать 64 символа.'
    return
  }

  error.value = ''
  loading.value = true
  try {
    const payload: CreateUserRequest = {
      uid: trimmedUid,
      server_ids: draft.value.server_ids || [],
      pool_ids: draft.value.pool_ids || [],
      route_map_id: draft.value.route_map_id || null,
      group_id: draft.value.group_id || null,
    }

    const created = await CreateUser(payload)
    message.success(`Пользователь «${created.uid}» создан`)
    emit('created', { id: created.id, uid: created.uid })
    show.value = false
    draft.value = defaultDraft()
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось создать пользователя')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <v-dialog v-model="show" :persistent="loading" max-width="620">
    <v-card>
      <v-card-title class="d-flex align-center justify-space-between text-h6 pt-5 px-6 pb-2">
        <span>Создать пользователя</span>
        <v-btn
          icon="mdi-close"
          variant="text"
          size="small"
          :disabled="loading"
          @click="show = false"
        />
      </v-card-title>

      <v-card-text class="px-6 pt-2">
        <v-alert
          v-if="error"
          type="error"
          variant="tonal"
          density="comfortable"
          class="mb-4"
          closable
          @click:close="error = ''"
        >
          {{ error }}
        </v-alert>

        <UserForm
          v-model="draft"
          v-model:valid="formValidRef"
          creating
          @submit="create"
        />
      </v-card-text>

      <v-divider />

      <v-card-actions class="px-6 py-4">
        <v-spacer />
        <v-btn variant="text" :disabled="loading" @click="show = false">
          Отмена
        </v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :loading="loading"
          :disabled="!canSubmit"
          @click="create"
        >
          Создать пользователя
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
