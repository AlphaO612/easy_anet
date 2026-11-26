export type GraphNodeRole = 'entry' | 'exit' | 'both'
export type GraphNodeStatus = 'online' | 'offline' | 'draining' | 'unknown'
export type GraphEdgeStatus = 'up' | 'degraded' | 'down' | 'unknown'

export interface GraphNode {
  id: string
  name: string
  role: GraphNodeRole
  address: string
  is_active: boolean
  status: GraphNodeStatus
  heartbeat_age_sec?: number | null
  uptime_seconds?: number | null
  active_connections: number
  accepting_connections: boolean
  version?: string | null
  transports: string[]
  rx_bytes: number
  tx_bytes: number
  device_count: number
  probe_revision: number
  probe_applied: number
  probe_error?: string | null
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  transport: string
  route_id?: string | null
  status: GraphEdgeStatus
  latency_ms?: number | null
  loss_pct?: number | null
  throughput_mbps?: number | null
  rx_bytes: number
  tx_bytes: number
  last_measured_at?: string | null
}

export interface UserGraphPath {
  user_id: string
  uid: string
  is_active: boolean
  protocols: string[]
  node_ids: string[]
  edge_ids: string[]
  rx_bytes: number
  tx_bytes: number
}

export interface ClientsSummary {
  total_users: number
  active_users: number
  active_sessions: number
  by_protocol: {
    vless: number
    wireguard: number
    anet: number
  }
}

export interface TopologyGraphResponse {
  generated_at: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  clients_summary: ClientsSummary
  user_paths: UserGraphPath[]
}
