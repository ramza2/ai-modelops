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
import * as modelsApi from '../api/models'

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

  it('hides switch workflow when disabled or missing ACTIVE route', async () => {
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
      expect(screen.getByRole('button', { name: 'Switch Enqueue' })).not.toBeDisabled()
    })
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
          !urls[i]?.includes('/routes'),
      ),
    ).toBe(false)
    expect(endpointsApi.listEndpoints).not.toHaveBeenCalled()
  })
})
