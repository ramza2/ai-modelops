import { apiGet, apiPost } from './client'
import type {
  Endpoint,
  EndpointRoute,
  Paginated,
  PreflightPreview,
  SwitchOperation,
} from './types'

export type ListEndpointsParams = {
  q?: string | null
  apiType?: string | null
  isEnabled?: boolean | null
  trafficState?: string | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listEndpoints(
  params: ListEndpointsParams = {},
): Promise<Paginated<Endpoint>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  if (params.q) query.q = params.q
  if (params.apiType) query.api_type = params.apiType
  if (params.isEnabled === true || params.isEnabled === false) {
    query.is_enabled = params.isEnabled
  }
  if (params.trafficState) query.traffic_state = params.trafficState
  return apiGet<Paginated<Endpoint>>('/api/v1/endpoints', {
    query,
    signal: params.signal,
  })
}

export async function getEndpoint(
  endpointId: string,
  signal?: AbortSignal,
): Promise<Endpoint> {
  return apiGet<Endpoint>(`/api/v1/endpoints/${endpointId}`, { signal })
}

export async function listEndpointRoutes(
  endpointId: string,
  signal?: AbortSignal,
): Promise<{ items: EndpointRoute[]; total: number }> {
  return apiGet<{ items: EndpointRoute[]; total: number }>(
    `/api/v1/endpoints/${endpointId}/routes`,
    { signal },
  )
}

export async function createPreflightPreview(
  endpointId: string,
  targetDeploymentId: string,
  signal?: AbortSignal,
): Promise<PreflightPreview> {
  return apiPost<PreflightPreview>('/api/v1/preflights', {
    signal,
    body: {
      endpoint_id: endpointId,
      target_deployment_id: targetDeploymentId,
    },
  })
}

export async function switchEndpoint(
  endpointId: string,
  body: {
    targetDeploymentId: string
    strategy: 'HOT' | 'COLD'
    reason?: string | null
  },
  signal?: AbortSignal,
): Promise<SwitchOperation> {
  return apiPost<SwitchOperation>(`/api/v1/endpoints/${endpointId}/switch`, {
    signal,
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    body: {
      target_deployment_id: body.targetDeploymentId,
      strategy: body.strategy,
      reason: body.reason?.trim() || null,
    },
  })
}
