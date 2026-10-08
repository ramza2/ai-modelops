import { apiGet, apiPost } from './client'
import type {
  HfCatalogModel,
  HfCatalogPage,
  ResourceFitAnalysis,
} from './types'

export type ListHfCatalogParams = {
  q?: string | null
  modelType?: string | null
  page?: number
  pageSize?: number
  fitOnly?: boolean
  nodeId?: string | null
  signal?: AbortSignal
}

export async function listHfCatalog(
  params: ListHfCatalogParams = {},
): Promise<HfCatalogPage<HfCatalogModel>> {
  return apiGet<HfCatalogPage<HfCatalogModel>>(
    '/api/v1/catalog/huggingface/models',
    {
      query: {
        q: params.q || undefined,
        model_type: params.modelType || undefined,
        page: params.page ?? 1,
        page_size: params.pageSize ?? 20,
        fit_only: params.fitOnly ? true : undefined,
        node_id: params.nodeId || undefined,
      },
      signal: params.signal,
    },
  )
}

export type AnalyzeResourceFitBody = {
  repositoryId: string
  revision?: string | null
  nodeId: string
  modelType?: string | null
  tensorParallel?: number
}

export async function analyzeHfResourceFit(
  body: AnalyzeResourceFitBody,
  signal?: AbortSignal,
): Promise<ResourceFitAnalysis> {
  return apiPost<ResourceFitAnalysis>(
    '/api/v1/catalog/huggingface/resource-fit',
    {
      body: {
        repository_id: body.repositoryId,
        revision: body.revision ?? null,
        node_id: body.nodeId,
        model_type: body.modelType ?? null,
        tensor_parallel: body.tensorParallel ?? 1,
      },
      signal,
    },
  )
}
