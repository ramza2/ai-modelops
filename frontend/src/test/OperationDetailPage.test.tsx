import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  OperationDetail,
  OperationStep,
  RetryOperationResponse,
} from '../api/types'
import { OperationDetailPage } from '../pages/OperationDetailPage'
import * as operationsApi from '../api/operations'

function makeStep(
  overrides: Partial<OperationStep> & Pick<OperationStep, 'id' | 'sequence_no'>,
): OperationStep {
  return {
    step_code: 'VALIDATE',
    status: 'SUCCEEDED',
    attempt_no: 1,
    started_at: '2026-10-07T00:00:00Z',
    finished_at: '2026-10-07T00:00:01Z',
    error: null,
    created_at: '2026-10-07T00:00:00Z',
    ...overrides,
  }
}

function makeOp(overrides: Partial<OperationDetail> = {}): OperationDetail {
  return {
    id: 'op-1',
    operation_type: 'SWITCH',
    status: 'RUNNING',
    switch_strategy: 'HOT',
    endpoint_alias_id: 'ep-1',
    source_deployment_id: 'src-1',
    target_deployment_id: 'tgt-1',
    current_step: 'DRAIN_TRAFFIC',
    cancel_requested_at: null,
    retry_of_operation_id: 'parent-op',
    requested_by: 'operator',
    request_reason: 'demo',
    created_at: '2026-10-07T00:00:00Z',
    started_at: '2026-10-07T00:01:00Z',
    finished_at: null,
    error: null,
    steps: [
      makeStep({ id: 's1', sequence_no: 1, step_code: 'VALIDATE' }),
      makeStep({
        id: 's2',
        sequence_no: 2,
        step_code: 'DRAIN_TRAFFIC',
        status: 'RUNNING',
        finished_at: null,
      }),
    ],
    ...overrides,
  }
}

function makeRetry(
  overrides: Partial<RetryOperationResponse> = {},
): RetryOperationResponse {
  return {
    id: 'op-retry-child',
    operation_type: 'SWITCH',
    status: 'QUEUED',
    switch_strategy: 'HOT',
    endpoint_alias_id: 'ep-1',
    source_deployment_id: 'src-1',
    target_deployment_id: 'tgt-1',
    retry_of_operation_id: 'op-1',
    current_step: 'VALIDATE',
    created_at: '2026-10-07T01:00:00Z',
    error: null,
    ...overrides,
  }
}

function renderDetail(path = '/operations/op-1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/operations/:operationId"
          element={<OperationDetailPage />}
        />
        <Route path="/operations" element={<div>Operations list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OperationDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(operationsApi, 'getOperation')
    vi.spyOn(operationsApi, 'listOperations')
    vi.spyOn(operationsApi, 'cancelOperation')
    vi.spyOn(operationsApi, 'retryOperation')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders identity, related links, steps, and does not call /steps', async () => {
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'op-1' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /ep-1/ })).toHaveAttribute(
      'href',
      '/endpoints/ep-1',
    )
    expect(screen.getByRole('link', { name: /src-1/ })).toHaveAttribute(
      'href',
      '/deployments/src-1',
    )
    expect(screen.getByRole('link', { name: /tgt-1/ })).toHaveAttribute(
      'href',
      '/deployments/tgt-1',
    )
    expect(screen.getByRole('link', { name: /parent-op/ })).toHaveAttribute(
      'href',
      '/operations/parent-op',
    )
    const stepsTable = screen.getByRole('table')
    expect(within(stepsTable).getByText('VALIDATE')).toBeInTheDocument()
    expect(within(stepsTable).getByText('DRAIN_TRAFFIC')).toBeInTheDocument()
    expect(operationsApi.getOperation).toHaveBeenCalledTimes(1)
    expect(operationsApi.listOperations).not.toHaveBeenCalled()
    expect(document.querySelector('pre')).toBeNull()
    expect(screen.queryByText(/secret_payload/)).not.toBeInTheDocument()
  })

  it('shows Safe Cancel only for HOT/COLD SWITCH in cancelable statuses', async () => {
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())
    renderDetail()
    expect(
      await screen.findByRole('button', { name: 'Safe Cancel' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Explicit Retry' }),
    ).not.toBeInTheDocument()
    cleanup()

    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOp({ status: 'SUCCEEDED', steps: [] }),
    )
    renderDetail()
    expect(
      await screen.findByText(/Cancel\/Retry action을 사용할 수 없습니다/),
    ).toBeInTheDocument()
    cleanup()

    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOp({
        operation_type: 'START',
        switch_strategy: null,
        status: 'RUNNING',
        steps: [],
      }),
    )
    renderDetail()
    expect(
      await screen.findByText(/Cancel\/Retry action을 사용할 수 없습니다/),
    ).toBeInTheDocument()
  })

  it('shows Retry gate for FAILED/ROLLED_BACK SWITCH', async () => {
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOp({
        status: 'FAILED',
        switch_strategy: 'COLD',
        finished_at: '2026-10-07T02:00:00Z',
        error: { code: 'TARGET_START_FAILED', message: 'boom' },
      }),
    )
    renderDetail()
    expect(
      await screen.findByRole('button', { name: 'Explicit Retry' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Safe Cancel' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText(/TARGET_START_FAILED/)).toBeInTheDocument()
  })

  it('cancels with optional reason and re-reads Operation', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation)
      .mockResolvedValueOnce(makeOp())
      .mockResolvedValueOnce(
        makeOp({
          status: 'CANCELLED',
          cancel_requested_at: '2026-10-07T00:05:00Z',
          finished_at: '2026-10-07T00:05:00Z',
          current_step: null,
        }),
      )
    vi.mocked(operationsApi.cancelOperation).mockResolvedValue(
      makeOp({ status: 'CANCELLED' }),
    )
    renderDetail()
    await screen.findByRole('button', { name: 'Safe Cancel' })
    await user.type(screen.getByLabelText(/Cancel Reason/), 'abort now')
    await user.click(screen.getByRole('button', { name: 'Safe Cancel' }))
    await waitFor(() => {
      expect(operationsApi.cancelOperation).toHaveBeenCalledWith(
        'op-1',
        'abort now',
      )
    })
    await waitFor(() => {
      expect(operationsApi.getOperation).toHaveBeenCalledTimes(2)
    })
    expect(await screen.findByText(/CANCELLED/)).toBeInTheDocument()
  })

  it('keeps stale Operation/Steps on cancel 409', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())
    vi.mocked(operationsApi.cancelOperation).mockRejectedValue(
      new ApiError('not cancelable', { status: 409 }),
    )
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'op-1' })
    await user.click(screen.getByRole('button', { name: 'Safe Cancel' }))
    expect(await screen.findByText(/not cancelable/)).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'op-1' }),
    ).toBeInTheDocument()
    expect(screen.getByText('VALIDATE')).toBeInTheDocument()
  })

  it('retries, links new Operation, and re-reads original', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation)
      .mockResolvedValueOnce(
        makeOp({
          status: 'ROLLED_BACK',
          finished_at: '2026-10-07T02:00:00Z',
        }),
      )
      .mockResolvedValueOnce(
        makeOp({
          status: 'ROLLED_BACK',
          finished_at: '2026-10-07T02:00:00Z',
        }),
      )
    vi.mocked(operationsApi.retryOperation).mockResolvedValue(makeRetry())
    renderDetail()
    await screen.findByRole('button', { name: 'Explicit Retry' })
    await user.click(screen.getByRole('button', { name: 'Explicit Retry' }))
    await waitFor(() => {
      expect(operationsApi.retryOperation).toHaveBeenCalledWith('op-1')
    })
    await waitFor(() => {
      expect(operationsApi.getOperation).toHaveBeenCalledTimes(2)
    })
    const childLink = await screen.findByRole('link', {
      name: 'op-retry-child',
    })
    expect(childLink).toHaveAttribute('href', '/operations/op-retry-child')
  })

  it('keeps stale Operation/Steps on retry 422', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOp({ status: 'FAILED', switch_strategy: 'HOT' }),
    )
    vi.mocked(operationsApi.retryOperation).mockRejectedValue(
      new ApiError('not eligible', { status: 422 }),
    )
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'op-1' })
    await user.click(screen.getByRole('button', { name: 'Explicit Retry' }))
    expect(await screen.findByText(/not eligible/)).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'op-1' }),
    ).toBeInTheDocument()
    expect(
      within(screen.getByRole('table')).getByText('DRAIN_TRAFFIC'),
    ).toBeInTheDocument()
  })

  it('mutually excludes refresh and mutation buttons while busy', async () => {
    const user = userEvent.setup()
    let resolveCancel!: (value: OperationDetail) => void
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())
    vi.mocked(operationsApi.cancelOperation).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCancel = resolve
        }),
    )
    renderDetail()
    await screen.findByRole('button', { name: 'Safe Cancel' })
    await user.click(screen.getByRole('button', { name: 'Safe Cancel' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Cancel 중…' })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()
    expect(screen.getByLabelText(/Cancel Reason/)).toBeDisabled()
    resolveCancel(makeOp({ status: 'CANCELLED' }))
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Cancel 중…' }),
      ).not.toBeInTheDocument()
    })
  })

  it('clears stale detail on authoritative 404 refresh', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation)
      .mockResolvedValueOnce(makeOp())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'op-1' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('Operation을 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'op-1' }),
    ).not.toBeInTheDocument()
  })

  it('keeps stale Operation on non-404 refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.getOperation)
      .mockResolvedValueOnce(makeOp())
      .mockRejectedValueOnce(new ApiError('boom', { status: 500 }))
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'op-1' })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(
        screen.getByText(/기존 Operation 정보를 표시하고 있습니다/),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('heading', { level: 2, name: 'op-1' }),
    ).toBeInTheDocument()
    expect(screen.getByText('VALIDATE')).toBeInTheDocument()
  })

  it('does not call forbidden APIs', async () => {
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'op-1' })
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/steps'))).toBe(false)
    expect(urls.some((u) => u.includes('/rollback'))).toBe(false)
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/observability'))).toBe(false)
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
  })
})
