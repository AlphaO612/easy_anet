<script setup lang="ts">
import { ref, watch } from 'vue'
import NodeInvitationSteps from './NodeInvitationSteps.vue'
import { useRouter } from 'vue-router'
import { CreateServer, IssueNodeInvitation } from '@/api/servers'
import { advanced } from '@/composables/operatorMode'
import type { CreateServerRequest, NodeCredential, Server } from '@/models/server'

const show = defineModel<boolean>()
const emit = defineEmits<{ created: [] }>()
const router = useRouter()
const defaults = (): CreateServerRequest => ({ name: '', address: '', public_key: '', ssh_user: null, quic_port: 4519, ssh_port: 22, is_active: true })
const form = ref(defaults())
const server = ref<Server>()
const invitation = ref<NodeCredential>()
const busy = ref(false)
const error = ref('')
const role = ref('entry')
watch(show, open => {
  if (!open) return
  form.value = defaults()
  server.value = invitation.value = undefined
  role.value = 'entry'
  error.value = ''
})
async function submit() {
  busy.value = true
  error.value = ''
  try {
    if (!server.value) {
      server.value = await CreateServer(form.value)
      emit('created')
    }
    invitation.value = await IssueNodeInvitation(server.value.id)
  } catch { error.value = server.value ? 'Сервер сохранён. Выпустите приглашение ещё раз.' : 'Не удалось сохранить сервер. Проверьте адрес и порты.' }
  finally { busy.value = false }
}
function next(pool = false) {
  if (!server.value) return
  show.value = false
  router.push(pool ? {path: '/pools', query: { member: server.value.id }} :
    {path: '/profile-topology', query: { [role.value]: server.value.id }})
}
</script>
<template>
  <v-dialog v-model="show" max-width="650" :persistent="busy || !!invitation">
    <v-card :title="invitation ? 'Присоединить сервер' : 'Добавить сервер'">
      <v-card-text>
        <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
        <template v-if="!server">
          <v-text-field v-model.trim="form.name" label="Название сервера" placeholder="Например, Хельсинки" />
          <v-text-field v-model.trim="form.address" label="Публичный IP или домен VPS" />
          <v-select v-model="role" label="Роль в новом маршруте" :items="[{ title:'Вход — принимает клиентов',value:'entry' },{ title:'Выход — выпускает трафик в интернет',value:'exit' }]" />
          <v-text-field v-model.number="form.quic_port" label="Порт ANet (UDP)" type="number" min="1" max="65535" />
          <v-text-field v-model.trim="form.ssh_user" label="Пользователь SSH" placeholder="root" />
          <v-text-field v-model.number="form.ssh_port" label="Порт SSH" type="number" min="1" max="65535" />
          <template v-if="advanced">
            <v-text-field v-model="form.public_key" label="Публичный ключ (узел может сообщить при регистрации)" />
            <v-text-field v-model.number="form.vnc_port" label="Порт VNC" type="number" />
            <v-text-field v-model="form.websocket_url" label="WebSocket URL" />
            <v-text-field v-model="form.ahttp_url" label="AHTTP URL" />
            <v-text-field v-model="form.wrtc_url" label="WRTC URL" />
            <v-select v-if="form.wrtc_url" v-model="form.wrtc_mode" label="Режим WRTC" :items="['auto','media_video','p2p_direct','jvb_datachannel','ws']" />
          </template>
        </template>
        <template v-if="invitation">
          <NodeInvitationSteps v-if="server" :server="server" :invitation="invitation" />
        </template>
      </v-card-text>
      <v-card-actions class="flex-wrap">
        <v-btn :disabled="busy" @click="show = false">{{ invitation ? 'Завершить' : 'Закрыть' }}</v-btn>
        <v-spacer />
        <v-btn v-if="!invitation" color="primary" :loading="busy" :disabled="!form.name || !form.address || !form.quic_port || form.quic_port < 1 || form.quic_port > 65535" @click="submit">{{ server ? 'Выпустить приглашение' : 'Создать и пригласить' }}</v-btn>
        <template v-else>
          <v-btn v-if="role === 'entry'" @click="next(true)">Балансировка входов</v-btn>
          <v-btn color="primary" @click="next()">Назначить маршрут</v-btn>
        </template>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
