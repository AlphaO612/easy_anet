export type DeviceProtocol = 'vless' | 'wireguard' | 'anet'

export interface IngressRoute {
  id: string
  protocol: DeviceProtocol
  name: string
  entry_host: string
  entry_port: number
  exit_host: string
  entry_server_id?: string | null
  exit_server_id?: string | null
  wg_server_public_key?: string | null
  wg_subnet?: string | null
  sni?: string | null
  sync_mode: 'live' | 'legacy_pool'
  last_sync_at?: string | null
  created_at: string
}

export interface IngressRoutesResponse {
  origin: string
  routes: IngressRoute[]
}

export interface CreateIngressRouteRequest {
  id?: string
  protocol: DeviceProtocol
  name: string
  entry_host: string
  entry_port: number
  exit_host: string
  entry_server_id?: string | null
  exit_server_id?: string | null
  wg_server_public_key?: string | null
  wg_subnet?: string | null
  sni?: string | null
}

export interface CreateIngressRouteResponse {
  route: IngressRoute
  sync_token: string
}

export type DeviceTrafficState = 'live' | 'stale' | 'no_reports'

export interface UserDevice {
  id: string
  user_id: string
  protocol: DeviceProtocol
  name: string
  route_id?: string | null
  entry_host?: string | null
  entry_port?: number | null
  exit_host?: string | null
  status: 'active' | 'revoked'
  sync_mode?: string | null
  synced_at?: string | null
  config_uri?: string | null
  config_text?: string | null
  assigned_ip?: string | null
  upload_bytes: number
  download_bytes: number
  last_traffic_at?: string | null
  traffic_state: DeviceTrafficState
  created_at: string
}

export interface UserDevicesResponse {
  user_id: string
  subscription_active: boolean
  subscription_origin: string
  devices: UserDevice[]
}

export interface CreateDeviceRequest {
  protocol: DeviceProtocol
  name: string
  route_id?: string
}

export interface SubscriptionResponse {
  path: string
  url: string
  token: string
}
