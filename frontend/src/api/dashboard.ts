import { apiGet } from './client'
import type {
  DeploymentSummary,
  EndpointSummary,
  HealthResponse,
  InvocationSummaryResponse,
  NodeSummary,
  OperationSummary,
  Paginated,
  ReadyResponse,
} from './types'

export class DashboardUnavailableError extends Error {
  constructor(message = 'Management API를 사용할 수 없습니다.') {
    super(message)
    this.name = 'DashboardUnavailableError'
  }
}

export type DashboardSnapshot = {
  health: HealthResponse | null
  healthError: string | null
  ready: ReadyResponse | null
  readyError: string | null
  nodesTotal: number | null
  nodesOnline: number | null
  nodesError: string | null
  deploymentsActive: number | null
  deploymentsRunning: number | null
  deploymentsHealthy: number | null
  deploymentsError: string | null
  endpointsTotal: number | null
  endpointsEnabled: number | null
  endpointsServing: number | null
  endpointsError: string | null
  operationsActive: number | null
  operationsRecent: OperationSummary[] | null
  operationsError: string | null
  invocations: InvocationSummaryResponse | null
  invocationsError: string | null
  fetchedAt: Date
}

function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const name = (err as { name?: string }).name
  return name === 'AbortError'
}

async function settle<T>(
  promise: Promise<T>,
): Promise<{ value: T | null; error: string | null; ok: boolean }> {
  try {
    return { value: await promise, error: null, ok: true }
  } catch (err) {
    if (isAbortError(err)) {
      throw err
    }
    const message =
      err instanceof Error ? err.message : '요청에 실패했습니다.'
    return { value: null, error: message, ok: false }
  }
}

export async function fetchDashboard(
  signal?: AbortSignal,
): Promise<DashboardSnapshot> {
  const [
    health,
    ready,
    nodesTotal,
    nodesOnline,
    depActive,
    depRunning,
    depHealthy,
    epTotal,
    epEnabled,
    epServing,
    opsActive,
    opsRecent,
    invocations,
  ] = await Promise.all([
    settle(apiGet<HealthResponse>('/health', { signal })),
    settle(apiGet<ReadyResponse>('/ready', { signal })),
    settle(
      apiGet<Paginated<NodeSummary>>('/api/v1/nodes', {
        query: { page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<NodeSummary>>('/api/v1/nodes', {
        query: { status: 'ONLINE', page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<DeploymentSummary>>('/api/v1/deployments', {
        query: { retired: false, page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<DeploymentSummary>>('/api/v1/deployments', {
        query: {
          retired: false,
          runtime_status: 'RUNNING',
          page: 1,
          page_size: 1,
        },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<DeploymentSummary>>('/api/v1/deployments', {
        query: {
          retired: false,
          health_status: 'HEALTHY',
          page: 1,
          page_size: 1,
        },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<EndpointSummary>>('/api/v1/endpoints', {
        query: { page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<EndpointSummary>>('/api/v1/endpoints', {
        query: { is_enabled: true, page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<EndpointSummary>>('/api/v1/endpoints', {
        query: { traffic_state: 'SERVING', page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<OperationSummary>>('/api/v1/operations', {
        query: { active: true, page: 1, page_size: 1 },
        signal,
      }),
    ),
    settle(
      apiGet<Paginated<OperationSummary>>('/api/v1/operations', {
        query: { page: 1, page_size: 8 },
        signal,
      }),
    ),
    settle(
      apiGet<InvocationSummaryResponse>(
        '/api/v1/observability/invocations/summary',
        {
          query: { hours: 24, group_by: 'deployment' },
          signal,
        },
      ),
    ),
  ])

  const settled = [
    health,
    ready,
    nodesTotal,
    nodesOnline,
    depActive,
    depRunning,
    depHealthy,
    epTotal,
    epEnabled,
    epServing,
    opsActive,
    opsRecent,
    invocations,
  ]
  const successCount = settled.filter((s) => s.ok).length
  if (successCount === 0) {
    throw new DashboardUnavailableError()
  }

  const nodesError = nodesTotal.error || nodesOnline.error
  const deploymentsError =
    depActive.error || depRunning.error || depHealthy.error
  const endpointsError = epTotal.error || epEnabled.error || epServing.error
  const operationsError = opsActive.error || opsRecent.error

  return {
    health: health.value,
    healthError: health.error,
    ready: ready.value,
    readyError: ready.error,
    nodesTotal: nodesTotal.value?.total ?? null,
    nodesOnline: nodesOnline.value?.total ?? null,
    nodesError,
    deploymentsActive: depActive.value?.total ?? null,
    deploymentsRunning: depRunning.value?.total ?? null,
    deploymentsHealthy: depHealthy.value?.total ?? null,
    deploymentsError,
    endpointsTotal: epTotal.value?.total ?? null,
    endpointsEnabled: epEnabled.value?.total ?? null,
    endpointsServing: epServing.value?.total ?? null,
    endpointsError,
    operationsActive: opsActive.value?.total ?? null,
    operationsRecent: opsRecent.value?.items ?? null,
    operationsError,
    invocations: invocations.value,
    invocationsError: invocations.error,
    fetchedAt: new Date(),
  }
}

export function aggregateInvocations(
  items: InvocationSummaryResponse['items'] | null | undefined,
): {
  requestCount: number
  successCount: number
  errorCount: number
  tokenizedRequestCount: number
  successRate: number | null
  tokenCoverage: number | null
  topDeployments: InvocationSummaryResponse['items']
} {
  const list = items ?? []
  const requestCount = list.reduce((s, i) => s + (i.request_count || 0), 0)
  const successCount = list.reduce((s, i) => s + (i.success_count || 0), 0)
  const errorCount = list.reduce((s, i) => s + (i.error_count || 0), 0)
  const tokenizedRequestCount = list.reduce(
    (s, i) => s + (i.tokenized_request_count || 0),
    0,
  )
  const successRate =
    requestCount > 0 ? successCount / requestCount : null
  const tokenCoverage =
    requestCount > 0 ? tokenizedRequestCount / requestCount : null
  const topDeployments = [...list]
    .sort((a, b) => (b.request_count || 0) - (a.request_count || 0))
    .slice(0, 5)
  return {
    requestCount,
    successCount,
    errorCount,
    tokenizedRequestCount,
    successRate,
    tokenCoverage,
    topDeployments,
  }
}
