import { apiGet } from './client'
import type {
  ModelArtifact,
  ModelDetail,
  ModelSummary,
  ModelVersion,
  Paginated,
} from './types'

export type ListModelsParams = {
  q?: string | null
  provider?: string | null
  modelType?: string | null
  isActive?: boolean | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listModels(
  params: ListModelsParams = {},
): Promise<Paginated<ModelSummary>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  if (params.q) query.q = params.q
  if (params.provider) query.provider = params.provider
  if (params.modelType) query.model_type = params.modelType
  if (params.isActive === true || params.isActive === false) {
    query.is_active = params.isActive
  }
  return apiGet<Paginated<ModelSummary>>('/api/v1/models', {
    query,
    signal: params.signal,
  })
}

export async function getModel(
  modelId: string,
  signal?: AbortSignal,
): Promise<ModelDetail> {
  return apiGet<ModelDetail>(`/api/v1/models/${modelId}`, { signal })
}

export type ListModelVersionsParams = {
  includeArchived?: boolean
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listModelVersions(
  modelId: string,
  params: ListModelVersionsParams = {},
): Promise<Paginated<ModelVersion>> {
  return apiGet<Paginated<ModelVersion>>(
    `/api/v1/models/${modelId}/versions`,
    {
      query: {
        include_archived: params.includeArchived ?? false,
        page: params.page ?? 1,
        page_size: params.pageSize ?? 20,
      },
      signal: params.signal,
    },
  )
}

export async function getModelVersion(
  versionId: string,
  signal?: AbortSignal,
): Promise<ModelVersion> {
  return apiGet<ModelVersion>(`/api/v1/model-versions/${versionId}`, {
    signal,
  })
}

export type ListModelArtifactsParams = {
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listModelArtifacts(
  versionId: string,
  params: ListModelArtifactsParams = {},
): Promise<Paginated<ModelArtifact>> {
  return apiGet<Paginated<ModelArtifact>>(
    `/api/v1/model-versions/${versionId}/artifacts`,
    {
      query: {
        page: params.page ?? 1,
        page_size: params.pageSize ?? 20,
      },
      signal: params.signal,
    },
  )
}
