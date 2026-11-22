import { api } from './client'
import type { Server } from '@/models/server'

export type ProbeEdge = { target_node: string; module: 'tcp_connect' | 'https_ready' | 'https_capacity'; target: string }
export type ProbeRevision = { revision: number; edges: ProbeEdge[] }
export type ProbeStatus = { desired: number; applied: number; error?: string | null; manifest?: ProbeRevision | null }

export function probeStatus(nodeId: string) {
  return api<ProbeStatus>(`/servers/${encodeURIComponent(nodeId)}/probe-revision`)
}

export async function exportNodeInventory(nodes: Server[]) {
  const hosts: Record<string, unknown> = {}
  for (const node of [...nodes].sort((a, b) => a.id.localeCompare(b.id))) {
    const status = await probeStatus(node.id)
    hosts[node.id] = {
      ansible_host: node.address, anet_host: node.address, anet_node_id: node.id,
      anet_probe_revision: status.desired, anet_probe_edges: status.manifest?.edges ?? [],
    }
  }
  const inventory = { all: { children: { anet: { hosts } } } }
  const url = URL.createObjectURL(new Blob([JSON.stringify(inventory, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.download = 'anet-inventory.yml'
  link.href = url
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
