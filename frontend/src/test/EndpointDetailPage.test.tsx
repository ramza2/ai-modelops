import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MemoryRouter,
  Route,
  Routes,
  useSearchParams,
} from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  Deployment,
  Endpoint,
  EndpointRoute,
  OperationDetail,
  Paginated,
  PreflightPreview,
  SwitchOperation,
} from '../api/types'
import { EndpointDetailPage } from '../pages/EndpointDetailPage'
import * as endpointsApi from '../api/endpoints'
import * as deploymentsApi from '../api/deployments'
import * as decommissionApi from '../api/decommission'
import * as operationsApi from '../api/operations'
import * as modelsApi from '../api/models'
import type { UnpublishResult } from '../api/decommission'
import { operationPollConfig } from '../utils/operationLifecycle'

function makeEndpoint(overrides: Partial<Endpoint> = {}): Endpoint {
  return {
    id: 'e1',
    alias: 'chat-main',
    display_name: 'Chat Main',
    api_type: 'CHAT',
    description: 'demo',
    is_enabled: true,
    traffic_state: 'SERVING',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    active_route: {
      route_id: 'r-active',
      deployment_id: 'source-dep',
      rewrite_model_name: 'served',
      activated_at: '2026-10-01T00:00:00Z',
      deployment: {
        id: 'source-dep',
        name: 'source-prod',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
        upstream_base_url: null,
        retired_at: null,
      },
    },
    ...overrides,
  }
}

function makeRoute(
  overrides: Partial<EndpointRoute> & Pick<EndpointRoute, 'id'>,
): EndpointRoute {
  return {
    endpoint_alias_id: 'e1',
    deployment_id: 'source-dep',
    status: 'ACTIVE',
    rewrite_model_name: null,
    operation_id: null,
    activated_at: '2026-10-01T00:00:00Z',
    deactivated_at: null,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function makeDeployment(
  overrides: Partial<Deployment> & Pick<Deployment, 'id' | 'name'>,
): Deployment {
  return {
    model_version_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    node_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    deployment_type: 'MANAGED',
    desired_state: 'RUNNING',
    runtime_status: 'RUNNING',
    health_status: 'HEALTHY',
    container_id: null,
    container_name: 'c',
    upstream_base_url: null,
    runtime_port: 8000,
    deployment_config: {},
    gpu_assignments: [],
    last_started_at: null,
    last_stopped_at: null,
    last_health_at: null,
    status_reason: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    retired_at: null,
    ...overrides,
  }
}

function makePreview(
  overrides: Partial<PreflightPreview> = {},
): PreflightPreview {
  return {
    id: 'pf1',
    operation_id: null,
    endpoint_id: 'e1',
    node_id: 'n1',
    target_model_version_id: 'v1',
    source_deployment_id: 'source-dep',
    target_deployment_id: 'target-dep',
    result: 'HOT_SWITCH_AVAILABLE',
    required_peak_vram_mb: 16000,
    available_hot_vram_mb: 18000,
    reclaimable_vram_mb: 8000,
    available_after_reclaim_mb: 26000,
    safety_margin_mb: 1024,
    gpu_results: [
      {
        gpu_device_id: 'gpu-0',
        free_vram_mb: 10000,
        reclaimable_vram_mb: 4000,
        required_vram_mb: 8000,
        available_hot_vram_mb: 9000,
        available_after_reclaim_mb: 13000,
        effective_available_mb: 9000,
        result: 'HOT_SWITCH_AVAILABLE',
        safety_margin_mb: 1024,
      },
      {
        gpu_device_id: 'gpu-1',
        free_vram_mb: 8000,
        reclaimable_vram_mb: 4000,
        required_vram_mb: 8000,
        available_hot_vram_mb: 7000,
        available_after_reclaim_mb: 11000,
        effective_available_mb: 7000,
        result: 'COLD_SWITCH_ONLY',
        safety_margin_mb: 1024,
      },
    ],
    evaluated_at: '2026-10-07T00:00:00Z',
    preview_only: true,
    worker_must_revalidate: true,
    ...overrides,
  }
}

function makeOp(overrides: Partial<SwitchOperation> = {}): SwitchOperation {
  return {
    id: 'op-sw',
    operation_id: 'op-sw',
    operation_type: 'SWITCH',
    switch_strategy: 'HOT',
    status: 'QUEUED',
    endpoint_alias_id: 'e1',
    source_deployment_id: 'source-dep',
    target_deployment_id: 'target-dep',
    current_step: 'VALIDATE',
    created_at: '2026-10-07T00:00:00Z',
    error: null,
    ...overrides,
  }
}

function makeOperationDetail(
  overrides: Partial<OperationDetail> = {},
): OperationDetail {
  return {
    id: 'op-sw',
    operation_type: 'SWITCH',
    status: 'QUEUED',
    switch_strategy: 'HOT',
    endpoint_alias_id: 'e1',
    source_deployment_id: 'source-dep',
    target_deployment_id: 'target-dep',
    current_step: 'VALIDATE',
    cancel_requested_at: null,
    retry_of_operation_id: null,
    requested_by: null,
    request_reason: null,
    created_at: '2026-10-07T00:00:00Z',
    started_at: null,
    finished_at: null,
    error: null,
    steps: [],
    ...overrides,
  }
}

function SearchProbe() {
  const [params] = useSearchParams()
  return (
    <div data-testid="search-probe">{params.toString() || '(empty)'}</div>
  )
}

function makeUnpublishResult(
  overrides: Partial<UnpublishResult> = {},
): UnpublishResult {
  return {
    endpoint: {
      id: 'e1',
      alias: 'chat-main',
      api_type: 'CHAT',
      active_route: null,
    },
    previous_route: {
      id: 'r-active',
      deployment_id: 'source-dep',
      status: 'INACTIVE',
      rewrite_model_name: 'served',
    },
    routing_version: 4,
    changed: true,
    gateway_verification: {
      status: 'PASSED',
      applied_routing_version: 4,
      active_deployment_id: null,
    },
    ...overrides,
  }
}

function pageResult(items: Deployment[]): Paginated<Deployment> {
  return { items, page: 1, page_size: 100, total: items.length }
}

function renderDetail(path = '/endpoints/e1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/endpoints/:endpointId"
          element={
            <>
              <SearchProbe />
              <EndpointDetailPage />
            </>
          }
        />
        <Route path="/endpoints" element={<div>Endpoints list</div>} />
        <Route
          path="/operations/:operationId"
          element={<div>Operation detail</div>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('EndpointDetailPage', () => {
  beforeEach(() => {
    operationPollConfig.ms = 4000
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
    vi.spyOn(endpointsApi, 'getEndpoint')
    vi.spyOn(endpointsApi, 'listEndpointRoutes')
    vi.spyOn(endpointsApi, 'createPreflightPreview')
    vi.spyOn(endpointsApi, 'switchEndpoint')
    vi.spyOn(endpointsApi, 'listEndpoints')
    vi.spyOn(deploymentsApi, 'listDeployments')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(decommissionApi, 'unpublishEndpoint')
    vi.spyOn(operationsApi, 'getOperation')
    vi.spyOn(modelsApi, 'getModel')
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.mocked(operationsApi.getOperation).mockImplementation(async (id) =>
      makeOperationDetail({ id }),
    )
  })

  afterEach(() => {
    cleanup()
    operationPollConfig.ms = 4000
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
    vi.restoreAllMocks()
  })

  it('renders identity, ACTIVE route, history, and loads targets without Model N+1', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [
        makeRoute({ id: 'r1', status: 'ACTIVE' }),
        makeRoute({
          id: 'r0',
          status: 'INACTIVE',
          deployment_id: 'old-dep',
          deactivated_at: '2026-09-01T00:00:00Z',
        }),
      ],
      total: 2,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'source-dep', name: 'source-prod' }),
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'source-prod' })).toHaveAttribute(
      'href',
      '/deployments/source-dep',
    )
    expect(await screen.findByLabelText(/Target Deployment/)).toBeInTheDocument()
    const select = screen.getByLabelText(/Target Deployment/)
    expect(within(select).queryByText(/source-prod/)).not.toBeInTheDocument()
    expect(within(select).getByText(/target-candidate/)).toBeInTheDocument()
    expect(modelsApi.getModel).not.toHaveBeenCalled()
    expect(modelsApi.getModelVersion).not.toHaveBeenCalled()
    expect(deploymentsApi.getDeployment).not.toHaveBeenCalled()
  })

  it('hides switch workflow when disabled, missing ACTIVE route, or not SERVING', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ is_enabled: false }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [],
      total: 0,
    })
    renderDetail()
    expect(
      await screen.findByText(/Disabled Endpoint에서는 Switch/),
    ).toBeInTheDocument()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ active_route: null }),
    )
    renderDetail()
    expect(
      await screen.findByText(/ACTIVE Source Route가 있어야/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Unpublish/ }),
    ).not.toBeInTheDocument()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ traffic_state: 'DRAINING' }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    renderDetail()
    expect(
      await screen.findByText(/traffic_state가 DRAINING/),
    ).toBeInTheDocument()
    expect(screen.getByText(/SERVING일 때만 Switch enqueue/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Target Deployment')).not.toBeInTheDocument()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ traffic_state: 'MAINTENANCE' }),
    )
    renderDetail()
    expect(
      await screen.findByText(/traffic_state가 MAINTENANCE/),
    ).toBeInTheDocument()
    expect(deploymentsApi.listDeployments).not.toHaveBeenCalled()
  })

  it('loads all non-retired Deployment pages for Target selector', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockImplementation(
      async (params) => {
        const page = params?.page ?? 1
        const pageSize = params?.pageSize ?? 200
        expect(params?.retired).toBe(false)
        expect(pageSize).toBe(200)
        if (page === 1) {
          return {
            items: [
              makeDeployment({ id: 'source-dep', name: 'source-prod' }),
              ...Array.from({ length: 199 }, (_, i) =>
                makeDeployment({
                  id: `page1-${i}`,
                  name: `target-p1-${i}`,
                }),
              ),
            ],
            page: 1,
            page_size: 200,
            total: 201,
          }
        }
        return {
          items: [makeDeployment({ id: 'page2-last', name: 'target-p2-last' })],
          page: 2,
          page_size: 200,
          total: 201,
        }
      },
    )
    renderDetail()
    const select = await screen.findByLabelText(/Target Deployment/)
    await waitFor(() => {
      expect(deploymentsApi.listDeployments).toHaveBeenCalledTimes(2)
    })
    expect(within(select).queryByText(/source-prod/)).not.toBeInTheDocument()
    expect(within(select).getByText(/target-p1-0/)).toBeInTheDocument()
    expect(within(select).getByText(/target-p2-last/)).toBeInTheDocument()
    expect(modelsApi.getModelVersion).not.toHaveBeenCalled()
    expect(deploymentsApi.getDeployment).not.toHaveBeenCalled()
  })

  it('invalidates preview on successful refresh but keeps it on non-404 failure', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValueOnce(makeEndpoint())
      .mockRejectedValueOnce(new ApiError('refresh boom', { status: 500 }))
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    })
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()

    await user.selectOptions(
      screen.getByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText(/기존 Endpoint 정보를 표시/)).toBeInTheDocument()
    })
    expect(screen.getByText('preview_only')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Switch Enqueue/ }),
    ).toBeInTheDocument()
  })

  it('renders every GPU result and strategy gates HOT/COLD/insufficient', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    renderDetail()
    await screen.findByLabelText(/Target Deployment/)
    await user.selectOptions(
      screen.getByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()
    expect(screen.getByText('worker_must_revalidate')).toBeInTheDocument()
    expect(screen.getByText('gpu-0').closest('tr')).toBeTruthy()
    expect(screen.getByText('gpu-1').closest('tr')).toBeTruthy()
    const strategy = screen.getByLabelText(/Strategy/)
    expect(within(strategy).getByRole('option', { name: 'HOT' })).toBeEnabled()
    expect(within(strategy).getByRole('option', { name: 'COLD' })).toBeEnabled()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview({ result: 'COLD_SWITCH_ONLY' }),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await screen.findByLabelText(/Strategy/)
    expect(
      within(screen.getByLabelText(/Strategy/)).getByRole('option', {
        name: 'HOT',
      }),
    ).toBeDisabled()
    expect(
      within(screen.getByLabelText(/Strategy/)).getByRole('option', {
        name: 'COLD',
      }),
    ).toBeEnabled()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview({ result: 'RESOURCE_INSUFFICIENT' }),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    expect(
      await screen.findByText(/RESOURCE_INSUFFICIENT preview에서는 Switch/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
  })

  it('invalidates preview when target changes', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-a' }),
        makeDeployment({ id: 'target-b', name: 'target-b' }),
      ]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText(/Target Deployment/), 'target-b')
    expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
  })

  it('enqueues HOT switch with operation display and re-read', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValueOnce(makeEndpoint({ traffic_state: 'DRAINING' }))
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockResolvedValue(makeOp())
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await screen.findByRole('button', { name: /Switch Enqueue/ })
    await user.click(screen.getByRole('button', { name: /Switch Enqueue/ }))
    await waitFor(() => {
      expect(endpointsApi.switchEndpoint).toHaveBeenCalledWith(
        'e1',
        expect.objectContaining({
          targetDeploymentId: 'target-dep',
          strategy: 'HOT',
        }),
      )
    })
    expect(await screen.findByText('op-sw')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('search-probe')).toHaveTextContent(
        /switch_operation_id=op-sw/,
      )
    })
    await waitFor(() => {
      expect(endpointsApi.getEndpoint).toHaveBeenCalledTimes(2)
    })
    // Successful re-read invalidates preview; in-flight Switch blocks enqueue UI.
    expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
    expect(screen.getByTestId('switch-op-active')).toBeInTheDocument()
  })

  it('mutually excludes refresh/preview/switch while enqueue is in flight', async () => {
    const user = userEvent.setup()
    let resolveSwitch!: (value: SwitchOperation) => void
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSwitch = resolve
        }),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await screen.findByRole('button', { name: /Switch Enqueue/ })
    await user.click(screen.getByRole('button', { name: /Switch Enqueue/ }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Enqueue 중…' })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()
    expect(
      screen.getByRole('button', { name: /Preflight Preview/ }),
    ).toBeDisabled()
    resolveSwitch(makeOp())
    await waitFor(() => {
      expect(screen.getByText('op-sw')).toBeInTheDocument()
    })
    // Post-switch re-read clears preview; active Switch blocks further enqueue UI.
    await waitFor(() => {
      expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Preflight Preview/ }),
    ).not.toBeInTheDocument()
  })

  it('keeps Endpoint snapshot on switch 409', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'target-dep', name: 'target-candidate' })]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockRejectedValue(
      new ApiError('active switch exists', { status: 409 }),
    )
    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await user.click(await screen.findByRole('button', { name: /Switch Enqueue/ }))
    expect(await screen.findByText(/active switch exists/i)).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'source-prod' })).toBeInTheDocument()
  })

  it('authoritative 404 clears stale; non-404 refresh keeps stale', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(endpointsApi.listEndpointRoutes)
      .mockResolvedValueOnce({ items: [makeRoute({ id: 'r1' })], total: 1 })
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText(/API 연결을 찾을 수 없습니다/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'Chat Main' }),
    ).not.toBeInTheDocument()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockRejectedValueOnce(new ApiError('boom', { status: 500 }))
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'Chat Main' })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText(/기존 Endpoint 정보를 표시/)).toBeInTheDocument()
    })
    expect(
      screen.getByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
  })

  it('does not call CRUD, route mutation, cancel/retry, or observability', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [],
      total: 0,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'Chat Main' })
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    const methods = fetchSpy.mock.calls.map(
      (c) => (c[1] as RequestInit | undefined)?.method ?? 'GET',
    )
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/observability'))).toBe(false)
    expect(urls.some((u) => u.includes('/cancel'))).toBe(false)
    expect(urls.some((u) => u.includes('/retry'))).toBe(false)
    expect(
      urls.some(
        (u, i) =>
          u.includes('/endpoints/e1/route') &&
          !u.includes('/routes') &&
          methods[i] === 'POST',
      ),
    ).toBe(false)
    expect(
      methods.some(
        (m, i) =>
          (m === 'PATCH' || m === 'POST') &&
          urls[i]?.includes('/api/v1/endpoints/e1') &&
          !urls[i]?.includes('/switch') &&
          !urls[i]?.includes('/routes') &&
          !urls[i]?.includes('/unpublish'),
      ),
    ).toBe(false)
    expect(endpointsApi.listEndpoints).not.toHaveBeenCalled()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('hides Unpublish when there is no ACTIVE Route', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ active_route: null }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [],
      total: 0,
    })
    renderDetail()
    await screen.findByText(/ACTIVE Route\)이 없습니다/)
    expect(
      screen.queryByRole('button', { name: /Unpublish/ }),
    ).not.toBeInTheDocument()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('cancels Unpublish confirm without calling API', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    expect(await screen.findByTestId('unpublish-dialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '취소' }))
    expect(screen.queryByTestId('unpublish-dialog')).not.toBeInTheDocument()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('blocks Unpublish when Alias confirmation does not match', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'wrong-alias',
    )
    expect(screen.getByTestId('unpublish-confirm')).toBeDisabled()
    // Force click path should still refuse even if enabled somehow.
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('posts Unpublish with expectedDeploymentId and verifyGateway once', async () => {
    const user = userEvent.setup()
    let resolveUnpublish!: (value: UnpublishResult) => void
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValue(
        makeEndpoint({
          active_route: null,
          traffic_state: 'SERVING',
        }),
      )
    vi.mocked(endpointsApi.listEndpointRoutes)
      .mockResolvedValueOnce({
        items: [makeRoute({ id: 'r1', status: 'ACTIVE' })],
        total: 1,
      })
      .mockResolvedValue({
        items: [
          makeRoute({
            id: 'r1',
            status: 'INACTIVE',
            deactivated_at: '2026-10-07T01:00:00Z',
          }),
        ],
        total: 1,
      })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(decommissionApi.unpublishEndpoint).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUnpublish = resolve
        }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    await waitFor(() => {
      expect(screen.getByTestId('unpublish-confirm')).toBeDisabled()
    })
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(decommissionApi.unpublishEndpoint).toHaveBeenCalledTimes(1)
    expect(decommissionApi.unpublishEndpoint).toHaveBeenCalledWith(
      'e1',
      {
        expectedDeploymentId: 'source-dep',
        verifyGateway: true,
        reason: 'Endpoint detail unpublish',
      },
      expect.any(AbortSignal),
    )
    resolveUnpublish(makeUnpublishResult({ changed: true }))
    expect(await screen.findByTestId('unpublish-result')).toBeInTheDocument()
    expect(screen.getByTestId('unpublish-changed')).toHaveTextContent(
      /changed=true/,
    )
    expect(screen.getByTestId('unpublish-gateway-hint')).toHaveTextContent(
      /Gateway 검증 통과/,
    )
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: /Unpublish/ }),
      ).not.toBeInTheDocument()
    })
    expect(screen.getByText(/비활성/)).toBeInTheDocument()
    // load('refresh') bumps readGen; busy must still clear afterward.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    })
  })

  it('clears Unpublish busy after success refresh so actions restore', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(decommissionApi.unpublishEndpoint).mockResolvedValue(
      makeUnpublishResult({ changed: true }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(await screen.findByTestId('unpublish-result')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    })
    expect(screen.getByTestId('unpublish-open')).not.toBeDisabled()
    expect(screen.getByLabelText(/Target Deployment/)).not.toBeDisabled()
  })

  it('shows changed=false and non-PASSED Gateway verification accurately', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValue(makeEndpoint({ active_route: null }))
    vi.mocked(endpointsApi.listEndpointRoutes)
      .mockResolvedValueOnce({
        items: [makeRoute({ id: 'r1', status: 'ACTIVE' })],
        total: 1,
      })
      .mockResolvedValue({
        items: [
          makeRoute({
            id: 'r1',
            status: 'INACTIVE',
            deactivated_at: '2026-10-07T01:00:00Z',
          }),
        ],
        total: 1,
      })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(decommissionApi.unpublishEndpoint).mockResolvedValue(
      makeUnpublishResult({
        changed: false,
        gateway_verification: {
          status: 'ROUTING_PENDING',
          reason: 'still converging',
        },
      }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(await screen.findByTestId('unpublish-changed')).toHaveTextContent(
      /changed=false/,
    )
    expect(screen.getByTestId('unpublish-gateway-hint')).toHaveTextContent(
      /ROUTING_PENDING/,
    )
    expect(screen.getByTestId('unpublish-gateway-hint').textContent).not.toMatch(
      /^Gateway 검증 통과\./,
    )
    cleanup()

    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValue(makeEndpoint({ active_route: null }))
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(decommissionApi.unpublishEndpoint).mockResolvedValue(
      makeUnpublishResult({
        changed: true,
        gateway_verification: { status: 'GATEWAY_UNAVAILABLE' },
      }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(await screen.findByTestId('unpublish-gateway-hint')).toHaveTextContent(
      /GATEWAY_UNAVAILABLE/,
    )
    expect(screen.getByTestId('unpublish-gateway-hint')).toHaveTextContent(
      /성공으로 오인하지 마세요/,
    )
  })

  it('reloads after 409 ROUTE_TARGET_CHANGED without inventing success', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(makeEndpoint())
      .mockResolvedValue(
        makeEndpoint({
          active_route: {
            route_id: 'r-other',
            deployment_id: 'other-dep',
            rewrite_model_name: 'other',
            activated_at: '2026-10-07T02:00:00Z',
            deployment: {
              id: 'other-dep',
              name: 'other-prod',
              runtime_status: 'RUNNING',
              health_status: 'HEALTHY',
              upstream_base_url: null,
              retired_at: null,
            },
          },
        }),
      )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(decommissionApi.unpublishEndpoint).mockRejectedValue(
      new ApiError('route moved', {
        status: 409,
        code: 'ROUTE_TARGET_CHANGED',
      }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(
      await screen.findByText(/ROUTE_TARGET_CHANGED/),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('unpublish-result')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(endpointsApi.getEndpoint).toHaveBeenCalledTimes(2)
    })
    expect(await screen.findByRole('link', { name: 'other-prod' })).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    })
    expect(screen.getByTestId('unpublish-open')).not.toBeDisabled()
  })

  it('clears Unpublish busy after generic error refresh', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(decommissionApi.unpublishEndpoint).mockRejectedValue(
      new ApiError('upstream timeout', { status: 504, code: 'TIMEOUT' }),
    )
    renderDetail()
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    expect(
      await screen.findByText(/실행 결과를 추정하지 않습니다/),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('unpublish-result')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    })
    expect(screen.getByTestId('unpublish-open')).not.toBeDisabled()
  })

  it('restores ACTIVE Route from GET Endpoint after remount (F5)', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    const { unmount } = renderDetail()
    expect(await screen.findByTestId('unpublish-open')).toBeInTheDocument()
    unmount()
    cleanup()

    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(
      makeEndpoint({ active_route: null }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [
        makeRoute({
          id: 'r1',
          status: 'INACTIVE',
          deactivated_at: '2026-10-07T01:00:00Z',
        }),
      ],
      total: 1,
    })
    renderDetail()
    await screen.findByText(/ACTIVE Route\)이 없습니다/)
    expect(
      screen.queryByRole('button', { name: /Unpublish/ }),
    ).not.toBeInTheDocument()
  })

  it('ignores stale Unpublish response after Endpoint identity change', async () => {
    const user = userEvent.setup()
    let resolveUnpublish!: (value: UnpublishResult) => void
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async (id) =>
      makeEndpoint({
        id,
        alias: id === 'e1' ? 'chat-main' : 'chat-other',
        display_name: id === 'e1' ? 'Chat Main' : 'Chat Other',
      }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(decommissionApi.unpublishEndpoint).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUnpublish = resolve
        }),
    )

    renderDetail('/endpoints/e1')
    await user.click(await screen.findByTestId('unpublish-open'))
    await user.type(
      screen.getByLabelText(/현재 Alias를 입력/),
      'chat-main',
    )
    await user.click(screen.getByTestId('unpublish-confirm'))
    await waitFor(() => {
      expect(decommissionApi.unpublishEndpoint).toHaveBeenCalledTimes(1)
    })
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()

    cleanup()
    renderDetail('/endpoints/e2')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Other' }),
    ).toBeInTheDocument()
    // New Endpoint must not inherit prior Unpublish busy.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    })
    expect(screen.getByTestId('unpublish-open')).not.toBeDisabled()
    resolveUnpublish(makeUnpublishResult({ changed: true }))
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('unpublish-result')).not.toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Chat Other' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
  })

  it('polls QUEUED → RUNNING → SUCCEEDED and refreshes Endpoint once', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    let phase: 'queued' | 'running' | 'succeeded' = 'queued'
    let endpointReads = 0
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async () => {
      endpointReads += 1
      if (phase === 'succeeded') {
        return makeEndpoint({
          active_route: {
            route_id: 'r-target',
            deployment_id: 'target-dep',
            rewrite_model_name: 'served',
            activated_at: '2026-10-07T01:00:00Z',
            deployment: {
              id: 'target-dep',
              name: 'target-candidate',
              runtime_status: 'RUNNING',
              health_status: 'HEALTHY',
              upstream_base_url: null,
              retired_at: null,
            },
          },
        })
      }
      return makeEndpoint()
    })
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockResolvedValue(makeOp())
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (phase === 'queued') {
        phase = 'running'
        return makeOperationDetail({ status: 'QUEUED', current_step: 'VALIDATE' })
      }
      if (phase === 'running') {
        phase = 'succeeded'
        return makeOperationDetail({
          status: 'RUNNING',
          current_step: 'ACTIVATE_TARGET_ROUTE',
        })
      }
      return makeOperationDetail({
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        finished_at: '2026-10-07T01:00:00Z',
      })
    })

    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await user.click(await screen.findByRole('button', { name: /Switch Enqueue/ }))
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(screen.getByTestId('search-probe')).toHaveTextContent(
      /switch_operation_id=op-sw/,
    )
    expect(endpointsApi.switchEndpoint).toHaveBeenCalledTimes(1)

    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'target-candidate' })).toBeInTheDocument()
    const readsAfterTerminal = endpointReads
    await new Promise((r) => setTimeout(r, 120))
    expect(endpointReads).toBe(readsAfterTerminal)
    const opCalls = vi.mocked(operationsApi.getOperation).mock.calls.length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(opCalls)
  })

  it('stops polling on FAILED and ROLLED_BACK / MANUAL_INTERVENTION_REQUIRED', async () => {
    operationPollConfig.ms = 40
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))

    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({
        status: 'FAILED',
        current_step: 'ACTIVATE_TARGET_ROUTE',
        error: { code: 'SWITCH_FAILED', message: 'activate failed' },
      }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
    expect(screen.getByText(/activate failed/)).toBeInTheDocument()
    const failedCalls = vi.mocked(operationsApi.getOperation).mock.calls.length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      failedCalls,
    )
    cleanup()

    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({
        status: 'ROLLED_BACK',
        current_step: 'ROLLBACK',
      }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-terminal')).toHaveTextContent(
      /ROLLED_BACK/,
    )
    cleanup()

    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({
        status: 'MANUAL_INTERVENTION_REQUIRED',
        current_step: 'ROLLBACK',
      }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-terminal')).toHaveTextContent(
      /MANUAL_INTERVENTION_REQUIRED/,
    )
  })

  it('blocks duplicate Switch Enqueue while tracked Operation is active', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({ status: 'RUNNING', current_step: 'DRAIN' }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
    expect(endpointsApi.switchEndpoint).not.toHaveBeenCalled()
    // Unpublish also blocked while Switch is active.
    expect(screen.getByTestId('unpublish-open')).toBeDisabled()
    await user.click(screen.getByTestId('unpublish-open'))
    expect(screen.queryByTestId('unpublish-dialog')).not.toBeInTheDocument()
  })

  it('restores Switch Operation from URL on F5 and resumes polling', async () => {
    operationPollConfig.ms = 40
    let status: 'RUNNING' | 'SUCCEEDED' = 'RUNNING'
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async () =>
      makeOperationDetail({
        status,
        current_step: status === 'RUNNING' ? 'DRAIN' : 'FINALIZE',
      }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(operationsApi.getOperation).toHaveBeenCalledWith(
      'op-sw',
      expect.any(AbortSignal),
    )
    status = 'SUCCEEDED'
    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
  })

  it('rejects Switch Operation owned by another Endpoint', async () => {
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({
        endpoint_alias_id: 'other-endpoint',
        status: 'RUNNING',
      }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(
      await screen.findByText(/이 Endpoint의 SWITCH가 아니므로/),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('switch-operation-result')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('search-probe')).toHaveTextContent('(empty)')
    })
  })

  it('keeps last Operation snapshot on poll failure and allows manual refresh', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    let failPoll = false
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (failPoll) {
        throw new ApiError('poll boom', { status: 503 })
      }
      return makeOperationDetail({
        status: 'RUNNING',
        current_step: 'DRAIN',
      })
    })
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    failPoll = true
    expect(
      await screen.findByText(/마지막 정상값을 유지/),
    ).toBeInTheDocument()
    expect(screen.getByTestId('switch-operation-result')).toHaveTextContent(
      /DRAIN/,
    )

    failPoll = false
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(
        screen.queryByText(/마지막 정상값을 유지/),
      ).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('switch-op-active')).toBeInTheDocument()
  })

  it('ignores late Switch Operation response after Endpoint identity change', async () => {
    let resolveOp!: (value: OperationDetail) => void
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async (id) =>
      makeEndpoint({
        id,
        alias: id === 'e1' ? 'chat-main' : 'chat-other',
        display_name: id === 'e1' ? 'Chat Main' : 'Chat Other',
      }),
    )
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveOp = resolve
        }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    await waitFor(() => {
      expect(operationsApi.getOperation).toHaveBeenCalled()
    })
    cleanup()
    renderDetail('/endpoints/e2')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Other' }),
    ).toBeInTheDocument()
    resolveOp(
      makeOperationDetail({
        status: 'SUCCEEDED',
        endpoint_alias_id: 'e1',
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('switch-operation-result')).not.toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Chat Other' }),
    ).toBeInTheDocument()
  })

  it('fail-closes Switch/Unpublish while F5 Operation restore is still pending', async () => {
    let resolveOp!: (value: OperationDetail) => void
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(operationsApi.getOperation).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveOp = resolve
        }),
    )
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    // Endpoint GET can finish first; actions stay blocked until Operation verified.
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
    expect(await screen.findByTestId('switch-op-unresolved')).toBeInTheDocument()
    expect(screen.getByTestId('unpublish-open')).toBeDisabled()
    expect(
      screen.queryByRole('button', { name: /Switch Enqueue/ }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Preflight Preview/ }),
    ).not.toBeInTheDocument()

    resolveOp(
      makeOperationDetail({ status: 'RUNNING', current_step: 'DRAIN' }),
    )
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(screen.queryByTestId('switch-op-unresolved')).not.toBeInTheDocument()
  })

  it('keeps ACTIVE Operation snapshot when post-enqueue restore GET fails transiently', async () => {
    const user = userEvent.setup()
    let restoreFailed = false
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockResolvedValue(makeOp())
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (restoreFailed) {
        throw new ApiError('restore boom', { status: 503 })
      }
      return makeOperationDetail({ status: 'QUEUED' })
    })

    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await user.click(await screen.findByRole('button', { name: /Switch Enqueue/ }))
    expect(await screen.findByTestId('switch-operation-result')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('search-probe')).toHaveTextContent(
        /switch_operation_id=op-sw/,
      )
    })

    restoreFailed = true
    // Force restore effect by remounting same URL state via cleanup+render with query.
    cleanup()
    // Seed last known path: render with URL; first GET fails but... we need snapshot.
    // Simulate by rendering after enqueue path: open with URL and a prior successful
    // getOperation once, then fail on the restore that follows remount.
    restoreFailed = false
    vi.mocked(operationsApi.getOperation)
      .mockResolvedValueOnce(
        makeOperationDetail({ status: 'RUNNING', current_step: 'DRAIN' }),
      )
      .mockRejectedValue(new ApiError('restore boom', { status: 503 }))
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    // Trigger another restore by clicking refresh while GET fails — snapshot kept.
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText(/마지막 정상값을 유지/),
    ).toBeInTheDocument()
    expect(screen.getByTestId('switch-operation-result')).toHaveTextContent(
      /DRAIN|RUNNING|실행/,
    )
    expect(screen.getByTestId('search-probe')).toHaveTextContent(
      /switch_operation_id=op-sw/,
    )
  })

  it('recovers Switch tracking via manual Refresh after restore failure', async () => {
    const user = userEvent.setup()
    let fail = true
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (fail) throw new ApiError('temp down', { status: 503 })
      return makeOperationDetail({
        status: 'RUNNING',
        current_step: 'DRAIN',
      })
    })
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-unresolved')).toBeInTheDocument()
    expect(
      await screen.findByText(/마지막 정상 Operation 값은 유지/),
    ).toBeInTheDocument()
    // Fail-closed remains — no auto unblock.
    expect(screen.getByTestId('unpublish-open')).toBeDisabled()

    fail = false
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    expect(screen.queryByTestId('switch-op-unresolved')).not.toBeInTheDocument()
  })

  it('keeps ownership-mismatch poll warning after stopping tracking', async () => {
    operationPollConfig.ms = 40
    let mismatched = false
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (mismatched) {
        return makeOperationDetail({
          status: 'RUNNING',
          endpoint_alias_id: 'other-endpoint',
        })
      }
      return makeOperationDetail({
        status: 'RUNNING',
        current_step: 'DRAIN',
      })
    })
    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    mismatched = true
    expect(
      await screen.findByTestId('switch-op-poll-warning'),
    ).toHaveTextContent(/소속이 일치하지 않아/)
    expect(screen.queryByTestId('switch-operation-result')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('search-probe')).toHaveTextContent('(empty)')
    })
    // Warning must remain after tracking cleared.
    await new Promise((r) => setTimeout(r, 80))
    expect(screen.getByTestId('switch-op-poll-warning')).toBeInTheDocument()
  })

  it('re-reads Endpoint/Routes once when F5 restore finds Terminal', async () => {
    let endpointReads = 0
    let resolveOp!: (value: OperationDetail) => void
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async () => {
      endpointReads += 1
      if (endpointReads <= 1) {
        return makeEndpoint()
      }
      return makeEndpoint({
        active_route: {
          route_id: 'r-target',
          deployment_id: 'target-dep',
          rewrite_model_name: 'served',
          activated_at: '2026-10-07T01:00:00Z',
          deployment: {
            id: 'target-dep',
            name: 'target-candidate',
            runtime_status: 'RUNNING',
            health_status: 'HEALTHY',
            upstream_base_url: null,
            retired_at: null,
          },
        },
      })
    })
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveOp = resolve
        }),
    )

    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat Main' }),
    ).toBeInTheDocument()
    expect(await screen.findByTestId('switch-op-unresolved')).toBeInTheDocument()
    const readsAfterEndpointFirst = endpointReads
    expect(readsAfterEndpointFirst).toBeGreaterThanOrEqual(1)

    resolveOp(
      makeOperationDetail({
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        finished_at: '2026-10-07T01:00:00Z',
      }),
    )
    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
    expect(
      await screen.findByRole('link', { name: 'target-candidate' }),
    ).toBeInTheDocument()
    await waitFor(() => {
      expect(endpointReads).toBeGreaterThan(readsAfterEndpointFirst)
    })
    const readsAfterTerminal = endpointReads
    await new Promise((r) => setTimeout(r, 120))
    expect(endpointReads).toBe(readsAfterTerminal)
  })

  it('does not duplicate Route refresh when enqueue already refreshed Terminal', async () => {
    const user = userEvent.setup()
    let endpointReads = 0
    let switched = false
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async () => {
      endpointReads += 1
      if (!switched) return makeEndpoint()
      return makeEndpoint({
        active_route: {
          route_id: 'r-target',
          deployment_id: 'target-dep',
          rewrite_model_name: 'served',
          activated_at: '2026-10-07T01:00:00Z',
          deployment: {
            id: 'target-dep',
            name: 'target-candidate',
            runtime_status: 'RUNNING',
            health_status: 'HEALTHY',
            upstream_base_url: null,
            retired_at: null,
          },
        },
      })
    })
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'target-dep', name: 'target-candidate' }),
      ]),
    )
    vi.mocked(endpointsApi.createPreflightPreview).mockResolvedValue(
      makePreview(),
    )
    vi.mocked(endpointsApi.switchEndpoint).mockImplementation(async () => {
      switched = true
      return makeOp({ status: 'SUCCEEDED', current_step: 'FINALIZE' })
    })
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOperationDetail({
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
      }),
    )

    renderDetail()
    await user.selectOptions(
      await screen.findByLabelText(/Target Deployment/),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: /Preflight Preview/ }))
    await user.click(await screen.findByRole('button', { name: /Switch Enqueue/ }))
    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('search-probe')).toHaveTextContent(
        /switch_operation_id=op-sw/,
      )
    })
    const readsAfterEnqueue = endpointReads
    await new Promise((r) => setTimeout(r, 120))
    // Restore GET sees Terminal but must not re-read again (already marked).
    expect(endpointReads).toBe(readsAfterEnqueue)
  })

  it('re-reads Endpoint after Manual Refresh first discovers Terminal', async () => {
    const user = userEvent.setup()
    let endpointReads = 0
    let opStatus: 'RUNNING' | 'SUCCEEDED' = 'RUNNING'
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async () => {
      endpointReads += 1
      if (opStatus === 'SUCCEEDED') {
        return makeEndpoint({
          active_route: {
            route_id: 'r-target',
            deployment_id: 'target-dep',
            rewrite_model_name: 'served',
            activated_at: '2026-10-07T01:00:00Z',
            deployment: {
              id: 'target-dep',
              name: 'target-candidate',
              runtime_status: 'RUNNING',
              health_status: 'HEALTHY',
              upstream_base_url: null,
              retired_at: null,
            },
          },
        })
      }
      return makeEndpoint()
    })
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async () =>
      makeOperationDetail({
        status: opStatus,
        current_step: opStatus === 'RUNNING' ? 'DRAIN' : 'FINALIZE',
      }),
    )

    renderDetail('/endpoints/e1?switch_operation_id=op-sw')
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()
    const readsWhileRunning = endpointReads

    opStatus = 'SUCCEEDED'
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByTestId('switch-op-terminal')).toBeInTheDocument()
    expect(
      await screen.findByRole('link', { name: 'target-candidate' }),
    ).toBeInTheDocument()
    // Manual refresh: Endpoint GET, then Operation Terminal, then one more Route GET.
    await waitFor(() => {
      expect(endpointReads).toBeGreaterThan(readsWhileRunning + 1)
    })
    const afterTerminal = endpointReads
    await new Promise((r) => setTimeout(r, 120))
    expect(endpointReads).toBe(afterTerminal)
  })

  it('ignores late Refresh Operation response after URL Operation ID changes', async () => {
    const user = userEvent.setup()
    let resolveRefreshOp!: (value: OperationDetail) => void
    let holdRefresh = false
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(makeEndpoint())
    vi.mocked(endpointsApi.listEndpointRoutes).mockResolvedValue({
      items: [makeRoute({ id: 'r1' })],
      total: 1,
    })
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    vi.mocked(operationsApi.getOperation).mockImplementation(async (id) => {
      if (holdRefresh && id === 'op-sw') {
        return new Promise((resolve) => {
          resolveRefreshOp = resolve
        })
      }
      if (id === 'op-sw-2') {
        return makeOperationDetail({
          id: 'op-sw-2',
          status: 'QUEUED',
          current_step: 'VALIDATE',
        })
      }
      return makeOperationDetail({
        id: 'op-sw',
        status: 'RUNNING',
        current_step: 'DRAIN',
      })
    })

    function SwapOpButton() {
      const [, setParams] = useSearchParams()
      return (
        <button
          type="button"
          onClick={() => {
            const next = new URLSearchParams()
            next.set('switch_operation_id', 'op-sw-2')
            setParams(next, { replace: true })
          }}
        >
          swap-op
        </button>
      )
    }

    render(
      <MemoryRouter initialEntries={['/endpoints/e1?switch_operation_id=op-sw']}>
        <Routes>
          <Route
            path="/endpoints/:endpointId"
            element={
              <>
                <SearchProbe />
                <SwapOpButton />
                <EndpointDetailPage />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByTestId('switch-op-active')).toBeInTheDocument()

    holdRefresh = true
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(resolveRefreshOp).toBeTypeOf('function')
    })

    await user.click(screen.getByRole('button', { name: 'swap-op' }))
    expect(await screen.findByTestId('search-probe')).toHaveTextContent(
      /switch_operation_id=op-sw-2/,
    )
    expect(await screen.findByTestId('switch-operation-result')).toHaveTextContent(
      /VALIDATE|QUEUED|대기/,
    )

    resolveRefreshOp(
      makeOperationDetail({
        id: 'op-sw',
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByTestId('switch-operation-result')).toHaveTextContent(
      /VALIDATE|QUEUED|대기/,
    )
    expect(screen.getByTestId('switch-operation-result')).not.toHaveTextContent(
      /FINALIZE/,
    )
  })
})
