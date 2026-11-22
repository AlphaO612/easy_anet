<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'

const blank = () => ({ observer: '', direction: '', transport: '', evidence: '' })
type Report = { id: string; operator_id: string; reported_at: string; observation: ReturnType<typeof blank> }
const form = reactive(blank())
const rows = ref<Report[]>([])
const message = ref('')
const busy = ref(false)
const ready = computed(() => Object.values(form).every(v => v.trim()))
async function load() {
  try { rows.value = await api<Report[]>('/operator-incidents') }
  catch { message.value = 'Не удалось загрузить сообщения.' }
}
async function submit() {
  busy.value = true
  try {
    const row = await api<Report>('/operator-incidents', {method:'POST', data:{...form}})
    rows.value = [row, ...rows.value].slice(0, 200)
    Object.assign(form, blank())
    message.value = 'Сообщение сохранено.'
  } catch { message.value = 'Не удалось сохранить сообщение.' }
  finally { busy.value = false }
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
      <v-btn type="submit" :disabled="busy || !ready">Записать сообщение</v-btn>
    </v-form>
    <p role="status">{{ message }}</p>
    <v-table><thead><tr><th>Время</th><th>Оператор</th><th>Наблюдатель</th><th>Направление</th><th>Транспорт</th><th>Наблюдение</th></tr></thead>
      <tbody><tr v-for="r in rows" :key="r.id"><td>{{ new Date(r.reported_at).toLocaleString() }}</td><td>{{ r.operator_id }}</td><td>{{ r.observation.observer }}</td><td>{{ r.observation.direction }}</td><td>{{ r.observation.transport }}</td><td>{{ r.observation.evidence }}</td></tr></tbody>
    </v-table>
  </v-container>
</template>
