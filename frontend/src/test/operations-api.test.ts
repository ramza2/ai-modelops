import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelOperation,
  getOperation,
  listOperations,
  retryOperation,
} from '../api/operations'

describe('operations API client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists operations with status/type/active filters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )
    await listOperations({
      status: 'RUNNING',
      operationType: 'SWITCH',
      page: 2,
      pageSize: 20,
    })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).toContain('/api/v1/operations')
    expect(url).toContain('status=RUNNING')
    expect(url).toContain('operation_type=SWITCH')
    expect(url).toContain('page=2')
    expect(url).not.toContain('active=')

    await listOperations({ active: true, page: 1 })
    const activeUrl = String(fetchSpy.mock.calls[1]?.[0])
    expect(activeUrl).toContain('active=true')
    expect(activeUrl).not.toContain('status=')
  })

  it('loads detail only from GET /operations/{id}', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'op1',
          operation_type: 'SWITCH',
          status: 'RUNNING',
          switch_strategy: 'HOT',
          steps: [],
          error: null,
          created_at: '2026-10-07T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await getOperation('op1')
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(
      '/api/v1/operations/op1',
    )
    expect(String(fetchSpy.mock.calls[0]?.[0])).not.toContain('/steps')
  })

  it('cancels without Idempotency-Key and retries with unique keys', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/cancel')) {
          return new Response(
            JSON.stringify({
              id: 'op1',
              status: 'CANCELLED',
              steps: [],
              error: null,
            }),
            { status: 202, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(
          JSON.stringify({
            id: 'op-retry',
            operation_type: 'SWITCH',
            status: 'QUEUED',
            switch_strategy: 'HOT',
            retry_of_operation_id: 'op1',
            created_at: '2026-10-07T00:00:00Z',
            error: null,
          }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )

    await cancelOperation('op1', 'operator abort')
    await cancelOperation('op1')
    await retryOperation('op1')
    await retryOperation('op1')

    const cancelCalls = fetchSpy.mock.calls.filter((c) =>
      String(c[0]).includes('/cancel'),
    )
    expect(cancelCalls).toHaveLength(2)
    for (const call of cancelCalls) {
      const headers = new Headers((call[1] as RequestInit).headers)
      expect(headers.get('Idempotency-Key')).toBeNull()
      expect((call[1] as RequestInit).method).toBe('POST')
    }
    const cancelBody = JSON.parse(
      String((cancelCalls[0]?.[1] as RequestInit).body),
    )
    expect(cancelBody).toEqual({ reason: 'operator abort' })
    const emptyCancelBody = JSON.parse(
      String((cancelCalls[1]?.[1] as RequestInit).body),
    )
    expect(emptyCancelBody).toEqual({})

    const retryCalls = fetchSpy.mock.calls.filter((c) =>
      String(c[0]).includes('/retry'),
    )
    expect(retryCalls).toHaveLength(2)
    const keys = retryCalls.map((c) =>
      new Headers((c[1] as RequestInit).headers).get('Idempotency-Key'),
    )
    expect(keys.every(Boolean)).toBe(true)
    expect(new Set(keys).size).toBe(2)
  })
})
