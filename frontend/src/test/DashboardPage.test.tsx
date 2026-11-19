import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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
    gpuNodes: [],
    gpuTruncated: false,
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
          group_key: '11111111-1111-4111-8111-111111111111',
          deployment_name: 'chat-a',
          request_count: 10,
          success_count: 9,
          error_count: 1,
          tokenized_request_count: 8,
          latency_ms_p95: 120,
          input_tokens_p95: 40,
        },
        {
          group_key: '22222222-2222-4222-8222-222222222222',
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
      expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
    })
    expect(screen.getByText('1 / 2')).toBeInTheDocument()
    expect(screen.getByText('최근 24시간 요청')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText('진행 중인 작업')).toBeInTheDocument()
    expect(screen.getByText(/모델 실행 \(START\)/)).toBeInTheDocument()
    expect(screen.getByText(/모델 중지 \(STOP\)/)).toBeInTheDocument()
    // newest-first API order preserved (START before STOP in DOM order)
    const rows = screen.getAllByRole('row')
    const text = rows.map((r) => r.textContent || '').join('\n')
    expect(text.indexOf('START')).toBeLessThan(text.indexOf('STOP'))
  })

  it('shows Health vs Ready Korean labels without duplicate badge cards', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(baseSnap())
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('관리 시스템 Health')).toBeInTheDocument()
    })
    expect(screen.getByText('관리 시스템 Ready')).toBeInTheDocument()
    expect(screen.getAllByText('정상').length).toBeGreaterThan(0)
    expect(screen.getAllByText('준비됨').length).toBeGreaterThan(0)
    expect(screen.queryByText('Health 배지')).not.toBeInTheDocument()
    expect(screen.queryByText('Ready 배지')).not.toBeInTheDocument()
  })

  it('links summary areas to detail pages and operation rows', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(baseSnap())
    renderPage()
    await waitFor(() => {
      expect(screen.getByRole('link', { name: '서버 및 GPU' })).toHaveAttribute(
        'href',
        '/nodes',
      )
    })
    expect(
      screen.getByRole('link', { name: '모델 배포 관리' }),
    ).toHaveAttribute('href', '/deployments')
    expect(
      screen.getByRole('link', { name: 'API 연결 관리' }),
    ).toHaveAttribute('href', '/endpoints')
    expect(screen.getByRole('link', { name: '운영 모니터링' })).toHaveAttribute(
      'href',
      '/observability',
    )
    expect(screen.getByRole('link', { name: '작업 이력' })).toHaveAttribute(
      'href',
      '/operations',
    )
    expect(screen.getByRole('link', { name: 'chat-a' })).toHaveAttribute(
      'href',
      '/deployments/11111111-1111-4111-8111-111111111111',
    )
    expect(screen.getByRole('link', { name: /op-1/ })).toHaveAttribute(
      'href',
      '/operations/op-1',
    )
  })

  it('lists attention items for section errors and failed operations with links', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        nodesError: 'nodes unavailable',
        operationsRecent: [
          {
            id: 'op-fail',
            operation_type: 'SWITCH',
            status: 'FAILED',
            switch_strategy: 'HOT',
            endpoint_alias_id: 'ep-1',
            source_deployment_id: null,
            target_deployment_id: null,
            requested_by: null,
            request_reason: null,
            error_code: 'SWITCH_FAILED',
            error_message: 'target unhealthy',
            cancel_requested_at: null,
            retry_of_operation_id: null,
            created_at: '2026-10-06T00:00:00Z',
            started_at: '2026-10-06T00:00:01Z',
            finished_at: '2026-10-06T00:01:00Z',
          },
        ],
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('서버 목록 조회 실패')).toBeInTheDocument()
    })
    expect(screen.getAllByText('nodes unavailable').length).toBeGreaterThan(0)
    expect(
      screen.getByRole('link', { name: '서버 및 GPU 보기' }),
    ).toHaveAttribute('href', '/nodes')
    expect(screen.getByText(/최근 실패 작업/)).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: '작업 상세 보기' }),
    ).toHaveAttribute('href', '/operations/op-fail')
    expect(screen.getByText(/최근 24시간 오류 호출/)).toBeInTheDocument()
  })

  it('distinguishes zero counts from missing and unavailable', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        nodesTotal: 0,
        nodesOnline: 0,
        nodesError: null,
        deploymentsActive: null as unknown as number,
        deploymentsRunning: null as unknown as number,
        deploymentsHealthy: null as unknown as number,
        deploymentsError: null,
        endpointsTotal: null as unknown as number,
        endpointsEnabled: null as unknown as number,
        endpointsServing: null as unknown as number,
        endpointsError: 'endpoints unavailable',
        operationsActive: 0,
        operationsRecent: [],
        invocations: { hours: 24, group_by: 'deployment', items: [] },
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('등록된 Node가 없습니다.')).toBeInTheDocument()
    })
    expect(screen.getByText('서버 ONLINE / 전체').closest('article')?.textContent).toMatch(
      /0 \/ 0/,
    )
    expect(screen.getAllByText('미수집').length).toBeGreaterThan(0)
    expect(screen.getAllByText('확인 불가').length).toBeGreaterThan(0)
    expect(
      screen.getByText('최근 24시간 호출 기록이 없습니다.'),
    ).toBeInTheDocument()
    expect(screen.getByText('표시할 Operation이 없습니다.')).toBeInTheDocument()
    const serverCard = screen.getByRole('heading', { name: '서버 (Node)' }).closest(
      'article',
    )
    expect(serverCard).toBeTruthy()
    expect(within(serverCard as HTMLElement).getAllByText('0').length).toBeGreaterThan(
      0,
    )
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
      expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
    })
    expect(screen.getByText('1 / 2')).toBeInTheDocument()
    expect(screen.getAllByText('summary unavailable').length).toBeGreaterThan(0)
    expect(screen.queryByText('최근 24시간 요청')).not.toBeInTheDocument()
  })

  it('does not synthesize a global p95 label', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(baseSnap())
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('chat-a')).toBeInTheDocument()
    })
    expect(screen.queryByText(/global p95/i)).not.toBeInTheDocument()
    expect(screen.getByText('120 ms')).toBeInTheDocument()
    expect(
      screen.getByText('응답시간 상위 5% 기준(P95)'),
    ).toBeInTheDocument()
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
      expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
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
    // Must not claim "no attention items" when nothing could be checked.
    expect(
      screen.queryByText(/확인된 주의 항목이 없습니다/),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('운영 요약')).not.toBeInTheDocument()
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
      expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
    })
    const stampBefore = screen.getByText(/마지막 갱신/).textContent
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText('최근 새로고침 실패')).toBeInTheDocument()
    })
    expect(spy).toHaveBeenCalledTimes(2)
    expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText(/마지막 갱신/).textContent).toBe(stampBefore)
    const stale = screen.getByTestId('dashboard-stale-banner')
    expect(stale).toHaveTextContent(/이전 수집값/)
    expect(stale).toHaveTextContent(/마지막 성공 수집/)
  })

  it('links deployment rows only when group_key is a deployment UUID', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        invocations: {
          hours: 24,
          group_by: 'deployment',
          items: [
            {
              group_key: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
              deployment_name: 'linked-dep',
              request_count: 3,
              success_count: 3,
              error_count: 0,
              tokenized_request_count: 3,
              latency_ms_p95: 10,
              input_tokens_p95: 1,
            },
            {
              group_key: 'unknown',
              deployment_name: 'orphan-bucket',
              request_count: 2,
              success_count: 1,
              error_count: 1,
              tokenized_request_count: 1,
              latency_ms_p95: null,
              input_tokens_p95: null,
            },
          ],
        },
      }),
    )
    renderPage()
    expect(await screen.findByRole('link', { name: 'linked-dep' })).toHaveAttribute(
      'href',
      '/deployments/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
    )
    expect(screen.getByText('orphan-bucket')).toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: 'orphan-bucket' }),
    ).not.toBeInTheDocument()
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
      expect(screen.getAllByText('summary unavailable').length).toBeGreaterThan(0)
    })
    expect(screen.getByText('서버 ONLINE / 전체')).toBeInTheDocument()
    expect(screen.queryByText('최근 새로고침 실패')).not.toBeInTheDocument()
    expect(screen.getByText(/마지막 갱신/)).not.toHaveTextContent('—')
  })

  it('does not claim all-clear when attention list is empty', async () => {
    vi.spyOn(dashboardApi, 'fetchDashboard').mockResolvedValue(
      baseSnap({
        invocations: { hours: 24, group_by: 'deployment', items: [] },
      }),
    )
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/확인된 주의 항목이 없습니다/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/정상 확인/)).not.toBeInTheDocument()
  })
})
