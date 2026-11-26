<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useAppMessage } from '@/composables/useAppMessage'
import { extractApiError, formatDateTime } from '@/utils/format'

const { confirmAction } = useConfirmDialog()
const appMessage = useAppMessage()

const blank = () => ({ observer: '', direction: '', transport: '', evidence: '' })
type Report = { id: string; operator_id: string; reported_at: string; observation: ReturnType<typeof blank> }

const form = reactive(blank())
const rows = ref<Report[]>([])
const message = ref('')
const busy = ref(false)
const deletingId = ref<string | null>(null)
const ready = computed(() => Object.values(form).every(v => v.trim()))

async function load() {
  try {
    rows.value = await api<Report[]>('/operator-incidents')
  } catch (err) {
    message.value = 'Не удалось загрузить сообщения.'
    appMessage.error(extractApiError(err, 'Не удалось загрузить сообщения об инцидентах'))
  }
}

async function submit() {
  busy.value = true
  try {
    const row = await api<Report>('/operator-incidents', { method: 'POST', data: { ...form } })
    rows.value = [row, ...rows.value].slice(0, 200)
    Object.assign(form, blank())
    message.value = 'Сообщение сохранено.'
    appMessage.success('Сообщение об инциденте сохранено')
  } catch (err) {
    message.value = 'Не удалось сохранить сообщение.'
    appMessage.error(extractApiError(err, 'Не удалось сохранить сообщение'))
  } finally {
    busy.value = false
  }
}

async function removeReport(report: Report) {
  const accepted = await confirmAction({
    title: 'Удалить сообщение об инциденте?',
    message: `Удалить запись от ${formatDateTime(report.reported_at)} (${report.observation.direction || report.id})?`,
    confirmText: 'Удалить',
    confirmColor: 'error',
  })
  if (!accepted) return
  deletingId.value = report.id
  try {
    await api('/operator-incidents/' + encodeURIComponent(report.id), { method: 'DELETE' })
    rows.value = rows.value.filter(r => r.id !== report.id)
    appMessage.success('Сообщение об инциденте удалено')
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось удалить сообщение'))
  } finally {
    deletingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <v-container>
    <h1>Ручные сообщения об инцидентах</h1>
    <v-form @submit.prevent="submit">
      <v-text-field v-model="form.observer" label="Точка наблюдения" maxlength="256" />
      <v-text-field v-model="form.direction" label="Направление (вход → выход)" maxlength="512" />
      <v-text-field v-model="form.transport" label="Транспорт" maxlength="128" />
      <v-textarea v-model="form.evidence" label="Наблюдаемые признаки и результат внешней проверки" maxlength="8192" />
      <v-btn type="submit" :disabled="busy || !ready" :loading="busy">Записать сообщение</v-btn>
    </v-form>
    <p role="status">{{ message }}</p>
    <v-table>
      <thead>
        <tr>
          <th>Время</th>
          <th>Оператор</th>
          <th>Наблюдатель</th>
          <th>Направление</th>
          <th>Транспорт</th>
          <th>Наблюдение</th>
          <th class="text-center">Действия</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ formatDateTime(r.reported_at) }}</td>
          <td>{{ r.operator_id }}</td>
          <td>{{ r.observation.observer }}</td>
          <td>{{ r.observation.direction }}</td>
          <td>{{ r.observation.transport }}</td>
          <td>{{ r.observation.evidence }}</td>
          <td class="text-center">
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="Удалить сообщение"
              :loading="deletingId === r.id"
              @click="removeReport(r)"
            />
          </td>
        </tr>
      </tbody>
    </v-table>
  </v-container>
</template>
