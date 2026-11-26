<script setup lang="ts">
import { computed } from 'vue'
import { encodeQrMatrix, type QrEccLevel } from '@/utils/qrcode'
import { copyText, downloadTextFile } from '@/utils/format'
import { useAppMessage } from '@/composables/useAppMessage'

const props = withDefaults(
  defineProps<{
    value: string
    title?: string
    subtitle?: string
    filename?: string
    size?: number
    ecc?: QrEccLevel
  }>(),
  {
    title: '',
    subtitle: '',
    filename: 'config.txt',
    size: 224,
    ecc: 'M',
  },
)

const message = useAppMessage()

const quietZone = 4

const qrData = computed(() => {
  const raw = props.value?.trim() || ''
  if (!raw) {
    return { ok: false as const, error: 'Нет данных для генерации QR-кода' }
  }
  try {
    const matrix = encodeQrMatrix(raw, props.ecc)
    const dim = matrix.size + quietZone * 2
    const parts: string[] = []
    for (let r = 0; r < matrix.size; r++) {
      for (let c = 0; c < matrix.size; c++) {
        if (matrix.modules[r * matrix.size + c]) {
          parts.push(`M${c + quietZone},${r + quietZone}h1v1h-1z`)
        }
      }
    }
    return {
      ok: true as const,
      dim,
      path: parts.join(''),
      version: matrix.version,
      ecc: matrix.ecc,
    }
  } catch (e) {
    return {
      ok: false as const,
      error: e instanceof Error ? e.message : 'Слишком длинный текст для QR-кода',
    }
  }
})

const handleCopy = async () => {
  if (!props.value) return
  const ok = await copyText(props.value)
  if (ok) {
    message.success('Скопировано в буфер обмена')
  } else {
    message.error('Не удалось скопировать текст')
  }
}

const handleDownload = () => {
  if (!props.value) return
  downloadTextFile(props.filename || 'config.txt', props.value)
  message.success(`Файл ${props.filename || 'config.txt'} скачан`)
}
</script>

<template>
  <div class="qr-box d-flex flex-column align-center text-center">
    <div v-if="title" class="text-subtitle-1 font-weight-bold mb-1">{{ title }}</div>
    <div v-if="subtitle" class="text-caption text-medium-emphasis mb-3">{{ subtitle }}</div>

    <div
      v-if="qrData.ok"
      class="qr-frame rounded-lg pa-2 bg-white elevation-2 mb-3"
      :style="{ width: `${size + 16}px`, height: `${size + 16}px` }"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        :viewBox="`0 0 ${qrData.dim} ${qrData.dim}`"
        :width="size"
        :height="size"
        shape-rendering="crispEdges"
        role="img"
        :aria-label="title || 'QR Code'"
        class="d-block"
      >
        <rect :width="qrData.dim" :height="qrData.dim" fill="#FFFFFF" />
        <path :d="qrData.path" fill="#0F1211" />
      </svg>
    </div>

    <v-alert
      v-else
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-3 text-left"
      max-width="340"
    >
      {{ qrData.error }}. Используйте копирование или скачивание файла ниже.
    </v-alert>

    <div class="d-flex flex-wrap justify-center ga-2">
      <v-btn
        color="primary"
        variant="tonal"
        size="small"
        prepend-icon="mdi-content-copy"
        :disabled="!value"
        @click="handleCopy"
      >
        Копировать
      </v-btn>
      <v-btn
        variant="outlined"
        size="small"
        prepend-icon="mdi-download"
        :disabled="!value"
        @click="handleDownload"
      >
        Скачать {{ filename }}
      </v-btn>
    </div>
  </div>
</template>

<style scoped>
.qr-frame {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(43, 184, 148, 0.35);
}
</style>
