import {
  type User,
  type UsersResponse,
  type UpdateUserRequest,
  type RegenerateUserRequest,
  type CreateUserRequest,
} from '@/models/user'
import { api } from './client'
import { toQuery } from '@/utils'

export async function GetUsers(
  from: number = 0,
  limit: number = 10,
  search?: string,
  groupIds?: string[],
  sortBy?: string,
  descending?: boolean,
): Promise<UsersResponse> {
  const params = toQuery({
    from,
    limit,
    search: search || undefined,
    group_ids: groupIds && groupIds.length ? groupIds.join(',') : undefined,
    sort_by: sortBy || undefined,
    descending: descending !== undefined ? String(descending) : undefined,
  })

  return api<UsersResponse>(`/users?${params}`, {
    method: 'GET',
  })
}

export async function GetUser(id: string): Promise<User> {
  return api<User>(`/user/${encodeURIComponent(id)}`, {
    method: 'GET',
  })
}

export async function UpdateUser(id: string, data: UpdateUserRequest) {
  return api<RegenerateUserRequest>(`/user/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    data,
  })
}

export async function DeleteUser(id: string): Promise<void> {
  return api<void>(`/user/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
}

export async function ResetUserTraffic(id: string): Promise<void> {
  return api<void>(`/users/${encodeURIComponent(id)}/reset-traffic`, {
    method: 'POST',
  })
}

export async function Regenerate(id: string): Promise<RegenerateUserRequest> {
  return api<RegenerateUserRequest>(`/regenerate/${encodeURIComponent(id)}`, {
    method: 'POST',
  })
}

export async function CreateUser(data: CreateUserRequest): Promise<Pick<User, 'id' | 'uid'>> {
  return api<Pick<User, 'id' | 'uid'>>(`/add`, {
    method: 'POST',
    data,
  })
}

// Authenticated download of client.toml (requires Admin Bearer JWT)
export async function GetUserConfig(id: string): Promise<string> {
  return api<string>(`/config/${encodeURIComponent(id)}`, {
    method: 'GET',
    responseType: 'text',
  })
}
