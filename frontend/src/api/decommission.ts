import { apiGet, apiPost } from './client'
import { purgeModelCache as purgeCache } from './downloads'
import type { Deployment, LifecycleOperation, ModelSummary, ModelVersion } from './types'

export type DecommissionBlocker = {
  code: string
  message: string
  [key: string]: unknown
}

export type DecommissionStatus = {
  deployment_id: string
  deployment_type: string
  desired_state: string
  runtime_status: string
  health_status: string
  retired_at: string | null
  active_routes: Array<{
    endpoint_id: string
    alias: string
    route_id: string
    rewrite_model_name?: string | null
    routing_version?: number | null
  }>
  container_present: boolean | null
  container?: {
    present: boolean | null
    applicable?: boolean
    runtime_status?: string | null
    container_id?: string | null
    error?: string
  }
  gpu_assignments: Array<{
    gpu_device_id: string
    device_order: number
    device_index: number
    gpu_uuid: string
    model_name: string
  }>
  source_cache: {
    cache_id: string
    status: string
    local_path: string | null
    repository_id?: string | null
    revision?: string | null
    size_bytes?: number | null
  } | null
  active_operation: {
    operation_id: string
    operation_type: string
    status: string
  } | null
  can_unpublish: boolean
  can_stop: boolean
  can_remove_container: boolean
  can_retire: boolean
  can_purge_cache: boolean
  blockers: DecommissionBlocker[]
  note?: string
}

export type UnpublishResult = {
  endpoint: {
    id: string
    alias: string
    api_type: string
    active_route?: unknown
  }
  previous_route: {
    id: string
    deployment_id: string
    status: string
    rewrite_model_name: string | null
  } | null
  routing_version: number | null
  changed: boolean
  gateway_verification: {
    status: string
    reason?: string
    applied_routing_version?: number | null
    active_deployment_id?: string | null
  } | null
}

export async function getDecommissionStatus(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<DecommissionStatus> {
  return apiGet<DecommissionStatus>(
    `/api/v1/deployments/${deploymentId}/decommission-status`,
    { signal },
  )
}

export async function unpublishEndpoint(
  endpointId: string,
  body: {
    expectedDeploymentId?: string | null
    reason?: string | null
    verifyGateway?: boolean
  },
  signal?: AbortSignal,
): Promise<UnpublishResult> {
  return apiPost<UnpublishResult>(`/api/v1/endpoints/${endpointId}/unpublish`, {
    signal,
    body: {
      expected_deployment_id: body.expectedDeploymentId ?? null,
      reason: body.reason ?? null,
      verify_gateway: body.verifyGateway ?? true,
    },
  })
}

export async function removeDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<LifecycleOperation> {
  return apiPost<LifecycleOperation>(
    `/api/v1/deployments/${deploymentId}/remove`,
    {
      signal,
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    },
  )
}

export async function retireDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<Deployment> {
  return apiPost<Deployment>(`/api/v1/deployments/${deploymentId}/retire`, {
    signal,
  })
}

export async function purgeModelCache(
  cacheId: string,
  opts?: { force?: boolean; signal?: AbortSignal },
): Promise<unknown> {
  return purgeCache(cacheId, {
    force: opts?.force ?? false,
    signal: opts?.signal,
  })
}

export async function archiveModelVersion(
  versionId: string,
  signal?: AbortSignal,
): Promise<ModelVersion> {
  return apiPost<ModelVersion>(`/api/v1/model-versions/${versionId}/archive`, {
    signal,
  })
}

export async function archiveModel(
  modelId: string,
  signal?: AbortSignal,
): Promise<ModelSummary> {
  return apiPost<ModelSummary>(`/api/v1/models/${modelId}/archive`, { signal })
}
