import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { OperationSummary, Paginated } from '../api/types'
import { OperationsPage } from '../pages/OperationsPage'
import * as operationsApi from '../api/operations'

function makeOp(
  overrides: Partial<OperationSummary> & Pick<OperationSummary, 'id'>,
): OperationSummary {
  return {
    operation_type: 'SWITCH',
    status: 'RUNNING',
    switch_strategy: 'HOT',
    endpoint_alias_id: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
    source_deployment_id: 'ssssssss-ssss-ssss-ssss-ssssssssssss',
    target_deployment_id: 'tttttttt-tttt-tttt-tttt-tttttttttttt',
    requested_by: 'operator',
    request_reason: null,
    error_code: null,
    error_message: null,
    cancel_requested_at: null,
    retry_of_operation_id: null,
    created_at: '2026-10-07T00:00:00Z',
    started_at: '2026-10-07T00:01:00Z',
    finished_at: null,
    ...overrides,
  }
}

function pageResult(
  items: OperationSummary[],
  opts: { page?: number; total?: number } = {},
): Paginated<OperationSummary> {
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
        <Route path="/operations" element={<OperationsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OperationsPage', () => {
  beforeEach(() => {
    vi.spyOn(operationsApi, 'listOperations')
    vi.spyOn(operationsApi, 'getOperation')
    vi.spyOn(operationsApi, 'cancelOperation')
    vi.spyOn(operationsApi, 'retryOperation')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders list links without row N+1 detail calls', async () => {
    vi.mocked(operationsApi.listOperations).mockResolvedValue(
      pageResult([
        makeOp({ id: 'op-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' }),
        makeOp({
          id: 'op-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
          operation_type: 'START',
          switch_strategy: null,
          endpoint_alias_id: null,
          source_deployment_id: null,
          target_deployment_id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
        }),
      ]),
    )
    renderAt('/operations')
    const link = await screen.findByRole('link', {
      name: /op-aaaaaaaa/,
    })
    expect(link).toHaveAttribute(
      'href',
      '/operations/op-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    )
    expect(
      screen.getByRole('link', { name: /eeeeeeee/ }),
    ).toHaveAttribute('href', '/endpoints/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee')
    expect(
      screen.getByRole('link', { name: /ssssssss/ }),
    ).toHaveAttribute(
      'href',
      '/deployments/ssssssss-ssss-ssss-ssss-ssssssssssss',
    )
    expect(
      screen.getByRole('link', { name: /tttttttt/ }),
    ).toHaveAttribute(
      'href',
      '/deployments/tttttttt-tttt-tttt-tttt-tttttttttttt',
    )
    expect(operationsApi.listOperations).toHaveBeenCalledTimes(1)
    expect(operationsApi.getOperation).not.toHaveBeenCalled()
    expect(operationsApi.cancelOperation).not.toHaveBeenCalled()
    expect(operationsApi.retryOperation).not.toHaveBeenCalled()
  })

  it('applies status/type/active filters with mutual exclusion', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.listOperations).mockResolvedValue(pageResult([]))
    renderAt('/operations')
    await screen.findByLabelText('Status')
    await waitFor(() => {
      expect(operationsApi.listOperations).toHaveBeenCalled()
    })

    await user.selectOptions(screen.getByLabelText('Status'), 'FAILED')
    await waitFor(() => {
      const last = vi.mocked(operationsApi.listOperations).mock.calls.at(-1)?.[0]
      expect(last).toEqual(
        expect.objectContaining({
          status: 'FAILED',
          active: null,
          page: 1,
        }),
      )
    })
    expect(screen.getByTestId('location-search')).toHaveTextContent(
      'status=FAILED',
    )
    expect(screen.getByTestId('location-search').textContent).not.toContain(
      'active=',
    )

    await user.selectOptions(screen.getByLabelText('Active'), 'ACTIVE')
    await waitFor(() => {
      const last = vi.mocked(operationsApi.listOperations).mock.calls.at(-1)?.[0]
      expect(last).toEqual(
        expect.objectContaining({
          status: null,
          active: true,
          page: 1,
        }),
      )
    })
    expect(screen.getByTestId('location-search')).toHaveTextContent(
      'active=true',
    )
    expect(screen.getByTestId('location-search').textContent).not.toContain(
      'status=',
    )

    await user.selectOptions(screen.getByLabelText('Type'), 'SWITCH')
    await waitFor(() => {
      const last = vi.mocked(operationsApi.listOperations).mock.calls.at(-1)?.[0]
      expect(last).toEqual(
        expect.objectContaining({
          operationType: 'SWITCH',
          active: true,
          status: null,
        }),
      )
    })
  })

  it.each([
    ['/operations?status=running', 'status=RUNNING'],
    ['/operations?status=ALL', ''],
    ['/operations?status=NOPE', ''],
    ['/operations?operation_type=switch', 'operation_type=SWITCH'],
    ['/operations?operation_type=ALL', ''],
    ['/operations?active=TRUE', 'active=true'],
    ['/operations?active=yes', ''],
    [
      '/operations?status=RUNNING&active=true&operation_type=SWITCH&page=002',
      'status=RUNNING&operation_type=SWITCH&page=2',
    ],
    [
      '/operations?active=false&operation_type=START',
      'active=false&operation_type=START',
    ],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(operationsApi.listOperations).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('preserves rows on refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(operationsApi.listOperations)
      .mockResolvedValueOnce(
        pageResult([
          makeOp({ id: 'op-keep-aaaa-aaaa-aaaa-aaaaaaaaaaaa' }),
        ]),
      )
      .mockRejectedValueOnce(new ApiError('refresh fail', { status: 503 }))
    renderAt('/operations')
    expect(
      await screen.findByRole('link', { name: /op-keep/ }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh fail')).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /op-keep/ }),
    ).toBeInTheDocument()
  })

  it('clamps out-of-range page when total is zero', async () => {
    vi.mocked(operationsApi.listOperations).mockResolvedValue(
      pageResult([], { page: 2, total: 0 }),
    )
    renderAt('/operations?page=2')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent('')
    })
    expect(
      await screen.findByText('등록된 Operation이 없습니다.'),
    ).toBeInTheDocument()
  })

  it('uses scope=col and avoids forbidden endpoints', async () => {
    vi.mocked(operationsApi.listOperations).mockResolvedValue(
      pageResult([makeOp({ id: 'op-x' })]),
    )
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/operations')
    await screen.findByRole('link', { name: /op-x/ })
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/steps'))).toBe(false)
    expect(urls.some((u) => u.includes('/rollback'))).toBe(false)
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/observability'))).toBe(false)
  })
})
