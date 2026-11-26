<script setup lang="ts">
import { useRouter } from 'vue-router'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  logout: []
}>()

const router = useRouter()

interface NavItem {
  label: string
  path: string
  icon: string
  group: string
}

const navigation: NavItem[] = [
  { label: 'Топология и обзор', path: '/overview', icon: 'mdi-hub-outline', group: 'Обзор' },
  { label: 'Пользователи и конфиги', path: '/users', icon: 'mdi-account-multiple-outline', group: 'Управление' },
  { label: 'Серверы', path: '/servers', icon: 'mdi-server', group: 'Управление' },
  { label: 'Балансировщик и пулы', path: '/pools', icon: 'mdi-scale-balance', group: 'Управление' },
  { label: 'Группы', path: '/groups', icon: 'mdi-account-group-outline', group: 'Управление' },
  { label: 'Маршруты и правила', path: '/route-maps', icon: 'mdi-map-marker-path', group: 'Управление' },
  { label: 'Топология потоков', path: '/profile-topology', icon: 'mdi-routes', group: 'Управление' },
  { label: 'Почтовые ящики', path: '/mailboxes', icon: 'mdi-email-outline', group: 'Управление' },
  { label: 'Статистика', path: '/statistics', icon: 'mdi-chart-line', group: 'Наблюдение' },
  { label: 'Инциденты', path: '/operator-incidents', icon: 'mdi-alert-outline', group: 'Наблюдение' },
  { label: 'Настройки', path: '/settings', icon: 'mdi-cog-outline', group: 'Система' },
]

const groups = ['Обзор', 'Управление', 'Наблюдение', 'Система']
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
          v-for="item in navigation.filter(entry => entry.group === group)"
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
        <v-btn block variant="tonal" color="error" prepend-icon="mdi-logout" @click="emit('logout')">
          Выйти
        </v-btn>
      </div>
    </template>
  </v-navigation-drawer>
</template>
