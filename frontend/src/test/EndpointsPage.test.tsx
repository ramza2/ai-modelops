import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { Endpoint, Paginated } from '../api/types'
import { EndpointsPage } from '../pages/EndpointsPage'
import * as endpointsApi from '../api/endpoints'
import * as deploymentsApi from '../api/deployments'
import * as modelsApi from '../api/models'

function makeEndpoint(
  overrides: Partial<Endpoint> & Pick<Endpoint, 'id' | 'alias'>,
): Endpoint {
  return {
    display_name: overrides.display_name ?? overrides.alias,
    api_type: 'CHAT',
    description: null,
    is_enabled: true,
    traffic_state: 'SERVING',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    active_route: {
      route_id: 'r1',
      deployment_id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
      rewrite_model_name: null,
      activated_at: '2026-10-01T00:00:00Z',
      deployment: {
        id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
        name: 'chat-prod',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
        upstream_base_url: null,
        retired_at: null,
      },
    },
    ...overrides,
  }
}

function pageResult(
  items: Endpoint[],
  opts: { page?: number; total?: number } = {},
): Paginated<Endpoint> {
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
        <Route path="/endpoints" element={<EndpointsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('EndpointsPage', () => {
  beforeEach(() => {
    vi.spyOn(endpointsApi, 'listEndpoints')
    vi.spyOn(endpointsApi, 'getEndpoint')
    vi.spyOn(endpointsApi, 'listEndpointRoutes')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(modelsApi, 'getModelVersion')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders list and links without row N+1', async () => {
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(
      pageResult([
        makeEndpoint({ id: 'e1', alias: 'chat-main', display_name: 'Chat Main' }),
        makeEndpoint({
          id: 'e2',
          alias: 'embed',
          display_name: 'Embed',
          api_type: 'EMBEDDING',
          active_route: null,
        }),
      ]),
    )
    renderAt('/endpoints')
    expect(await screen.findByRole('link', { name: 'Chat Main' })).toHaveAttribute(
      'href',
      '/endpoints/e1',
    )
    expect(screen.getByRole('link', { name: 'chat-prod' })).toHaveAttribute(
      'href',
      '/deployments/dddddddd-dddd-dddd-dddd-dddddddddddd',
    )
    expect(endpointsApi.listEndpoints).toHaveBeenCalledTimes(1)
    expect(endpointsApi.getEndpoint).not.toHaveBeenCalled()
    expect(endpointsApi.listEndpointRoutes).not.toHaveBeenCalled()
    expect(deploymentsApi.getDeployment).not.toHaveBeenCalled()
    expect(modelsApi.getModelVersion).not.toHaveBeenCalled()
  })

  it('applies filters and submits search without keystroke requests', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(pageResult([]))
    renderAt('/endpoints')
    await screen.findByLabelText('API Type')
    await waitFor(() => {
      expect(endpointsApi.listEndpoints).toHaveBeenCalled()
    })
    const before = vi.mocked(endpointsApi.listEndpoints).mock.calls.length
    await user.type(screen.getByLabelText('검색'), 'demo')
    expect(vi.mocked(endpointsApi.listEndpoints).mock.calls.length).toBe(before)
    await user.click(screen.getByRole('button', { name: '검색' }))
    await waitFor(() => {
      const last = vi.mocked(endpointsApi.listEndpoints).mock.calls.at(-1)?.[0]
      expect(last?.q).toBe('demo')
      expect(last?.page).toBe(1)
    })
    await user.selectOptions(screen.getByLabelText('API Type'), 'CHAT')
    await user.selectOptions(screen.getByLabelText('Enabled'), 'ENABLED')
    await user.selectOptions(screen.getByLabelText('Traffic'), 'DRAINING')
    await waitFor(() => {
      const last = vi.mocked(endpointsApi.listEndpoints).mock.calls.at(-1)?.[0]
      expect(last).toEqual(
        expect.objectContaining({
          apiType: 'CHAT',
          isEnabled: true,
          trafficState: 'DRAINING',
          page: 1,
        }),
      )
    })
  })

  it.each([
    ['/endpoints?api_type=chat', 'api_type=CHAT'],
    ['/endpoints?api_type=ALL', ''],
    ['/endpoints?is_enabled=TRUE', 'is_enabled=true'],
    ['/endpoints?traffic_state=OTHER', ''],
    [
      '/endpoints?api_type=chat&is_enabled=true&q=x&page=002',
      'api_type=CHAT&is_enabled=true&q=x&page=2',
    ],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('preserves rows on refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.listEndpoints)
      .mockResolvedValueOnce(
        pageResult([makeEndpoint({ id: 'e1', alias: 'keep', display_name: 'Keep' })]),
      )
      .mockRejectedValueOnce(new ApiError('refresh fail', { status: 503 }))
    renderAt('/endpoints')
    expect(await screen.findByRole('link', { name: 'Keep' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh fail')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Keep' })).toBeInTheDocument()
  })

  it('uses scope=col and avoids forbidden endpoints', async () => {
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(
      pageResult([makeEndpoint({ id: 'e1', alias: 'x', display_name: 'X' })]),
    )
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/endpoints')
    await screen.findByRole('link', { name: 'X' })
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/preflights'))).toBe(false)
    expect(urls.some((u) => u.includes('/switch'))).toBe(false)
  })
})
