import { apiGet, apiPost } from './client'
import type {
  OperationDetail,
  OperationSummary,
  Paginated,
  RetryOperationResponse,
} from './types'

export type ListOperationsParams = {
  status?: string | null
  operationType?: string | null
  active?: boolean | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listOperations(
  params: ListOperationsParams = {},
): Promise<Paginated<OperationSummary>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  if (params.status) query.status = params.status
  if (params.operationType) query.operation_type = params.operationType
  if (params.active === true || params.active === false) {
    query.active = params.active
  }
  return apiGet<Paginated<OperationSummary>>('/api/v1/operations', {
    query,
    signal: params.signal,
  })
}

export async function getOperation(
  operationId: string,
  signal?: AbortSignal,
): Promise<OperationDetail> {
  return apiGet<OperationDetail>(`/api/v1/operations/${operationId}`, {
    signal,
  })
}

export async function cancelOperation(
  operationId: string,
  reason?: string | null,
  signal?: AbortSignal,
): Promise<OperationDetail> {
  const trimmed = reason?.trim()
  return apiPost<OperationDetail>(`/api/v1/operations/${operationId}/cancel`, {
    signal,
    body: trimmed ? { reason: trimmed } : {},
  })
}

export async function retryOperation(
  operationId: string,
  signal?: AbortSignal,
): Promise<RetryOperationResponse> {
  return apiPost<RetryOperationResponse>(
    `/api/v1/operations/${operationId}/retry`,
    {
      signal,
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    },
  )
}
