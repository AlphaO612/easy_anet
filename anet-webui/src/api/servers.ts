import { api } from './client'
import type { Server, CreateServerRequest, NodeCommand, NodeCommandStatus, NodeCredential } from '@/models/server'

export async function GetServers(): Promise<Server[]> {
  return api<Server[]>('/servers', {
    method: 'GET',
  })
}

export async function CreateServer(data: CreateServerRequest): Promise<Server> {
  return api<Server>('/servers', {
    method: 'POST',
    data,
  })
}

export async function UpdateServer(id: string, data: Partial<CreateServerRequest>): Promise<Server> {
  return api<Server>(`/servers/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    data,
  })
}

export async function DeleteServer(id: string): Promise<void> {
  return api<void>(`/servers/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
}

export async function ResetServerState(id: string): Promise<void> {
  return api<void>(`/servers/${encodeURIComponent(id)}/reset-state`, {
    method: 'POST',
  })
}

export async function SetNodeAdmission(id: string, acceptingConnections: boolean): Promise<NodeCommand> {
  return api<NodeCommand>(`/servers/${encodeURIComponent(id)}/commands/admission`, {
    method: 'POST',
    data: { accepting_connections: acceptingConnections },
  })
}

export async function GetNodeCommandStatus(serverId: string, commandId: string): Promise<NodeCommandStatus> {
  return api<NodeCommandStatus>(`/servers/${encodeURIComponent(serverId)}/commands/${encodeURIComponent(commandId)}`, {
    method: 'GET',
  })
}

export async function RotateNodeCredential(serverId: string): Promise<NodeCredential> {
  return api<NodeCredential>(`/servers/${encodeURIComponent(serverId)}/credentials`, {
    method: 'POST',
  })
}

export function IssueNodeInvitation(serverId: string): Promise<NodeCredential> {
  return api<NodeCredential>(`/servers/${encodeURIComponent(serverId)}/invitations`, { method: 'POST' })
}
