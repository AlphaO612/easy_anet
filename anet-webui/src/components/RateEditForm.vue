<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { User } from '@/models/user'
import type { RateReqRequest } from '@/models/rate'
import { DeleteRate } from '@/api/rate'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError, toDateTimeLocal } from '@/utils/format'

const user = defineModel<User>('modelValue', {
  required: true,
})

defineProps<{
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'save', payload: RateReqRequest): void
  (e: 'deleted'): void
}>()

const { confirmAction } = useConfirmDialog()
const message = useAppMessage()

const sessions = ref<number>(0)
const dateEnd = ref<string>(toDateTimeLocal())
const deleting = ref(false)
const deleteError = ref('')

watch(
  () => user.value.rate,
  (rate) => {
    if (!rate) return
    sessions.value = Number(rate.sessions ?? 0)
    dateEnd.value = toDateTimeLocal(rate.date_end)
    deleteError.value = ''
  },
  { immediate: true, deep: true },
)

const canSubmit = computed(() => {
  const s = Number(sessions.value)
  return !Number.isNaN(s) && s >= 0 && Boolean(dateEnd.value?.trim())
})

const handleSave = () => {
  if (!user.value.rate || !canSubmit.value) return
  const payload: RateReqRequest = {
    sessions: Math.max(0, Math.floor(Number(sessions.value) || 0)),
    date_end: dateEnd.value.trim(),
  }
  user.value.rate.sessions = payload.sessions
  user.value.rate.date_end = payload.date_end
  emit('save', payload)
}

const handleDelete = async () => {
  const rate = user.value.rate
  if (!rate || deleting.value) return

  const accepted = await confirmAction({
    title: 'Удалить тариф?',
    message: `Тариф пользователя «${user.value.uid}» будет удалён без возможности восстановления.`,
    confirmText: 'Удалить тариф',
    confirmColor: 'error',
  })
  if (!accepted) return

  deleting.value = true
  deleteError.value = ''
  try {
    await DeleteRate(rate.id)
    user.value.rate = null
    message.success('Тариф удалён')
    emit('deleted')
  } catch (e: unknown) {
    deleteError.value = extractApiError(e, 'Не удалось удалить тариф')
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div v-if="user.rate">
    <div class="text-subtitle-1 font-weight-bold mb-3">Редактировать тариф</div>

    <v-alert
      v-if="deleteError"
      type="error"
      variant="tonal"
      density="compact"
      class="mb-3"
      closable
      @click:close="deleteError = ''"
    >
      {{ deleteError }}
    </v-alert>

    <v-form @submit.prevent="handleSave">
      <v-text-field
        v-model.number="sessions"
        type="number"
        min="0"
        step="1"
        label="Лимит одновременных сессий"
        hint="0 — без ограничения сессий"
        persistent-hint
        variant="outlined"
        density="comfortable"
        class="mb-3"
      />

      <v-text-field
        v-model="dateEnd"
        label="Дата окончания тарифа"
        type="datetime-local"
        variant="outlined"
        density="comfortable"
        :rules="[(v: string) => !!v || 'Укажите дату окончания']"
        class="mb-3"
      />

      <div class="d-flex flex-wrap ga-2 align-center">
        <v-btn
          type="submit"
          color="primary"
          variant="flat"
          prepend-icon="mdi-content-save"
          :loading="saving"
          :disabled="!canSubmit || deleting"
        >
          Сохранить тариф
        </v-btn>

        <v-btn
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete-outline"
          :loading="deleting"
          :disabled="saving"
          @click="handleDelete"
        >
          Удалить тариф
        </v-btn>
      </div>
    </v-form>
  </div>
</template>
