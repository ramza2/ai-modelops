import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  Deployment,
  Endpoint,
  EndpointRoute,
  Paginated,
  PreflightPreview,
  SwitchOperation,
} from '../api/types'
import { EndpointDetailPage } from '../pages/EndpointDetailPage'
import * as endpointsApi from '../api/endpoints'
import * as deploymentsApi from '../api/deployments'
import * as decommissionApi from '../api/decommission'
import * as modelsApi from '../api/models'
import type { UnpublishResult } from '../api/decommission'

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
        <Route path="/endpoints/:endpointId" element={<EndpointDetailPage />} />
        <Route path="/endpoints" element={<div>Endpoints list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('EndpointDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(endpointsApi, 'getEndpoint')
    vi.spyOn(endpointsApi, 'listEndpointRoutes')
    vi.spyOn(endpointsApi, 'createPreflightPreview')
    vi.spyOn(endpointsApi, 'switchEndpoint')
    vi.spyOn(endpointsApi, 'listEndpoints')
    vi.spyOn(deploymentsApi, 'listDeployments')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(decommissionApi, 'unpublishEndpoint')
    vi.spyOn(modelsApi, 'getModel')
    vi.spyOn(modelsApi, 'getModelVersion')
  })

  afterEach(() => {
    cleanup()
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
    expect(await screen.findByLabelText('Target Deployment')).toBeInTheDocument()
    const select = screen.getByLabelText('Target Deployment')
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
      screen.queryByRole('button', { name: 'Unpublish' }),
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
    const select = await screen.findByLabelText('Target Deployment')
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    })
    expect(
      screen.queryByRole('button', { name: 'Switch Enqueue' }),
    ).not.toBeInTheDocument()

    await user.selectOptions(
      screen.getByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText(/기존 Endpoint 정보를 표시/)).toBeInTheDocument()
    })
    expect(screen.getByText('preview_only')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Switch Enqueue' }),
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
    await screen.findByLabelText('Target Deployment')
    await user.selectOptions(
      screen.getByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()
    expect(screen.getByText('worker_must_revalidate')).toBeInTheDocument()
    expect(screen.getByText('gpu-0').closest('tr')).toBeTruthy()
    expect(screen.getByText('gpu-1').closest('tr')).toBeTruthy()
    const strategy = screen.getByLabelText('Strategy')
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    await screen.findByLabelText('Strategy')
    expect(
      within(screen.getByLabelText('Strategy')).getByRole('option', {
        name: 'HOT',
      }),
    ).toBeDisabled()
    expect(
      within(screen.getByLabelText('Strategy')).getByRole('option', {
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    expect(
      await screen.findByText(/RESOURCE_INSUFFICIENT preview에서는 Switch/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Switch Enqueue' }),
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    expect(await screen.findByText('preview_only')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Target Deployment'), 'target-b')
    expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Switch Enqueue' }),
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    await screen.findByRole('button', { name: 'Switch Enqueue' })
    await user.click(screen.getByRole('button', { name: 'Switch Enqueue' }))
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
      expect(endpointsApi.getEndpoint).toHaveBeenCalledTimes(2)
    })
    // Successful re-read invalidates preview; DRAINING blocks further enqueue UI.
    expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Switch Enqueue' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText(/traffic_state가 DRAINING/)).toBeInTheDocument()
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    await screen.findByRole('button', { name: 'Switch Enqueue' })
    await user.click(screen.getByRole('button', { name: 'Switch Enqueue' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Enqueue 중…' })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()
    expect(
      screen.getByRole('button', { name: 'Preflight Preview' }),
    ).toBeDisabled()
    resolveSwitch(makeOp())
    await waitFor(() => {
      expect(screen.getByText('op-sw')).toBeInTheDocument()
    })
    // Post-switch re-read clears preview; enqueue controls are not left active.
    await waitFor(() => {
      expect(screen.queryByText('preview_only')).not.toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: '새로고침' })).not.toBeDisabled()
    expect(
      screen.getByRole('button', { name: 'Preflight Preview' }),
    ).not.toBeDisabled()
    expect(
      screen.queryByRole('button', { name: 'Switch Enqueue' }),
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
      await screen.findByLabelText('Target Deployment'),
      'target-dep',
    )
    await user.click(screen.getByRole('button', { name: 'Preflight Preview' }))
    await user.click(await screen.findByRole('button', { name: 'Switch Enqueue' }))
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
      await screen.findByText('Endpoint를 찾을 수 없습니다.'),
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
    await screen.findByText(/ACTIVE Route가 없습니다/)
    expect(
      screen.queryByRole('button', { name: 'Unpublish' }),
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
        screen.queryByRole('button', { name: 'Unpublish' }),
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
    expect(screen.getByLabelText('Target Deployment')).not.toBeDisabled()
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
    await screen.findByText(/ACTIVE Route가 없습니다/)
    expect(
      screen.queryByRole('button', { name: 'Unpublish' }),
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
})
