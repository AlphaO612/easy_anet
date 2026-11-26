import { api } from './client'

export interface TelegramExitOption {
  exit_host: string
  label: string
  region: string
  latency_ms?: number | null
  tunnel_ready: boolean
}

export interface TelegramAlertConfig {
  enabled: boolean
  has_bot_token: boolean
  chat_id: string
  cooldown_minutes: number
  exit_route: string
  proxy_url: string
  api_base_url: string
  relay_online: boolean
  available_exits: TelegramExitOption[]
  updated_at?: string | null
}

export interface SaveTelegramAlertRequest {
  enabled: boolean
  bot_token?: string | null
  chat_id: string
  cooldown_minutes?: number
  exit_route?: string
  proxy_url?: string
  api_base_url?: string
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

export interface SitePreview {
  domain: string
  mode: string
  preset: string
  title: string
  html: string
}

export type OperatingMode = 'balanced' | 'low_latency' | 'high_throughput' | 'failover_only'

export interface SystemSettings {
  operating_mode: OperatingMode
  multi_exit_enabled: boolean
  parallel_transports: boolean
  auto_failover: boolean
  metrics_enabled: boolean
  metrics_retention_days: number
  probe_interval_seconds: number
  sync_interval_seconds: number
  subscription_origin_override: string
  updated_at?: string | null
}

export interface PruneMetricsResponse {
  deleted_rows: number
  retention_days: number
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

export function GetSitePresetPreview(preset: string, title?: string): Promise<SitePreview> {
  const params = new URLSearchParams({ preset })
  if (title?.trim()) params.set('title', title.trim())
  return api<SitePreview>(`/sites/preview?${params.toString()}`, { method: 'GET' })
}

export function GetDomainSiteHtml(domain: string): Promise<SitePreview> {
  return api<SitePreview>(`/sites/${encodeURIComponent(domain)}/html`, { method: 'GET' })
}

export function GetSystemSettings(): Promise<SystemSettings> {
  return api<SystemSettings>('/settings', { method: 'GET' })
}

export function SaveSystemSettings(data: Omit<SystemSettings, 'updated_at'>): Promise<SystemSettings> {
  return api<SystemSettings>('/settings', { method: 'PUT', data })
}

export function PruneMetrics(days?: number): Promise<PruneMetricsResponse> {
  const q = days ? `?days=${encodeURIComponent(String(days))}` : ''
  return api<PruneMetricsResponse>(`/settings/prune-metrics${q}`, { method: 'POST' })
}
