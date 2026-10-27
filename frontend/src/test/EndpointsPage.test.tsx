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

  it('shows neutral traffic for disabled and unrouted aliases; keeps SERVING for live', async () => {
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(
      pageResult([
        makeEndpoint({
          id: 'e-disabled',
          alias: 'minilm-test',
          display_name: 'MiniLM',
          is_enabled: false,
          traffic_state: 'SERVING',
          active_route: null,
        }),
        makeEndpoint({
          id: 'e-unrouted',
          alias: 'orphan',
          display_name: 'Orphan',
          is_enabled: true,
          traffic_state: 'SERVING',
          active_route: null,
        }),
        makeEndpoint({
          id: 'e-live',
          alias: 'chat-main',
          display_name: 'Chat Main',
        }),
      ]),
    )
    renderAt('/endpoints')
    await screen.findByRole('link', { name: 'MiniLM' })
    const badges = screen.getAllByTestId('endpoint-traffic-display')
    expect(badges).toHaveLength(3)
    expect(badges[0]).toHaveTextContent('사용 중지')
    expect(badges[0]).toHaveTextContent('Traffic: SERVING')
    expect(badges[0].className).toContain('muted')
    expect(badges[1]).toHaveTextContent('모델 미연결')
    expect(badges[2]).toHaveTextContent(/요청 처리 중/)
    expect(badges[2]).toHaveTextContent('(SERVING)')
    expect(badges[2].querySelector('.status-badge--ok')).toBeTruthy()
  })

  it('shows Korean API type labels and keeps filter option values as API codes', async () => {
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
    expect(
      (await screen.findAllByText('대화형 API (CHAT)')).length,
    ).toBeGreaterThan(0)
    expect(screen.getAllByText('임베딩 API (EMBEDDING)').length).toBeGreaterThan(
      0,
    )
    expect(screen.getByText(/검색어/)).toBeInTheDocument()
    const apiType = screen.getByLabelText(/API Type/)
    expect(
      within(apiType).getByRole('option', { name: /대화형 API \(CHAT\)/ }),
    ).toHaveValue('CHAT')
    expect(
      within(apiType).getByRole('option', {
        name: /임베딩 API \(EMBEDDING\)/,
      }),
    ).toHaveValue('EMBEDDING')
    const traffic = screen.getByLabelText(/Traffic/)
    expect(
      within(traffic).getByRole('option', {
        name: /요청 처리 중 \(SERVING\)/,
      }),
    ).toHaveValue('SERVING')
    expect(
      within(traffic).getByRole('option', {
        name: /요청 종료 중 \(DRAINING\)/,
      }),
    ).toHaveValue('DRAINING')
  })

  it('applies filters and submits search without keystroke requests', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(pageResult([]))
    renderAt('/endpoints')
    await screen.findByLabelText(/API Type/)
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
    await user.selectOptions(screen.getByLabelText(/API Type/), 'CHAT')
    await user.selectOptions(screen.getByLabelText(/Enabled/), 'ENABLED')
    await user.selectOptions(screen.getByLabelText(/Traffic/), 'DRAINING')
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
    expect(await screen.findByText(/refresh fail/)).toBeInTheDocument()
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

  it('defaults to full list and filters with 사용 설정된 API만 보기', async () => {
    const user = userEvent.setup()
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(pageResult([]))
    renderAt('/endpoints')
    await waitFor(() => {
      expect(endpointsApi.listEndpoints).toHaveBeenCalled()
    })
    const lastAll = vi.mocked(endpointsApi.listEndpoints).mock.calls.at(-1)?.[0]
    expect(lastAll?.isEnabled).toBeNull()
    expect(screen.getByText('사용 설정된 API만 보기')).toBeInTheDocument()
    expect(
      screen.getByText(/실제 모델 연결 여부와는 다릅니다/),
    ).toBeInTheDocument()
    await user.click(screen.getByTestId('endpoint-enabled-only'))
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        'is_enabled=true',
      )
    })
    const lastEnabled = vi.mocked(endpointsApi.listEndpoints).mock.calls.at(
      -1,
    )?.[0]
    expect(lastEnabled?.isEnabled).toBe(true)
  })

  it('distinguishes unused vs connected aliases and disables unused safely', async () => {
    const user = userEvent.setup()
    vi.spyOn(endpointsApi, 'updateEndpointEnabled')
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(
      pageResult([
        makeEndpoint({
          id: 'e-unused',
          alias: 'minilm-test',
          display_name: 'MiniLM Test',
          active_route: null,
        }),
        makeEndpoint({
          id: 'e-live',
          alias: 'chat-main',
          display_name: 'Chat Main',
        }),
      ]),
    )
    vi.mocked(endpointsApi.getEndpoint).mockImplementation(async (id) => {
      if (id === 'e-unused') {
        return makeEndpoint({
          id: 'e-unused',
          alias: 'minilm-test',
          display_name: 'MiniLM Test',
          active_route: null,
        })
      }
      return makeEndpoint({ id: 'e-live', alias: 'chat-main' })
    })
    vi.mocked(endpointsApi.updateEndpointEnabled).mockResolvedValue(
      makeEndpoint({
        id: 'e-unused',
        alias: 'minilm-test',
        display_name: 'MiniLM Test',
        is_enabled: false,
        active_route: null,
      }),
    )
    renderAt('/endpoints')
    expect(await screen.findByText('활성 연결 없음')).toBeInTheDocument()
    expect(screen.getByText('모델 연결됨')).toBeInTheDocument()

    await user.click(screen.getByTestId('endpoint-disable-e-unused'))
    expect(await screen.findByTestId('endpoint-enabled-dialog')).toBeInTheDocument()
    expect(screen.getByText(/ACTIVE Route가 없는 미사용 별칭/)).toBeInTheDocument()
    expect(
      screen.queryByTestId('endpoint-enabled-alias-confirm'),
    ).not.toBeInTheDocument()
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(endpointsApi.updateEndpointEnabled).toHaveBeenCalledWith(
        'e-unused',
        false,
        expect.anything(),
      )
    })
    expect(await screen.findByTestId('endpoint-enabled-result')).toHaveTextContent(
      /사용 중지/,
    )
  })

  it('requires alias confirm when disabling a connected alias', async () => {
    const user = userEvent.setup()
    vi.spyOn(endpointsApi, 'updateEndpointEnabled')
    const live = makeEndpoint({ id: 'e-live', alias: 'chat-main' })
    vi.mocked(endpointsApi.listEndpoints).mockResolvedValue(pageResult([live]))
    vi.mocked(endpointsApi.getEndpoint).mockResolvedValue(live)
    renderAt('/endpoints')
    await user.click(await screen.findByTestId('endpoint-disable-e-live'))
    expect(await screen.findByTestId('endpoint-enabled-alias-confirm')).toBeInTheDocument()
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    expect(screen.getByTestId('endpoint-enabled-confirm')).toBeDisabled()
    await user.type(screen.getByTestId('endpoint-enabled-alias-confirm'), 'chat-main')
    expect(screen.getByTestId('endpoint-enabled-confirm')).toBeEnabled()
    expect(endpointsApi.updateEndpointEnabled).not.toHaveBeenCalled()
  })
})
