import { api } from './client'
import type { NodePool, PoolPreviewResponse, SaveNodePoolRequest } from '@/models/pool'

export const GetPools = () => api<NodePool[]>('/pools')
export const CreatePool = (pool: SaveNodePoolRequest) =>
  api<NodePool>('/pools', {
    method: 'POST',
    data: pool,
  })
export const UpdatePool = (id: string, pool: SaveNodePoolRequest) =>
  api<NodePool>(`/pools/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    data: pool,
  })
export const DeletePool = (id: string) =>
  api<void>(`/pools/${encodeURIComponent(id)}`, { method: 'DELETE' })

export const GetPoolPreview = (id: string, userId?: string) =>
  api<PoolPreviewResponse>(`/pools/${encodeURIComponent(id)}/preview`, {
    method: 'GET',
    params: userId ? { user_id: userId } : undefined,
  })
