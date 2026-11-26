<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'
import Navigation from '@/components/Navigation.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { logout as apiLogout } from '@/api/auth'

const route = useRoute()
const router = useRouter()
const { mobile } = useDisplay()

const drawer = ref(!mobile.value)
const snackbar = ref({ open: false, message: '', color: 'info' })
const pageTitle = computed(() => String(route.meta.title || 'ANet'))

// На мобильном drawer — оверлей поверх контента, поэтому закрываем его после
// перехода; на десктопе он постоянно закреплён и закрывать его не нужно.
watch(() => route.path, () => {
  if (mobile.value) drawer.value = false
})

const onMessage = (event: Event) => {
  const detail = (event as CustomEvent<{ kind: string; message: string }>).detail
  snackbar.value = { open: true, message: detail.message, color: detail.kind }
}

onMounted(() => window.addEventListener('anet:message', onMessage))
onBeforeUnmount(() => window.removeEventListener('anet:message', onMessage))

const handleLogout = async () => {
  try {
    await apiLogout()
  } catch {
    // Даже если сессия на сервере уже истекла, локальный токен всё равно очищаем.
  } finally {
    localStorage.removeItem('token')
    router.push('/')
  }
}
</script>

<template>
  <v-app>
    <router-view v-if="route.meta.isAuth" />

    <template v-else>
      <Navigation v-model="drawer" @logout="handleLogout" />

      <v-app-bar flat border>
        <v-app-bar-nav-icon @click="drawer = !drawer" />
        <v-toolbar-title>{{ pageTitle }}</v-toolbar-title>
        <v-spacer />
      </v-app-bar>

      <v-main>
        <v-container fluid>
          <router-view />
        </v-container>
      </v-main>
    </template>

    <ConfirmDialog />

    <v-snackbar v-model="snackbar.open" :color="snackbar.color" :timeout="4000">
      {{ snackbar.message }}
    </v-snackbar>
  </v-app>
</template>
