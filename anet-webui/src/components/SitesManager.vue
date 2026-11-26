<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import {
  DeleteSiteBundle,
  GetSites,
  SaveSiteBundle,
  type SiteBundle,
  type SitePreset,
} from '@/api/alertsAndSites'
import { useAppMessage } from '@/composables/useAppMessage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { extractApiError, formatBytes, formatDateTime } from '@/utils/format'

const appMessage = useAppMessage()
const { confirmAction } = useConfirmDialog()

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const presets = ref<SitePreset[]>([])
const sites = ref<SiteBundle[]>([])

const dialogOpen = ref(false)
const form = reactive({
  domain: '',
  mode: 'preset' as 'preset' | 'custom_html' | 'custom_zip',
  preset: 'studio',
  title: '',
  html: '',
  zipBase64: '',
  zipFilename: '',
  zipSize: 0,
})

const modeLabel = (mode: SiteBundle['mode']) => {
  if (mode === 'custom_zip') return 'ZIP-бандл'
  if (mode === 'custom_html') return 'Свой HTML'
  return 'Пресет'
}

const modeColor = (mode: SiteBundle['mode']) => {
  if (mode === 'custom_zip') return 'secondary'
  if (mode === 'custom_html') return 'info'
  return 'primary'
}

const loadSites = async () => {
  loading.value = true
  error.value = ''
  try {
    const res = await GetSites()
    presets.value = res.presets
    sites.value = res.sites
  } catch (err) {
    error.value = extractApiError(err, 'Не удалось загрузить список сайтов маскировки')
  } finally {
    loading.value = false
  }
}

const openEditor = (site?: SiteBundle) => {
  form.domain = site?.domain ?? ''
  form.mode = site?.mode ?? 'preset'
  form.preset = site?.preset || 'studio'
  form.title = site?.title || ''
  form.html = ''
  form.zipBase64 = ''
  form.zipFilename = ''
  form.zipSize = 0
  dialogOpen.value = true
}

const onHtmlFileChange = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 1024 * 1024) {
    appMessage.error('HTML-файл превышает лимит 1 МБ')
    return
  }
  form.html = await file.text()
  if (!form.title) form.title = file.name
}

const onZipFileChange = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 4 * 1024 * 1024) {
    appMessage.error('ZIP-бандл превышает лимит 4 МБ')
    return
  }
  const buf = await file.arrayBuffer()
  const bytes = new Uint8Array(buf)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  form.zipBase64 = btoa(binary)
  form.zipFilename = file.name
  form.zipSize = file.size
  if (!form.title) form.title = file.name
}

const submit = async () => {
  const dom = form.domain.trim().toLowerCase()
  if (!dom) {
    appMessage.warning('Укажите домен шлюза (или * по умолчанию)')
    return
  }
  if (form.mode === 'custom_html' && !form.html.trim()) {
    appMessage.warning('Вставьте HTML или выберите .html файл')
    return
  }
  if (form.mode === 'custom_zip' && !form.zipBase64) {
    appMessage.warning('Выберите .zip архив с index.html в корне')
    return
  }
  saving.value = true
  try {
    await SaveSiteBundle(dom, {
      mode: form.mode,
      preset: form.preset,
      title: form.title.trim(),
      html: form.mode === 'custom_html' ? form.html : undefined,
      zip_base64: form.mode === 'custom_zip' ? form.zipBase64 : undefined,
    })
    appMessage.success(`Сайт маскировки для «${dom}» сохранён — шлюз применит его в течение 30 секунд`)
    dialogOpen.value = false
    await loadSites()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось сохранить сайт маскировки'))
  } finally {
    saving.value = false
  }
}

const resetDomain = async (site: SiteBundle) => {
  const ok = await confirmAction({
    title: 'Сбросить сайт маскировки?',
    message: `Удалить кастомный бандл для «${site.domain}» и вернуть стандартный шаблон?`,
    confirmText: 'Сбросить',
    confirmColor: 'warning',
  })
  if (!ok) return
  try {
    await DeleteSiteBundle(site.domain)
    appMessage.success(`Настройки сайта для «${site.domain}» сброшены`)
    await loadSites()
  } catch (err) {
    appMessage.error(extractApiError(err, 'Не удалось сбросить сайт'))
  }
}

onMounted(loadSites)
</script>

<template>
  <div>
    <div class="d-flex justify-space-between align-center flex-wrap ga-2 mb-4">
      <div class="text-body-2 text-medium-emphasis">
        Управляйте HTTPS-страницами маскировки на порту 443 шлюзов: выбирайте встроенный шаблон или загружайте свой HTML / ZIP-бандл (с автоматической доставкой на шлюз без перезапуска VLESS).
      </div>
      <div class="d-flex ga-2">
        <v-btn variant="outlined" prepend-icon="mdi-refresh" :loading="loading" @click="loadSites">
          Обновить
        </v-btn>
        <v-btn color="primary" prepend-icon="mdi-web-plus" @click="openEditor()">
          Настроить домен
        </v-btn>
      </div>
    </div>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>

    <v-card variant="outlined" class="rounded-lg">
      <v-table>
        <thead>
          <tr>
            <th>Домен шлюза</th>
            <th>Режим</th>
            <th>Шаблон / Название</th>
            <th>Размер / Хэш</th>
            <th>Обновлён</th>
            <th class="text-center">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!loading && !sites.length">
            <td colspan="6" class="text-center py-8 text-medium-emphasis">
              Домены VLESS ещё не обнаружены. Вы можете добавить домен вручную кнопкой «Настроить домен».
            </td>
          </tr>
          <tr v-for="site in sites" :key="site.domain">
            <td>
              <div class="d-flex align-center ga-2">
                <code class="font-weight-bold text-primary">{{ site.domain }}</code>
                <v-chip v-if="site.domain === '*'" size="x-small" variant="tonal">По умолчанию</v-chip>
              </div>
            </td>
            <td>
              <v-chip size="small" variant="tonal" :color="modeColor(site.mode)">
                {{ modeLabel(site.mode) }}
              </v-chip>
            </td>
            <td>
              <div class="font-weight-medium">{{ site.title || site.preset }}</div>
              <div v-if="site.mode === 'preset'" class="text-caption text-medium-emphasis">
                пресет: {{ site.preset }}
              </div>
            </td>
            <td>
              <div v-if="site.configured" class="text-caption">
                {{ formatBytes(site.size_bytes) }}
                <span class="text-medium-emphasis ms-1">({{ site.bundle_sha256.slice(0, 10) }}…)</span>
              </div>
              <span v-else class="text-caption text-medium-emphasis">Стандартный (установщик)</span>
            </td>
            <td>
              <span class="text-caption text-medium-emphasis">
                {{ site.updated_at ? formatDateTime(site.updated_at) : '—' }}
              </span>
            </td>
            <td class="text-center">
              <div class="d-flex justify-center ga-1">
                <v-btn
                  size="small"
                  variant="tonal"
                  color="primary"
                  prepend-icon="mdi-pencil"
                  @click="openEditor(site)"
                >
                  Изменить
                </v-btn>
                <v-btn
                  v-if="site.configured"
                  icon="mdi-delete-outline"
                  size="small"
                  variant="text"
                  color="error"
                  title="Сбросить к стандартному"
                  @click="resetDomain(site)"
                />
              </div>
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>

    <v-dialog v-model="dialogOpen" max-width="720">
      <v-card>
        <v-card-title class="text-h6">Сайт маскировки HTTPS</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="form.domain"
            label="Домен шлюза (например gateway1.altnight.art или * для всех)"
            variant="outlined"
            density="comfortable"
            class="mb-3"
          />

          <v-btn-toggle
            v-model="form.mode"
            mandatory
            color="primary"
            variant="outlined"
            class="mb-4"
          >
            <v-btn value="preset" prepend-icon="mdi-palette-outline">Предустановленный</v-btn>
            <v-btn value="custom_html" prepend-icon="mdi-language-html5">Свой HTML</v-btn>
            <v-btn value="custom_zip" prepend-icon="mdi-folder-zip-outline">ZIP-бандл</v-btn>
          </v-btn-toggle>

          <template v-if="form.mode === 'preset'">
            <v-row dense class="mb-3">
              <v-col v-for="p in presets" :key="p.id" cols="12" sm="6">
                <v-card
                  :variant="form.preset === p.id ? 'tonal' : 'outlined'"
                  :color="form.preset === p.id ? 'primary' : undefined"
                  class="pa-3 cursor-pointer"
                  @click="form.preset = p.id"
                >
                  <div class="font-weight-bold text-body-2">{{ p.name }}</div>
                  <div class="text-caption text-medium-emphasis mt-1">{{ p.description }}</div>
                </v-card>
              </v-col>
            </v-row>
            <v-text-field
              v-model="form.title"
              label="Заголовок сайта (опционально)"
              placeholder="Оставьте пустым для стандартного названия"
              variant="outlined"
              density="comfortable"
            />
          </template>

          <template v-else-if="form.mode === 'custom_html'">
            <div class="mb-3">
              <label class="text-caption text-medium-emphasis d-block mb-1">
                Загрузить .html файл (до 1 МБ) или вставить код ниже:
              </label>
              <input type="file" accept=".html,.htm,text/html" @change="onHtmlFileChange" />
            </div>
            <v-text-field
              v-model="form.title"
              label="Метка/название бандла"
              variant="outlined"
              density="compact"
              class="mb-2"
            />
            <v-textarea
              v-model="form.html"
              label="Содержимое index.html"
              rows="8"
              variant="outlined"
              placeholder="<!doctype html><html>...</html>"
            />
          </template>

          <template v-else>
            <v-alert type="info" variant="tonal" density="compact" class="mb-3">
              Загрузите статический <strong>.zip</strong>-архив (до 4 МБ) с файлом <code>index.html</code> в корне.
              Шлюз безопасно распакует его на порт 443, сохранив <code>.well-known</code> для Let's Encrypt.
            </v-alert>
            <div class="mb-3">
              <input type="file" accept=".zip,application/zip" @change="onZipFileChange" />
            </div>
            <div v-if="form.zipFilename" class="text-body-2 text-success mb-2">
              Выбран файл: <strong>{{ form.zipFilename }}</strong> ({{ formatBytes(form.zipSize) }})
            </div>
            <v-text-field
              v-model="form.title"
              label="Метка/название бандла"
              variant="outlined"
              density="compact"
            />
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="saving" @click="dialogOpen = false">Отмена</v-btn>
          <v-btn color="primary" :loading="saving" @click="submit">Применить</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
