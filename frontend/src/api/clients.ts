import { apiGet, apiPatch, apiPost } from './client'
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

export type CreateClientBody = {
  client_key: string
  display_name: string
  description?: string | null
}

/** PATCH body: omit unchanged fields; use ``null`` to clear description. */
export type UpdateClientBody = {
  display_name?: string
  description?: string | null
  is_active?: boolean
}

export type ClientMutationOptions = {
  /** Caller-managed key — reuse only for the same logical payload retry. */
  idempotencyKey: string
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

export async function createClient(
  body: CreateClientBody,
  opts: ClientMutationOptions,
): Promise<ClientApp> {
  const payload: Record<string, unknown> = {
    client_key: body.client_key,
    display_name: body.display_name,
  }
  if (body.description !== undefined) {
    payload.description = body.description
  }
  return apiPost<ClientApp>('/api/v1/clients', {
    body: payload,
    signal: opts.signal,
    headers: { 'Idempotency-Key': opts.idempotencyKey },
  })
}

export async function updateClient(
  clientId: string,
  body: UpdateClientBody,
  opts: ClientMutationOptions,
): Promise<ClientApp> {
  const payload: Record<string, unknown> = {}
  if (body.display_name !== undefined) {
    payload.display_name = body.display_name
  }
  if ('description' in body) {
    payload.description = body.description ?? null
  }
  if (body.is_active !== undefined) {
    payload.is_active = body.is_active
  }
  return apiPatch<ClientApp>(`/api/v1/clients/${clientId}`, {
    body: payload,
    signal: opts.signal,
    headers: { 'Idempotency-Key': opts.idempotencyKey },
  })
}
