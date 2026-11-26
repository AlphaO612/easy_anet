import { ref, type Ref } from 'vue'
import { AddRate, DeleteRate, UpdateRate } from '@/api/rate'
import type { RateReqRequest } from '@/models/rate'
import type { User } from '@/models/user'

export function useRate(user: Ref<User | null>) {
  const saving = ref(false)
  const deleting = ref(false)

  const saveRate = async (override?: RateReqRequest) => {
    if (!user.value?.rate) return
    saving.value = true
    try {
      const payload: RateReqRequest = override ?? {
        sessions: user.value.rate.sessions,
        date_end: user.value.rate.date_end,
      }
      const updated = await UpdateRate(user.value.rate.id, payload)
      if (user.value) {
        user.value.rate = updated
      }
      return updated
    } finally {
      saving.value = false
    }
  }

  const createRate = async (payload: RateReqRequest) => {
    if (!user.value) return
    saving.value = true
    try {
      const created = await AddRate(user.value.id, payload)
      if (user.value) {
        user.value.rate = created
      }
      return created
    } finally {
      saving.value = false
    }
  }

  const deleteRate = async () => {
    if (!user.value?.rate) return
    deleting.value = true
    try {
      await DeleteRate(user.value.rate.id)
      if (user.value) {
        user.value.rate = null
      }
    } finally {
      deleting.value = false
    }
  }

  return {
    saving,
    deleting,
    saveRate,
    createRate,
    deleteRate,
  }
}
