import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  InvocationSummaryResponse,
  RuntimeLatestResponse,
} from '../api/types'
import { ObservabilityPage } from '../pages/ObservabilityPage'
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
        sampled_at: '2026-10-07T00:00:00Z',
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
    expect(await screen.findByText('alzi')).toBeInTheDocument()
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

  it.each([
    ['/observability?hours=48&group_by=alias', 'hours=48&group_by=alias'],
    ['/observability?hours=9999', ''],
    ['/observability?group_by=OTHER', ''],
    ['/observability?hours=024&group_by=DEPLOYMENT', 'hours=24&group_by=deployment'],
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

  it('keeps each section on independent refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getInvocationSummary)
      .mockResolvedValueOnce(summary())
      .mockRejectedValueOnce(new ApiError('inv boom', { status: 500 }))
    vi.mocked(observabilityApi.getRuntimeLatest)
      .mockResolvedValueOnce(latest())
      .mockRejectedValueOnce(new ApiError('rt boom', { status: 500 }))
    renderAt('/observability')
    expect(await screen.findByText('alzi')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'chat-prod' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText(/inv boom/)).toBeInTheDocument()
    expect(screen.getByText(/rt boom/)).toBeInTheDocument()
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
    await screen.findByText('alzi')
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
