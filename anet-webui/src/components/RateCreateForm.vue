<script setup lang="ts">
import { computed, ref } from 'vue'
import type { RateReqRequest } from '@/models/rate'
import { toDateTimeLocal } from '@/utils/format'

defineProps<{
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'create', payload: RateReqRequest): void
}>()

const sessions = ref<number>(0)
const dateEnd = ref<string>(toDateTimeLocal())

const canSubmit = computed(() => {
  const s = Number(sessions.value)
  return !Number.isNaN(s) && s >= 0 && Boolean(dateEnd.value?.trim())
})

const handleCreate = () => {
  if (!canSubmit.value) return
  emit('create', {
    sessions: Math.max(0, Math.floor(Number(sessions.value) || 0)),
    date_end: dateEnd.value.trim(),
  })
}
</script>

<template>
  <div>
    <div class="text-subtitle-1 font-weight-bold mb-3">Назначить тариф</div>

    <v-form @submit.prevent="handleCreate">
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

      <v-btn
        type="submit"
        color="primary"
        variant="flat"
        prepend-icon="mdi-plus"
        :loading="loading"
        :disabled="!canSubmit"
      >
        Создать тариф
      </v-btn>
    </v-form>
  </div>
</template>
