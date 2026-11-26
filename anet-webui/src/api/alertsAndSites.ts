import { api } from './client'

export interface TelegramAlertConfig {
  enabled: boolean
  has_bot_token: boolean
  chat_id: string
  cooldown_minutes: number
  updated_at?: string | null
}

export interface SaveTelegramAlertRequest {
  enabled: boolean
  bot_token?: string | null
  chat_id: string
  cooldown_minutes?: number
}

export interface TelegramTestResponse {
  ok: boolean
  message: string
}

export interface SitePreset {
  id: string
  name: string
  description: string
}

export interface SiteBundle {
  domain: string
  mode: 'preset' | 'custom_html' | 'custom_zip'
  preset: string
  title: string
  bundle_sha256: string
  size_bytes: number
  updated_at?: string | null
  configured: boolean
}

export interface SitesListResponse {
  presets: SitePreset[]
  sites: SiteBundle[]
}

export interface SaveSiteBundleRequest {
  mode: 'preset' | 'custom_html' | 'custom_zip'
  preset?: string
  title?: string
  html?: string
  zip_base64?: string
}

export function GetTelegramAlertConfig(): Promise<TelegramAlertConfig> {
  return api<TelegramAlertConfig>('/alerts/telegram', { method: 'GET' })
}

export function SaveTelegramAlertConfig(data: SaveTelegramAlertRequest): Promise<TelegramAlertConfig> {
  return api<TelegramAlertConfig>('/alerts/telegram', { method: 'PUT', data })
}

export function TestTelegramAlert(): Promise<TelegramTestResponse> {
  return api<TelegramTestResponse>('/alerts/telegram/test', { method: 'POST' })
}

export function GetSites(): Promise<SitesListResponse> {
  return api<SitesListResponse>('/sites', { method: 'GET' })
}

export function SaveSiteBundle(domain: string, data: SaveSiteBundleRequest): Promise<SiteBundle> {
  return api<SiteBundle>(`/sites/${encodeURIComponent(domain)}`, { method: 'PUT', data })
}

export function DeleteSiteBundle(domain: string): Promise<void> {
  return api<void>(`/sites/${encodeURIComponent(domain)}`, { method: 'DELETE' })
}
