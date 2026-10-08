import { apiDelete, apiGet, apiPost } from './client'
import type { HfDownloadJob, ModelCacheEntry, ModelCachePurgeResult, Paginated } from './types'

export const ACTIVE_DOWNLOAD_STATUSES: ReadonlySet<string> = new Set([
  'QUEUED',
  'RESOLVING',
  'DOWNLOADING',
  'MATERIALIZING',
  'VERIFYING',
])

export function isActiveDownloadStatus(status: string | null | undefined): boolean {
  if (!status) return false
  return ACTIVE_DOWNLOAD_STATUSES.has(status.toUpperCase())
}

export type StartHfDownloadBody = {
  repositoryId: string
  revision?: string | null
  nodeId: string
  modelType?: string | null
}

export async function startHfDownload(
  body: StartHfDownloadBody,
  signal?: AbortSignal,
): Promise<HfDownloadJob> {
  return apiPost<HfDownloadJob>('/api/v1/catalog/huggingface/downloads', {
    body: {
      repository_id: body.repositoryId,
      revision: body.revision ?? null,
      node_id: body.nodeId,
      model_type: body.modelType ?? null,
    },
    signal,
  })
}

export async function getHfDownload(
  jobId: string,
  signal?: AbortSignal,
): Promise<HfDownloadJob> {
  return apiGet<HfDownloadJob>(`/api/v1/catalog/huggingface/downloads/${jobId}`, {
    signal,
  })
}

export type ListModelCachesParams = {
  nodeId?: string | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listModelCaches(
  params: ListModelCachesParams = {},
): Promise<Paginated<ModelCacheEntry>> {
  return apiGet<Paginated<ModelCacheEntry>>('/api/v1/model-cache', {
    query: {
      node_id: params.nodeId || undefined,
      page: params.page ?? 1,
      page_size: params.pageSize ?? 20,
    },
    signal: params.signal,
  })
}

export type PurgeModelCacheOptions = {
  force?: boolean
  signal?: AbortSignal
}

export async function purgeModelCache(
  cacheId: string,
  options: PurgeModelCacheOptions = {},
): Promise<ModelCachePurgeResult> {
  return apiDelete<ModelCachePurgeResult>(`/api/v1/model-cache/${cacheId}`, {
    query: options.force ? { force: true } : undefined,
    signal: options.signal,
  })
}
