import { api } from './client'
import type {
  CreateDeviceRequest,
  CreateIngressRouteRequest,
  CreateIngressRouteResponse,
  IngressRoutesResponse,
  QuickProvisionRequest,
  QuickProvisionResponse,
  SubscriptionResponse,
  UserDevice,
  UserDevicesResponse,
} from '@/models/device'

export function GetIngressRoutes(): Promise<IngressRoutesResponse> {
  return api<IngressRoutesResponse>('/ingress-routes', { method: 'GET' })
}

export function CreateIngressRoute(data: CreateIngressRouteRequest): Promise<CreateIngressRouteResponse> {
  return api<CreateIngressRouteResponse>('/ingress-routes', { method: 'POST', data })
}

export function DeleteIngressRoute(id: string): Promise<void> {
  return api<void>(`/ingress-routes/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function GetUserDevices(userId: string): Promise<UserDevicesResponse> {
  return api<UserDevicesResponse>(`/users/${encodeURIComponent(userId)}/devices`, { method: 'GET' })
}

export function CreateUserDevice(userId: string, data: CreateDeviceRequest): Promise<UserDevice> {
  return api<UserDevice>(`/users/${encodeURIComponent(userId)}/devices`, { method: 'POST', data })
}

export function RevokeUserDevice(userId: string, deviceId: string): Promise<void> {
  return api<void>(`/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/revoke`, {
    method: 'POST',
  })
}

export function DeleteUserDevice(userId: string, deviceId: string): Promise<void> {
  return api<void>(`/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
  })
}

export function CreateUserSubscription(userId: string): Promise<SubscriptionResponse> {
  return api<SubscriptionResponse>(`/users/${encodeURIComponent(userId)}/subscription`, {
    method: 'POST',
  })
}

export function RevokeUserSubscription(userId: string): Promise<void> {
  return api<void>(`/users/${encodeURIComponent(userId)}/subscription`, {
    method: 'DELETE',
  })
}

export function QuickProvisionUser(
  userId: string,
  data: QuickProvisionRequest = { include_vless: true, include_wireguard: true, include_anet: false },
): Promise<QuickProvisionResponse> {
  return api<QuickProvisionResponse>(`/users/${encodeURIComponent(userId)}/quick-provision`, {
    method: 'POST',
    data,
  })
}
