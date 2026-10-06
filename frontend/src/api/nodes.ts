import { apiGet, apiPost } from './client'
import type {
  NodeDetail,
  NodeResourcesLatest,
  NodeSummary,
  Paginated,
} from './types'

export type ListNodesParams = {
  status?: string | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listNodes(
  params: ListNodesParams = {},
): Promise<Paginated<NodeSummary>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  if (params.status) {
    query.status = params.status
  }
  return apiGet<Paginated<NodeSummary>>('/api/v1/nodes', {
    query,
    signal: params.signal,
  })
}

export async function getNode(
  nodeId: string,
  signal?: AbortSignal,
): Promise<NodeDetail> {
  return apiGet<NodeDetail>(`/api/v1/nodes/${nodeId}`, { signal })
}

export async function getNodeResources(
  nodeId: string,
  signal?: AbortSignal,
): Promise<NodeResourcesLatest> {
  return apiGet<NodeResourcesLatest>(
    `/api/v1/nodes/${nodeId}/resources/latest`,
    { signal },
  )
}

export async function refreshNodeResources(
  nodeId: string,
  signal?: AbortSignal,
): Promise<NodeResourcesLatest> {
  return apiPost<NodeResourcesLatest>(
    `/api/v1/nodes/${nodeId}/resources/refresh`,
    { signal },
  )
}
