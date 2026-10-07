import { apiGet } from './client'
import type {
  ClientApp,
  ClientRuntimePolicyResponse,
  Paginated,
} from './types'

export type ListClientsParams = {
  q?: string | null
  isActive?: boolean | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listClients(
  params: ListClientsParams = {},
): Promise<Paginated<ClientApp>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  const q = params.q?.trim()
  if (q) query.q = q
  if (params.isActive === true || params.isActive === false) {
    query.is_active = params.isActive
  }
  return apiGet<Paginated<ClientApp>>('/api/v1/clients', {
    query,
    signal: params.signal,
  })
}

export async function getClient(
  clientId: string,
  signal?: AbortSignal,
): Promise<ClientApp> {
  return apiGet<ClientApp>(`/api/v1/clients/${clientId}`, { signal })
}

export async function getClientRuntimePolicy(
  clientId: string,
  signal?: AbortSignal,
): Promise<ClientRuntimePolicyResponse> {
  return apiGet<ClientRuntimePolicyResponse>(
    `/api/v1/clients/${clientId}/runtime-policy`,
    { signal },
  )
}
