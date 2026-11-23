<script setup lang="ts">
export type Traffic = { upload: number; download: number; updated: number }
defineProps<{ traffic?: Traffic | null }>()
function bytes(n: number) {
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ', 'ПиБ']
  let unit = 0
  while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++ }
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + ' ' + units[unit]
}
</script>

<template>
  <p v-if="traffic" class="text-body-2 my-1">
    Отправлено {{ bytes(traffic.upload) }} · Получено {{ bytes(traffic.download) }} ·
    Обновлено {{ new Date(traffic.updated * 1000).toLocaleString() }}
  </p>
  <p v-else class="text-body-2 my-1">Трафик · ждём первый отчёт с VLESS-входа</p>
</template>
