import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { Deployment, Paginated } from '../api/types'
import { DeploymentsPage } from '../pages/DeploymentsPage'
import * as deploymentsApi from '../api/deployments'
import * as modelsApi from '../api/models'
import * as nodesApi from '../api/nodes'

function makeDeployment(
  overrides: Partial<Deployment> & Pick<Deployment, 'id' | 'name'>,
): Deployment {
  return {
    model_version_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    node_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    deployment_type: 'MANAGED',
    desired_state: 'STOPPED',
    runtime_status: 'CREATED',
    health_status: 'UNKNOWN',
    container_id: null,
    container_name: 'demo-container',
    upstream_base_url: 'http://internal-placeholder:8000',
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

function pageResult(
  items: Deployment[],
  opts: { page?: number; total?: number } = {},
): Paginated<Deployment> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    total: opts.total ?? items.length,
  }
}

function LocationProbe() {
  const [params] = useSearchParams()
  return <div data-testid="location-search">{params.toString()}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/deployments" element={<DeploymentsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('DeploymentsPage', () => {
  beforeEach(() => {
    vi.spyOn(deploymentsApi, 'listDeployments')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.spyOn(nodesApi, 'getNode')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders list and links to detail without row N+1', async () => {
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([
        makeDeployment({ id: 'd1', name: 'chat-prod' }),
        makeDeployment({
          id: 'd2',
          name: 'embed-import',
          deployment_type: 'IMPORTED',
          runtime_status: 'RUNNING',
          health_status: 'HEALTHY',
        }),
      ]),
    )
    renderAt('/deployments')
    expect(await screen.findByRole('link', { name: 'chat-prod' })).toHaveAttribute(
      'href',
      '/deployments/d1',
    )
    expect(screen.getByRole('link', { name: 'embed-import' })).toBeInTheDocument()
    expect(deploymentsApi.listDeployments).toHaveBeenCalledTimes(1)
    expect(deploymentsApi.getDeployment).not.toHaveBeenCalled()
    expect(modelsApi.getModelVersion).not.toHaveBeenCalled()
    expect(nodesApi.getNode).not.toHaveBeenCalled()
  })

  it('applies type/runtime/health/retired filters and resets page', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderAt('/deployments?page=2')
    await screen.findByLabelText('Type')
    await user.selectOptions(screen.getByLabelText('Type'), 'MANAGED')
    await waitFor(() => {
      const last = vi.mocked(deploymentsApi.listDeployments).mock.calls.at(-1)?.[0]
      expect(last?.deploymentType).toBe('MANAGED')
      expect(last?.page).toBe(1)
    })
    await user.selectOptions(screen.getByLabelText('Runtime'), 'RUNNING')
    await user.selectOptions(screen.getByLabelText('Health'), 'HEALTHY')
    await user.selectOptions(screen.getByLabelText('Retired'), 'ACTIVE')
    await waitFor(() => {
      const last = vi.mocked(deploymentsApi.listDeployments).mock.calls.at(-1)?.[0]
      expect(last).toEqual(
        expect.objectContaining({
          deploymentType: 'MANAGED',
          runtimeStatus: 'RUNNING',
          healthStatus: 'HEALTHY',
          retired: false,
          page: 1,
        }),
      )
    })
  })

  it.each([
    ['/deployments?deployment_type=managed', 'deployment_type=MANAGED'],
    ['/deployments?deployment_type=ALL', ''],
    ['/deployments?runtime_status=OTHER', ''],
    ['/deployments?retired=TRUE', 'retired=true'],
    [
      '/deployments?deployment_type=managed&runtime_status=running&page=002',
      'deployment_type=MANAGED&runtime_status=RUNNING&page=2',
    ],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('preserves rows on refresh failure and shows empty states', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValueOnce(pageResult([]))
    renderAt('/deployments')
    expect(
      await screen.findByText('등록된 Deployment가 없습니다.'),
    ).toBeInTheDocument()
    cleanup()

    vi.mocked(deploymentsApi.listDeployments)
      .mockResolvedValueOnce(
        pageResult([makeDeployment({ id: 'd1', name: 'Keep Dep' })]),
      )
      .mockRejectedValueOnce(new ApiError('refresh fail', { status: 503 }))
    renderAt('/deployments')
    expect(await screen.findByRole('link', { name: 'Keep Dep' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh fail')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Keep Dep' })).toBeInTheDocument()
  })

  it('uses scope=col and does not call forbidden endpoints', async () => {
    vi.mocked(deploymentsApi.listDeployments).mockResolvedValue(
      pageResult([makeDeployment({ id: 'd1', name: 'X' })]),
    )
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/deployments')
    await screen.findByRole('link', { name: 'X' })
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/resources/latest'))).toBe(false)
    expect(urls.some((u) => u.includes('/health'))).toBe(false)
    expect(urls.some((u) => u.includes('/preflights'))).toBe(false)
  })
})
