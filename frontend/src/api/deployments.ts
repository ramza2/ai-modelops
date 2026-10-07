import { apiGet, apiPost } from './client'
import type { Deployment, LifecycleOperation, Paginated } from './types'

export type ListDeploymentsParams = {
  nodeId?: string | null
  modelId?: string | null
  modelVersionId?: string | null
  deploymentType?: string | null
  runtimeStatus?: string | null
  healthStatus?: string | null
  retired?: boolean | null
  page?: number
  pageSize?: number
  signal?: AbortSignal
}

export async function listDeployments(
  params: ListDeploymentsParams = {},
): Promise<Paginated<Deployment>> {
  const query: Record<string, string | number | boolean | null | undefined> = {
    page: params.page ?? 1,
    page_size: params.pageSize ?? 20,
  }
  if (params.nodeId) query.node_id = params.nodeId
  if (params.modelId) query.model_id = params.modelId
  if (params.modelVersionId) query.model_version_id = params.modelVersionId
  if (params.deploymentType) query.deployment_type = params.deploymentType
  if (params.runtimeStatus) query.runtime_status = params.runtimeStatus
  if (params.healthStatus) query.health_status = params.healthStatus
  if (params.retired === true || params.retired === false) {
    query.retired = params.retired
  }
  return apiGet<Paginated<Deployment>>('/api/v1/deployments', {
    query,
    signal: params.signal,
  })
}

export async function getDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<Deployment> {
  return apiGet<Deployment>(`/api/v1/deployments/${deploymentId}`, { signal })
}

function lifecycleOptions(signal?: AbortSignal) {
  return {
    signal,
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  }
}

export async function startDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<LifecycleOperation> {
  return apiPost<LifecycleOperation>(
    `/api/v1/deployments/${deploymentId}/start`,
    lifecycleOptions(signal),
  )
}

export async function stopDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<LifecycleOperation> {
  return apiPost<LifecycleOperation>(
    `/api/v1/deployments/${deploymentId}/stop`,
    lifecycleOptions(signal),
  )
}

export async function restartDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<LifecycleOperation> {
  return apiPost<LifecycleOperation>(
    `/api/v1/deployments/${deploymentId}/restart`,
    lifecycleOptions(signal),
  )
}
