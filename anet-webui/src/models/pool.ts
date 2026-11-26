export type ProtocolType = 'quic' | 'ssh' | 'vnc' | 'ws' | 'ahttp' | 'wrtc' | 'imap'

export type PoolStrategy = 'weighted' | 'least_connections' | 'latency'

export type NodePoolMember = {
  server_id: string
  protocol: ProtocolType
  port_or_url?: string | null
  wrtc_mode?: string | null
  weight: number
}

export type NodePool = {
  id: string
  name: string
  strategy: PoolStrategy
  is_active: boolean
  members: NodePoolMember[]
}

export type SaveNodePoolRequest = Omit<NodePool, 'id'>

export type PoolPreviewCandidate = {
  rank: number
  server_id: string
  server_name: string
  address: string
  protocol: string
  port_or_url?: string | null
  weight: number
  status: string
  accepting_connections: boolean
  active_connections: number
  latency_ms?: number | null
  loss_pct?: number | null
  throughput_mbps?: number | null
  health_tier: string
  reason: string
}

export type PoolPreviewResponse = {
  pool_id: string
  pool_name: string
  strategy: string
  candidates: PoolPreviewCandidate[]
}
