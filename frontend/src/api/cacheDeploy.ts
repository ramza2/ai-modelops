import { apiGet, apiPost } from './client'
import type { Deployment, LifecycleOperation } from './types'

export type CacheDeployRuntimeConfig = {
  max_model_len?: number | null
  max_num_seqs?: number | null
  gpu_memory_utilization?: number | null
  tensor_parallel_size?: number | null
  dtype?: string | null
  quantization?: string | null
  runner?: string | null
  health_path?: string | null
  probe_type?: string | null
  scheduling_policy?: string | null
}

export type CreateCacheDeploymentBody = {
  name: string
  containerName: string
  gpuDeviceIds: string[]
  runtimePort?: number
  servedModelName?: string | null
  runtimeConfig?: CacheDeployRuntimeConfig
  expectedVramMb?: number | null
  acknowledgeUnknownFit?: boolean
}

export type CacheDeploymentResult = Deployment & {
  reused?: boolean
  source_cache_id?: string
  fit?: FreshFitResult | null
}

export type PublishDeploymentBody = {
  alias?: string | null
  endpointId?: string | null
  displayName?: string | null
  rewriteModelName?: string | null
  reason?: string | null
  verifyGateway?: boolean
}

export type PublishResult = {
  endpoint: {
    id: string
    alias: string
    api_type: string
    active_route?: unknown
  }
  route: {
    id: string
    deployment_id: string
    rewrite_model_name: string | null
    status: string
  }
  routing_version: number | null
  gateway_verification: {
    status: string
    http_status?: number
    reason?: string
    alias?: string
    api_type?: string
  } | null
  reused?: boolean
  note?: string
}

export type PublishStatusResult =
  | {
      published: false
      deployment_id: string
    }
  | (PublishResult & {
      published: true
      deployment_id: string
    })

export type FreshFitResult = {
  result: string
  tensor_parallel: number
  gpu_results: Array<{
    gpu_device_id: string
    gpu_index: number | null
    name: string | null
    vram_total_mb: number
    vram_free_mb: number
    safety_margin_mb: number
    estimated_required_vram_mb: number | null
    result: string
    reasons: string[]
  }>
  reasons: string[]
  warnings: string[]
  assumptions: string[]
  suggested_gpu_device_ids: string[]
  estimated_required_vram_mb: number | null
  estimated_required_vram_mb_per_gpu: number | null
  disk_gate: string
  evaluated_at: string | null
}

export async function previewCacheDeployFit(
  cacheId: string,
  body: {
    gpuDeviceIds: string[]
    tensorParallel?: number | null
    expectedVramMb?: number | null
    dtype?: string | null
    quantization?: string | null
  },
  signal?: AbortSignal,
): Promise<FreshFitResult> {
  return apiPost<FreshFitResult>(`/api/v1/model-cache/${cacheId}/fit-preview`, {
    signal,
    body: {
      gpu_device_ids: body.gpuDeviceIds,
      tensor_parallel: body.tensorParallel ?? null,
      expected_vram_mb: body.expectedVramMb ?? null,
      dtype: body.dtype ?? null,
      quantization: body.quantization ?? null,
    },
  })
}

export async function createDeploymentFromCache(
  cacheId: string,
  body: CreateCacheDeploymentBody,
  signal?: AbortSignal,
): Promise<CacheDeploymentResult> {
  return apiPost<CacheDeploymentResult>(
    `/api/v1/model-cache/${cacheId}/deployment`,
    {
      signal,
      body: {
        name: body.name,
        container_name: body.containerName,
        gpu_device_ids: body.gpuDeviceIds,
        runtime_port: body.runtimePort ?? 8000,
        served_model_name: body.servedModelName ?? null,
        expected_vram_mb: body.expectedVramMb ?? null,
        acknowledge_unknown_fit: body.acknowledgeUnknownFit ?? false,
        runtime_config: body.runtimeConfig
          ? {
              max_model_len: body.runtimeConfig.max_model_len ?? undefined,
              max_num_seqs: body.runtimeConfig.max_num_seqs ?? undefined,
              gpu_memory_utilization:
                body.runtimeConfig.gpu_memory_utilization ?? undefined,
              tensor_parallel_size:
                body.runtimeConfig.tensor_parallel_size ?? undefined,
              dtype: body.runtimeConfig.dtype ?? undefined,
              quantization: body.runtimeConfig.quantization ?? undefined,
              runner: body.runtimeConfig.runner ?? undefined,
              health_path: body.runtimeConfig.health_path ?? undefined,
              probe_type: body.runtimeConfig.probe_type ?? undefined,
              scheduling_policy:
                body.runtimeConfig.scheduling_policy ?? undefined,
            }
          : undefined,
      },
    },
  )
}

export async function publishCacheDeployment(
  deploymentId: string,
  body: PublishDeploymentBody,
  signal?: AbortSignal,
): Promise<PublishResult> {
  return apiPost<PublishResult>(
    `/api/v1/model-cache/deployments/${deploymentId}/publish`,
    {
      signal,
      body: {
        alias: body.alias ?? null,
        endpoint_id: body.endpointId ?? null,
        display_name: body.displayName ?? null,
        rewrite_model_name: body.rewriteModelName ?? null,
        reason: body.reason ?? null,
        verify_gateway: body.verifyGateway ?? true,
      },
    },
  )
}

export async function getCacheDeployPublishStatus(
  deploymentId: string,
  body?: { verifyGateway?: boolean },
  signal?: AbortSignal,
): Promise<PublishStatusResult> {
  return apiGet<PublishStatusResult>(
    `/api/v1/model-cache/deployments/${deploymentId}/publish-status`,
    {
      signal,
      query: {
        verify_gateway: body?.verifyGateway ?? false,
      },
    },
  )
}

export async function getCacheEntry(
  cacheId: string,
  signal?: AbortSignal,
): Promise<import('./types').ModelCacheEntry | null> {
  // List and find — no dedicated get-by-id yet.
  const page = await apiGet<{
    items: import('./types').ModelCacheEntry[]
  }>('/api/v1/model-cache', {
    query: { page: 1, page_size: 100 },
    signal,
  })
  return page.items.find((i) => i.id === cacheId) ?? null
}

// Re-export lifecycle helpers used by the wizard for clarity.
export type { LifecycleOperation }
