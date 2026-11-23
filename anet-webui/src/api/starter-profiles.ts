import { isAxiosError } from 'axios'
import { api } from './client'

export interface StarterProfile {
  id: string
  entry: string
  exit: string
  exit_public_key?: string
  protocol: 'wireguard' | 'anet' | 'vless'
  port: number
}

export async function getStarterProfiles() {
  try {
    return await api<StarterProfile[]>('/starter-profiles/index.json')
  } catch (error) {
    if (isAxiosError(error) && error.response?.status === 404) return []
    throw error
  }
}
