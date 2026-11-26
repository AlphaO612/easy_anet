<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { GetUserConfig } from '@/api/users'
import type { RateReqRequest } from '@/models/rate'
import { useAppMessage } from '@/composables/useAppMessage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useUser } from '@/composables/useUser'
import { useRate } from '@/composables/useRate'
import { downloadTextFile, extractApiError } from '@/utils/format'
import UserForm from './UserForm.vue'
import RateEditForm from './RateEditForm.vue'
import RateCreateForm from './RateCreateForm.vue'
import QrCodeBox from './QrCodeBox.vue'

const show = defineModel<boolean>()

const props = defineProps<{
  userId: string
}>()

const emit = defineEmits<{
  (e: 'updated'): void
  (e: 'close'): void
}>()

const router = useRouter()
const message = useAppMessage()
const { confirmAction } = useConfirmDialog()

const { user, loading, saving: savingUser, regenerating, loadUser, saveUser, regenerate } = useUser()
const { saving: savingRate, saveRate, createRate } = useRate(user)

const formValid = ref(true)
const error = ref('')
const configText = ref('')
const configLoading = ref(false)
const showQrSection = ref(false)

watch(
  () => [props.userId, show.value] as const,
  async ([id, isOpen]) => {
    configText.value = ''
    showQrSection.value = false
    error.value = ''
    if (isOpen && id) {
      try {
        await loadUser(id)
      } catch (e: unknown) {
        error.value = extractApiError(e, 'Не удалось загрузить пользователя')
      }
    }
  },
  { immediate: true },
)

const close = () => {
  show.value = false
  user.value = null
  configText.value = ''
  showQrSection.value = false
  error.value = ''
  emit('close')
}

const handleDialogUpdate = (val: boolean) => {
  if (!val) close()
}

const openFullProfile = () => {
  const id = user.value?.id || props.userId
  close()
  if (id) void router.push(`/users/${encodeURIComponent(id)}`)
}

const handleSaveUser = async () => {
  if (!user.value) return
  error.value = ''
  try {
    await saveUser()
    message.success('Данные пользователя сохранены')
    emit('updated')
    close()
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось сохранить пользователя')
  }
}

const handleRegenerate = async () => {
  if (!user.value) return
  const accepted = await confirmAction({
    title: 'Перегенерировать ключи ANet?',
    message: `Старые ключи ANet пользователя «${user.value.uid}» перестанут работать. Потребуется заново скачать client.toml.`,
    confirmText: 'Перегенерировать',
    confirmColor: 'warning',
  })
  if (!accepted) return

  error.value = ''
  try {
    await regenerate()
    configText.value = ''
    message.success('Ключи ANet перегенерированы')
    emit('updated')
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось перегенерировать ключи')
  }
}

const handleSaveRate = async (payload: RateReqRequest) => {
  error.value = ''
  try {
    await saveRate(payload)
    message.success('Тариф обновлён')
    emit('updated')
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось обновить тариф')
  }
}

const handleCreateRate = async (payload: RateReqRequest) => {
  error.value = ''
  try {
    await createRate(payload)
    message.success('Тариф создан')
    emit('updated')
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось создать тариф')
  }
}

const handleDeletedRate = () => {
  emit('updated')
}

const fetchConfigText = async (): Promise<string | null> => {
  if (!user.value) return null
  if (configText.value) return configText.value
  configLoading.value = true
  error.value = ''
  try {
    configText.value = await GetUserConfig(user.value.id)
    return configText.value
  } catch (e: unknown) {
    error.value = extractApiError(e, 'Не удалось загрузить client.toml')
    return null
  } finally {
    configLoading.value = false
  }
}

const handleDownloadConfig = async () => {
  const text = await fetchConfigText()
  if (!text || !user.value) return
  const safeName = user.value.uid.replace(/[^a-zA-Z0-9._-]/g, '_') || 'client'
  downloadTextFile(`${safeName}.toml`, text, 'application/toml;charset=utf-8')
  message.success(`Файл ${safeName}.toml скачан`)
}

const handleToggleQr = async () => {
  if (showQrSection.value) {
    showQrSection.value = false
    return
  }
  const text = await fetchConfigText()
  if (text) {
    showQrSection.value = true
  }
}
</script>

<template>
  <v-dialog
    :model-value="show"
    scrollable
    max-width="860"
    @update:model-value="handleDialogUpdate"
  >
    <v-card class="d-flex flex-column" style="max-height: 88vh;">
      <v-card-title class="d-flex align-center justify-space-between px-6 pt-5 pb-3 flex-shrink-0">
        <span class="text-h6">Редактировать пользователя</span>
        <div class="d-flex align-center ga-2">
          <v-btn
            v-if="user"
            variant="tonal"
            size="small"
            prepend-icon="mdi-open-in-new"
            @click="openFullProfile"
          >
            Полный профиль
          </v-btn>
          <v-btn icon="mdi-close" variant="text" size="small" @click="close" />
        </div>
      </v-card-title>

      <v-divider />

      <v-card-text class="px-6 py-4 flex-grow-1" style="overflow-y: auto;">
        <v-alert
          v-if="error"
          type="error"
          variant="tonal"
          class="mb-4"
          closable
          @click:close="error = ''"
        >
          {{ error }}
        </v-alert>

        <div v-if="loading" class="d-flex justify-center py-8">
          <v-progress-circular indeterminate color="primary" />
        </div>

        <template v-else-if="user">
          <UserForm
            v-model="user"
            v-model:valid="formValid"
            @submit="handleSaveUser"
          />

          <v-divider class="my-5" />

          <RateEditForm
            v-if="user.rate"
            v-model="user"
            :saving="savingRate"
            @save="handleSaveRate"
            @deleted="handleDeletedRate"
          />
          <RateCreateForm
            v-else
            :loading="savingRate"
            @create="handleCreateRate"
          />

          <v-divider class="my-5" />

          <v-card variant="outlined" class="pa-4">
            <div class="text-subtitle-2 font-weight-bold mb-2">Конфигурация ANet (client.toml)</div>
            <div class="text-caption text-medium-emphasis mb-3">
              Загрузка выполняется с авторизацией администратора; QR-код генерируется локально в браузере.
            </div>

            <div class="d-flex flex-wrap ga-2">
              <v-btn
                color="success"
                variant="flat"
                prepend-icon="mdi-download"
                :loading="configLoading"
                @click="handleDownloadConfig"
              >
                Скачать client.toml
              </v-btn>

              <v-btn
                color="primary"
                variant="tonal"
                prepend-icon="mdi-qrcode"
                :loading="configLoading"
                @click="handleToggleQr"
              >
                {{ showQrSection ? 'Скрыть QR-код' : 'Показать QR-код' }}
              </v-btn>
            </div>

            <div v-if="showQrSection && configText" class="mt-4 d-flex justify-center">
              <QrCodeBox
                :value="configText"
                :title="`ANet · ${user.uid}`"
                subtitle="Локальный QR-код конфигурации client.toml"
                :filename="`${user.uid.replace(/[^a-zA-Z0-9._-]/g, '_') || 'client'}.toml`"
              />
            </div>
          </v-card>
        </template>
      </v-card-text>

      <v-divider />

      <v-card-actions class="px-6 py-4 justify-space-between flex-shrink-0">
        <v-btn
          v-if="user"
          color="warning"
          variant="text"
          prepend-icon="mdi-key-change"
          :loading="regenerating"
          @click="handleRegenerate"
        >
          Перегенерировать ключи
        </v-btn>
        <div v-else />

        <div class="d-flex ga-2">
          <v-btn variant="text" @click="close">Закрыть</v-btn>
          <v-btn
            v-if="user"
            color="primary"
            variant="flat"
            :loading="savingUser"
            :disabled="!formValid"
            @click="handleSaveUser"
          >
            Сохранить
          </v-btn>
        </div>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
