<script setup lang="ts">
import { advanced } from '@/composables/operatorMode'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  logout: []
}>()

const router = useRouter()

const navigation = [
  { label: 'Обзор', path: '/overview', icon: 'mdi-view-dashboard-outline', group: 'Обзор' },
  { label: 'Пользователи', path: '/users', icon: 'mdi-account-multiple-outline', group: 'Управление' },
  { label: 'Серверы', path: '/servers', icon: 'mdi-server', group: 'Управление' },
  { label: 'Балансировка', path: '/pools', icon: 'mdi-lan', group: 'Управление' },
  { label: 'IMAP Mailboxes', path: '/mailboxes', icon: 'mdi-email-outline', group: 'Управление' },
  { label: 'Incident Reports', path: '/operator-incidents', icon: 'mdi-alert-outline', group: 'Управление' },
  { label: 'Профили и маршруты', path: '/profile-topology', icon: 'mdi-routes', group: 'Управление' },
  { label: 'Route Maps', path: '/route-maps', icon: 'mdi-map-marker-path', group: 'Управление' },
  { label: 'User Groups', path: '/groups', icon: 'mdi-wallet-membership', group: 'Управление' },
  { label: 'Трафик', path: '/statistics', icon: 'mdi-chart-line', group: 'Наблюдение' },
]
const simplePaths = ['/overview', '/servers', '/profile-topology', '/users', '/pools']
const visible = computed(() => navigation.filter(item => advanced.value || simplePaths.includes(item.path)))
const groups = computed(() => [...new Set(visible.value.map(item => item.group))])
</script>

<template>
  <v-navigation-drawer :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)">
    <v-list-item title="ANet" subtitle="Control Plane" link @click="router.push('/overview')">
      <template #prepend>
        <v-avatar color="primary" size="34">A</v-avatar>
      </template>
    </v-list-item>

    <v-divider />

    <v-list nav density="comfortable" color="primary">
      <template v-for="group in groups" :key="group">
        <v-list-subheader>{{ group }}</v-list-subheader>
        <v-list-item
            v-for="item in visible.filter(entry => entry.group === group)"
            :key="item.path"
            :to="item.path"
            :prepend-icon="item.icon"
            :title="item.label"
        />
      </template>
    </v-list>

    <template #append>
      <v-divider />
      <div class="pa-3">
        <v-switch v-model="advanced" label="Расширенный режим" density="compact" color="primary" hide-details />
        <v-btn block variant="tonal" color="error" prepend-icon="mdi-logout" @click="emit('logout')">
          Выйти
        </v-btn>
      </div>
    </template>
  </v-navigation-drawer>
</template>
