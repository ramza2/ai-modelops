import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  InvocationSummaryResponse,
  RuntimeLatestResponse,
} from '../api/types'
import {
  ClientGroupLabel,
  ObservabilityPage,
  displayedRuntimeAvailability,
} from '../pages/ObservabilityPage'
import * as observabilityApi from '../api/observability'
import * as deploymentsApi from '../api/deployments'
import * as clientsApi from '../api/clients'
import * as modelsApi from '../api/models'

function LocationProbe() {
  const [params] = useSearchParams()
  return <div data-testid="location-search">{params.toString()}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/observability" element={<ObservabilityPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function summary(
  overrides: Partial<InvocationSummaryResponse> = {},
): InvocationSummaryResponse {
  return {
    hours: 24,
    group_by: 'client',
    items: [
      {
        group_key: 'alzi',
        client_display_name: null,
        client_app_id: null,
        request_count: 10,
        success_count: 8,
        error_count: 2,
        tokenized_request_count: 5,
        input_tokens_p95: 100,
        output_tokens_avg: 20,
        latency_ms_p95: 250,
      },
    ],
    ...overrides,
  }
}

function latest(
  overrides: Partial<RuntimeLatestResponse> = {},
): RuntimeLatestResponse {
  return {
    items: [
      {
        deployment_id: 'dep-1',
        deployment_name: 'chat-prod',
        sampled_at: new Date().toISOString(),
        runtime_status: 'RUNNING',
        retired_at: null,
        availability: 'PARTIAL',
        kv_cache_usage_ratio: 0.42,
        num_requests_running: 2,
        num_requests_waiting: 1,
        prompt_tokens_total: 1000,
        generation_tokens_total: 500,
        error_code: null,
        error_message: null,
        runtime_instance: null,
      },
    ],
    ...overrides,
  }
}

describe('ObservabilityPage', () => {
  beforeEach(() => {
    vi.spyOn(observabilityApi, 'getInvocationSummary')
    vi.spyOn(observabilityApi, 'getRuntimeLatest')
    vi.spyOn(observabilityApi, 'getRuntimeHistory')
    vi.spyOn(observabilityApi, 'getCapacityProfile')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(clientsApi, 'getClient')
    vi.spyOn(modelsApi, 'getModel')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('loads summary and runtime latest independently without N+1', async () => {
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary(),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue(latest())
    renderAt('/observability')
    expect(await screen.findByText('미등록 시스템')).toBeInTheDocument()
    expect(screen.getByText('alzi')).toBeInTheDocument()
    expect(screen.getByText('50.0% (5/10)')).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'chat-prod' }),
    ).toHaveAttribute('href', '/observability/deployments/dep-1')
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument()
    expect(observabilityApi.getInvocationSummary).toHaveBeenCalledTimes(1)
    expect(observabilityApi.getRuntimeLatest).toHaveBeenCalledTimes(1)
    expect(observabilityApi.getRuntimeHistory).not.toHaveBeenCalled()
    expect(observabilityApi.getCapacityProfile).not.toHaveBeenCalled()
    expect(deploymentsApi.getDeployment).not.toHaveBeenCalled()
    expect(clientsApi.getClient).not.toHaveBeenCalled()
    expect(modelsApi.getModel).not.toHaveBeenCalled()
  })

  it('shows registered client display_name with detail link and service key', async () => {
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary({
        items: [
          {
            group_key: 'svc-ideaflow',
            client_display_name: 'Ideaflow',
            client_app_id: 'client-uuid-1',
            request_count: 3,
            success_count: 3,
            error_count: 0,
            tokenized_request_count: 2,
          },
          {
            group_key: 'unknown',
            client_display_name: null,
            client_app_id: null,
            request_count: 1,
            success_count: 0,
            error_count: 1,
            tokenized_request_count: 0,
          },
          {
            group_key: 'ghost-key',
            client_display_name: null,
            client_app_id: null,
            request_count: 2,
            success_count: 2,
            error_count: 0,
            tokenized_request_count: 0,
          },
        ],
      }),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue({ items: [] })
    renderAt('/observability')
    const link = await screen.findByRole('link', { name: 'Ideaflow' })
    expect(link).toHaveAttribute('href', '/clients/client-uuid-1')
    expect(screen.getByText('svc-ideaflow')).toBeInTheDocument()
    expect(screen.getByText('미식별 시스템')).toBeInTheDocument()
    expect(screen.getByText('미등록 시스템')).toBeInTheDocument()
    expect(screen.getByText('ghost-key')).toBeInTheDocument()
    expect(clientsApi.getClient).not.toHaveBeenCalled()
  })

  it('keeps alias group_key text unchanged', async () => {
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary({
        group_by: 'alias',
        items: [
          {
            group_key: 'chat-prod-alias',
            request_count: 4,
            success_count: 4,
            error_count: 0,
            tokenized_request_count: 4,
          },
        ],
      }),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue({ items: [] })
    renderAt('/observability?group_by=alias')
    expect(await screen.findByText('chat-prod-alias')).toBeInTheDocument()
    expect(screen.queryByText('미등록 시스템')).not.toBeInTheDocument()
    expect(screen.queryByText('미식별 시스템')).not.toBeInTheDocument()
  })

  it('shows active deployments by default and retired snapshots only on opt-in', async () => {
    const user = userEvent.setup()
    const current = latest().items[0]
    const retired = {
      ...current,
      deployment_id: 'dep-old',
      deployment_name: 'all-MiniLM-L6-v2-managed',
      sampled_at: '2026-10-15T03:57:42Z',
      availability: 'AVAILABLE',
      runtime_status: 'STOPPED',
      retired_at: '2026-10-19T03:28:56Z',
    }
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(summary())
    vi.mocked(observabilityApi.getRuntimeLatest).mockImplementation(
      async (_deploymentId, _signal, activeOnly) =>
        latest({ items: activeOnly ? [current] : [current, retired] }),
    )

    renderAt('/observability')
    expect(await screen.findByRole('link', { name: 'chat-prod' })).toBeInTheDocument()
    expect(observabilityApi.getRuntimeLatest).toHaveBeenCalledWith(
      null, expect.any(AbortSignal), true,
    )
    expect(screen.queryByRole('link', { name: 'all-MiniLM-L6-v2-managed' })).not.toBeInTheDocument()

    await user.click(screen.getByLabelText('종료·중지된 배포 포함 (과거 데이터)'))
    expect(await screen.findByRole('link', { name: 'all-MiniLM-L6-v2-managed' })).toBeInTheDocument()
    expect(screen.getByText(/RETIRED/)).toBeInTheDocument()
    expect(screen.getByText('수집 당시: AVAILABLE')).toBeInTheDocument()
    expect(observabilityApi.getRuntimeLatest).toHaveBeenLastCalledWith(
      null, expect.any(AbortSignal), false,
    )
    expect(screen.getByTestId('location-search')).toHaveTextContent('include_inactive=true')
    expect(observabilityApi.getInvocationSummary).toHaveBeenCalledTimes(1)

    await user.click(screen.getByLabelText('종료·중지된 배포 포함 (과거 데이터)'))
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'all-MiniLM-L6-v2-managed' })).not.toBeInTheDocument()
      expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/)
    })
  })

  it('does not show old AVAILABLE samples as current availability', () => {
    const row = latest().items[0]
    const now = Date.parse('2026-10-19T03:40:52Z')
    expect(displayedRuntimeAvailability({
      ...row,
      availability: 'AVAILABLE',
      sampled_at: '2026-10-15T03:57:42Z',
    }, now)).toBe('STALE')
    expect(displayedRuntimeAvailability({
      ...row,
      availability: 'AVAILABLE',
      sampled_at: '2026-10-15T03:57:42Z',
      retired_at: '2026-10-19T03:28:56Z',
    }, now)).toBe('RETIRED')
    expect(displayedRuntimeAvailability({
      ...row,
      availability: 'AVAILABLE',
      sampled_at: new Date(now - 30_000).toISOString(),
      runtime_status: 'STOPPED',
    }, now)).toBe('STOPPED')
    expect(displayedRuntimeAvailability({
      ...row,
      availability: 'AVAILABLE',
      sampled_at: new Date(now - 30_000).toISOString(),
    }, now)).toBe('AVAILABLE')
    expect(displayedRuntimeAvailability({
      ...row,
      availability: 'AVAILABLE',
      sampled_at: null,
    }, now)).toBe('STALE')
  })

  it.each([
    ['/observability?hours=48&group_by=alias', 'hours=48&group_by=alias'],
    ['/observability?hours=13', 'hours=13'],
    ['/observability?include_inactive=false', ''],
    ['/observability?include_inactive=TRUE', ''],
    ['/observability?include_inactive=true', 'include_inactive=true'],
    ['/observability?hours=9999', ''],
    ['/observability?group_by=OTHER', ''],
    ['/observability?hours=024&group_by=DEPLOYMENT', 'group_by=deployment'],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary({ items: [] }),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue({
      items: [],
    })
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('applies arbitrary valid hours through number control', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary({ items: [] }),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue({
      items: [],
    })
    renderAt('/observability?hours=13')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        'hours=13',
      )
    })
    await waitFor(() => {
      expect(observabilityApi.getInvocationSummary).toHaveBeenCalledWith(
        expect.objectContaining({ hours: 13 }),
      )
    })
    await user.clear(screen.getByLabelText(/Hours/))
    await user.type(screen.getByLabelText(/Hours/), '37')
    await user.click(screen.getByRole('button', { name: '적용' }))
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        'hours=37',
      )
    })
    await waitFor(() => {
      expect(observabilityApi.getInvocationSummary).toHaveBeenCalledWith(
        expect.objectContaining({ hours: 37 }),
      )
    })
  })

  it('keeps each section on independent refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getInvocationSummary)
      .mockResolvedValueOnce(summary())
      .mockRejectedValueOnce(new ApiError('inv boom', { status: 500 }))
    vi.mocked(observabilityApi.getRuntimeLatest)
      .mockResolvedValueOnce(latest())
      .mockRejectedValueOnce(new ApiError('rt boom', { status: 500 }))
    renderAt('/observability')
    expect(await screen.findByText('미등록 시스템')).toBeInTheDocument()
    expect(screen.getByText('alzi')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'chat-prod' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText(/inv boom/)).toBeInTheDocument()
    expect(screen.getByText(/rt boom/)).toBeInTheDocument()
    expect(screen.getByText('미등록 시스템')).toBeInTheDocument()
    expect(screen.getByText('alzi')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'chat-prod' })).toBeInTheDocument()
  })

  it('uses scope=col and avoids forbidden endpoints', async () => {
    vi.mocked(observabilityApi.getInvocationSummary).mockResolvedValue(
      summary(),
    )
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue(latest())
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/observability')
    await screen.findByText('미등록 시스템')
    for (const table of screen.getAllByRole('table')) {
      for (const th of within(table).getAllByRole('columnheader')) {
        expect(th).toHaveAttribute('scope', 'col')
      }
    }
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/analytics'))).toBe(false)
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/internal/'))).toBe(false)
  })
})

describe('ClientGroupLabel', () => {
  it('renders registered, unknown, and unregistered labels', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ClientGroupLabel
          item={{
            group_key: 'k1',
            client_display_name: 'Alpha',
            client_app_id: 'id-1',
            request_count: 1,
            success_count: 1,
            error_count: 0,
            tokenized_request_count: 0,
          }}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('link', { name: 'Alpha' })).toHaveAttribute(
      'href',
      '/clients/id-1',
    )
    expect(screen.getByText('k1')).toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <ClientGroupLabel
          item={{
            group_key: 'unknown',
            client_display_name: null,
            client_app_id: null,
            request_count: 1,
            success_count: 0,
            error_count: 1,
            tokenized_request_count: 0,
          }}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('미식별 시스템')).toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <ClientGroupLabel
          item={{
            group_key: 'orphan',
            client_display_name: null,
            client_app_id: null,
            request_count: 1,
            success_count: 1,
            error_count: 0,
            tokenized_request_count: 0,
          }}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('미등록 시스템')).toBeInTheDocument()
    expect(screen.getByText('orphan')).toBeInTheDocument()
  })
})
