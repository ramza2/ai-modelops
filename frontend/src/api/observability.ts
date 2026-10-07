import { apiGet } from './client'
import type {
  CapacityProfile,
  InvocationSummaryResponse,
  RuntimeHistoryResponse,
  RuntimeLatestResponse,
} from './types'

export type InvocationSummaryParams = {
  hours?: number
  groupBy?: 'client' | 'alias' | 'deployment'
  signal?: AbortSignal
}

export async function getInvocationSummary(
  params: InvocationSummaryParams = {},
): Promise<InvocationSummaryResponse> {
  return apiGet<InvocationSummaryResponse>(
    '/api/v1/observability/invocations/summary',
    {
      query: {
        hours: params.hours ?? 24,
        group_by: params.groupBy ?? 'client',
      },
      signal: params.signal,
    },
  )
}

export async function getRuntimeLatest(
  deploymentId?: string | null,
  signal?: AbortSignal,
): Promise<RuntimeLatestResponse> {
  return apiGet<RuntimeLatestResponse>('/api/v1/observability/runtime/latest', {
    query: deploymentId ? { deployment_id: deploymentId } : undefined,
    signal,
  })
}

export type RuntimeHistoryParams = {
  hours?: number
  limit?: number
  signal?: AbortSignal
}

export async function getRuntimeHistory(
  deploymentId: string,
  params: RuntimeHistoryParams = {},
): Promise<RuntimeHistoryResponse> {
  return apiGet<RuntimeHistoryResponse>(
    `/api/v1/observability/runtime/deployments/${deploymentId}/history`,
    {
      query: {
        hours: params.hours ?? 24,
        limit: params.limit ?? 500,
      },
      signal: params.signal,
    },
  )
}

export async function getCapacityProfile(
  deploymentId: string,
  hours = 24,
  signal?: AbortSignal,
): Promise<CapacityProfile> {
  return apiGet<CapacityProfile>(
    `/api/v1/observability/runtime/deployments/${deploymentId}/capacity-profile`,
    {
      query: { hours },
      signal,
    },
  )
}
