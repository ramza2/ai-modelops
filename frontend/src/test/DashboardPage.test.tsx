import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { DashboardPage } from '../pages/DashboardPage'
import type { DashboardSnapshot } from '../api/dashboard'
import * as dashboardApi from '../api/dashboard'

function baseSnap(
  overrides: Partial<DashboardSnapshot> = {},
): DashboardSnapshot {
  return {
    health: { status: 'ok' },
    healthError: null,
    ready: { status: 'READY' },
    readyError: null,
    nodesTotal: 2,
    nodesOnline: 1,
    nodesError: null,
    deploymentsActive: 3,
    deploymentsRunning: 2,
    deploymentsHealthy: 2,
    deploymentsError: null,
    endpointsTotal: 4,
    endpointsEnabled: 3,
    endpointsServing: 2,
    endpointsError: null,
    operationsActive: 1,
    operationsRecent: [
      {
        id: 'op-1',
        operation_type: 'START',
        status: 'RUNNING',
        switch_strategy: null,
        endpoint_alias_id: null,
        source_deployment_id: null,
        target_deployment_id: 'dep-aaaa-bbbb',
        requested_by: null,
        request_reason: null,
        error_code: null,
        error_message: null,
        cancel_requested_at: null,
        retry_of_operation_id: null,
        created_at: '2026-10-06T00:00:00Z',
        started_at: '2026-10-06T00:00:01Z',
        finished_at: null,
      },
      {
        id: 'op-2',
        operation_type: 'STOP',
        status: 'SUCCEEDED',
        switch_strategy: null,
        endpoint_alias_id: 'ep-1',
        source_deployment_id: null,
        target_deployment_id: null,
        requested_by: null,
        request_reason: null,
        error_code: null,
        error_message: null,
        cancel_requested_at: null,
        retry_of_operation_id: null,
        created_at: '2026-10-05T23:00:00Z',
        started_at: '2026-10-05T23:00:01Z',
        finished_at: '2026-10-05T23:00:10Z',
      },
    ],
    operationsError: null,
    invocations: {
      hours: 24,
      group_by: 'deployment',
      items: [
        {
          group_key: 'dep-1',
          deployment_name: 'chat-a',
          request_count: 10,
          success_count: 9,
          error_count: 1,
          tokenized_request_count: 8,
          latency_ms_p95: 120,
          input_tokens_p95: 40,
        },
        {
          group_key: 'dep-2',
          deployment_name: 'chat-b',
          request_count: 5,
          success_count: 5,
          error_count: 0,
          tokenized_request_count: 5,
          latency_ms_p95: 80,
          input_tokens_p95: 20,
        },
      ],
    },
    invocationsError: null,
    fetchedAt: new Date('2026-10-06T01:02:03Z'),
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders successful dashboard KPIs and operations', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(baseSnap())
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('전체 Node')).toBeInTheDocument()
    })
    expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    expect(screen.getByText('24h Requests')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText('Active Operations')).toBeInTheDocument()
    expect(screen.getByText('START')).toBeInTheDocument()
    expect(screen.getByText('STOP')).toBeInTheDocument()
    // newest-first API order preserved (START before STOP in DOM order)
    const rows = screen.getAllByRole('row')
    const text = rows.map((r) => r.textContent || '').join('\n')
    expect(text.indexOf('START')).toBeLessThan(text.indexOf('STOP'))
  })

  it('shows empty states for zero results', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        nodesTotal: 0,
        nodesOnline: 0,
        operationsRecent: [],
        operationsActive: 0,
        invocations: { hours: 24, group_by: 'deployment', items: [] },
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('등록된 Node가 없습니다.')).toBeInTheDocument()
    })
    expect(
      screen.getByText('최근 24시간 호출 기록이 없습니다.'),
    ).toBeInTheDocument()
    expect(screen.getByText('표시할 Operation이 없습니다.')).toBeInTheDocument()
  })

  it('keeps other sections when invocation section fails', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        invocations: null,
        invocationsError: 'summary unavailable',
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('전체 Node')).toBeInTheDocument()
    })
    expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    expect(screen.getByText('summary unavailable')).toBeInTheDocument()
    expect(screen.queryByText('24h Requests')).not.toBeInTheDocument()
  })

  it('does not synthesize a global p95 label', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(baseSnap())
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('chat-a')).toBeInTheDocument()
    })
    expect(screen.queryByText(/global p95/i)).not.toBeInTheDocument()
    expect(screen.getByText('120 ms')).toBeInTheDocument()
  })

  it('shows loading then resolved content', async () => {
    let resolve!: (v: DashboardSnapshot) => void
    const pending = new Promise<DashboardSnapshot>((r) => {
      resolve = r
    })
    vi.spyOn(dashboardApi, 'fetchDashboard').mockReturnValue(pending)
    renderPage()
    expect(screen.getAllByText('불러오는 중').length).toBeGreaterThan(0)
    resolve(baseSnap())
    await waitFor(() => {
      expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    })
  })

  it('refresh triggers another fetch', async () => {
    const spy = vi
      .spyOn(dashboardApi, 'fetchDashboard')
      .mockResolvedValue(baseSnap())
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('renders unknown status safely', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        operationsRecent: [
          {
            id: 'op-x',
            operation_type: 'START',
            status: 'WEIRD_NEW_STATUS',
            switch_strategy: null,
            endpoint_alias_id: null,
            source_deployment_id: null,
            target_deployment_id: null,
            requested_by: null,
            request_reason: null,
            error_code: null,
            error_message: null,
            cancel_requested_at: null,
            retry_of_operation_id: null,
            created_at: '2026-10-06T00:00:00Z',
            started_at: null,
            finished_at: null,
          },
        ],
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getAllByText(/WEIRD_NEW_STATUS/).length).toBeGreaterThan(0)
    })
  })

  it('shows fatal unavailable on initial total outage', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockRejectedValue(
      new dashboardApi.DashboardUnavailableError(),
    )
    renderPage()
    await waitFor(() => {
      expect(
        screen.getByText('Management API를 사용할 수 없습니다'),
      ).toBeInTheDocument()
    })
    expect(screen.getByText('마지막 갱신 —')).toBeInTheDocument()
    // No successful KPI values (unknown stays "—", not numeric totals).
    expect(screen.queryByText('15')).not.toBeInTheDocument()
  })

  it('preserves prior data and timestamp when refresh totally fails', async () => {
    const first = baseSnap({
      fetchedAt: new Date('2026-10-06T01:02:03Z'),
    })
    const spy = vi
      .spyOn(dashboardApi, 'fetchDashboard')
      .mockResolvedValueOnce(first)
      .mockRejectedValueOnce(new dashboardApi.DashboardUnavailableError())
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    })
    const stampBefore = screen.getByText(/마지막 갱신/).textContent
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText('최근 새로고침 실패')).toBeInTheDocument()
    })
    expect(spy).toHaveBeenCalledTimes(2)
    expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText(/마지막 갱신/).textContent).toBe(stampBefore)
  })

  it('keeps partial success as a successful refresh', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        invocations: null,
        invocationsError: 'summary unavailable',
        fetchedAt: new Date('2026-10-06T04:05:06Z'),
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('summary unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText('ONLINE Node')).toBeInTheDocument()
    expect(screen.queryByText('최근 새로고침 실패')).not.toBeInTheDocument()
    expect(screen.getByText(/마지막 갱신/)).not.toHaveTextContent('—')
  })
})
