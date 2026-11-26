import { api } from './client'
import type { TopologyGraphResponse } from '@/models/topology'

export function GetTopologyGraph(): Promise<TopologyGraphResponse> {
  return api<TopologyGraphResponse>('/topology/graph', { method: 'GET' })
}
