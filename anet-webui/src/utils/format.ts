import { isAxiosError } from 'axios'

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes <= 0) return '0 Б'
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ', 'ПиБ']
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** unit
  return `${value.toLocaleString('ru-RU', { maximumFractionDigits: unit === 0 ? 0 : 2 })} ${units[unit]}`
}

export function formatUptime(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || seconds < 0) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}д ${hours}ч`
  if (hours > 0) return `${hours}ч ${minutes}м`
  return `${minutes}м`
}

export function formatHeartbeatAge(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || seconds < 0) return 'нет отчёта'
  if (seconds < 60) return `${Math.round(seconds)}с назад`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}м назад`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}ч назад`
  return `${Math.floor(seconds / 86400)}д назад`
}

export function formatDateTime(value?: string | number | null): string {
  if (value === undefined || value === null || value === '') return '—'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('ru-RU')
}

// Convert backend date strings (`YYYY-MM-DD-HH:MM`, ISO8601, `YYYY-MM-DDTHH:MM`) into HTML datetime-local `YYYY-MM-DDTHH:mm`
export function toDateTimeLocal(value?: string | null): string {
  if (!value) {
    const now = new Date()
    now.setDate(now.getDate() + 30)
    return formatLocalInput(now)
  }
  const dashMatch = value.match(/^(\d{4}-\d{2}-\d{2})-(\d{2}:\d{2})/)
  if (dashMatch) return `${dashMatch[1]}T${dashMatch[2]}`

  const isoLocalMatch = value.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
  if (isoLocalMatch && !value.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(value)) {
    return `${isoLocalMatch[1]}T${isoLocalMatch[2]}`
  }

  const parsed = new Date(value)
  if (!Number.isNaN(parsed.getTime())) {
    return formatLocalInput(parsed)
  }
  return value
}

function formatLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function extractApiError(error: unknown, fallback = 'Произошла ошибка при выполнении запроса'): string {
  if (isAxiosError(error)) {
    const data = error.response?.data
    if (typeof data === 'string' && data.trim()) return data.trim()
    if (data && typeof data === 'object') {
      const record = data as Record<string, unknown>
      if (typeof record.error === 'string' && record.error.trim()) return record.error.trim()
      if (typeof record.message === 'string' && record.message.trim()) return record.message.trim()
      if (typeof record.detail === 'string' && record.detail.trim()) return record.detail.trim()
    }
    if (error.message) return error.message
  }
  if (error instanceof Error && error.message) return error.message
  return fallback
}

export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // fall through to textarea fallback
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  area.style.position = 'fixed'
  area.style.left = '-9999px'
  area.style.top = '-9999px'
  document.body.appendChild(area)
  area.focus()
  area.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}

export function downloadTextFile(filename: string, content: string, mime = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type: mime }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
