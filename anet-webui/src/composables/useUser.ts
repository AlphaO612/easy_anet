import { ref } from 'vue'
import { DeleteUser, GetUser, Regenerate, ResetUserTraffic, UpdateUser } from '@/api/users'
import type { User } from '@/models/user'

export function useUser() {
  const user = ref<User | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const regenerating = ref(false)

  const loadUser = async (id: string) => {
    loading.value = true
    try {
      user.value = await GetUser(id)
    } finally {
      loading.value = false
    }
  }

  const saveUser = async () => {
    if (!user.value) return
    saving.value = true
    try {
      const trimmedIp = user.value.static_ip?.trim() || ''
      await UpdateUser(user.value.id, {
        uid: user.value.uid.trim(),
        is_active: user.value.is_active,
        static_ip: trimmedIp || undefined,
        clear_static_ip: !trimmedIp,
        server_ids: user.value.server_ids || [],
        pool_ids: user.value.pool_ids || [],
        route_map_id: user.value.route_map_id || undefined,
        clear_route_map: !user.value.route_map_id,
        group_id: user.value.group_id || undefined,
        clear_group: !user.value.group_id,
      })
    } finally {
      saving.value = false
    }
  }

  const regenerate = async () => {
    if (!user.value) return
    regenerating.value = true
    try {
      const updated = await Regenerate(user.value.id)
      user.value.fingerprint = updated.fingerprint
      return updated
    } finally {
      regenerating.value = false
    }
  }

  const removeUser = async () => {
    if (!user.value) return
    await DeleteUser(user.value.id)
  }

  const resetTraffic = async () => {
    if (!user.value) return
    await ResetUserTraffic(user.value.id)
  }

  return {
    user,
    loading,
    saving,
    regenerating,
    loadUser,
    saveUser,
    regenerate,
    removeUser,
    resetTraffic,
  }
}
