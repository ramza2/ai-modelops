import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getCapacityProfile,
  getInvocationSummary,
  getRuntimeHistory,
  getRuntimeLatest,
} from '../api/observability'

describe('observability API client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loads invocation summary with hours and group_by', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ hours: 24, group_by: 'alias', items: [] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await getInvocationSummary({ hours: 48, groupBy: 'alias' })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).toContain('/api/v1/observability/invocations/summary')
    expect(url).toContain('hours=48')
    expect(url).toContain('group_by=alias')
  })

  it('loads runtime latest with optional deployment_id', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )
    await getRuntimeLatest()
    await getRuntimeLatest('dep-1')
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(
      '/api/v1/observability/runtime/latest',
    )
    expect(String(fetchSpy.mock.calls[0]?.[0])).not.toContain('deployment_id=')
    expect(String(fetchSpy.mock.calls[1]?.[0])).toContain(
      'deployment_id=dep-1',
    )
  })

  it('loads history and capacity-profile without analytics', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/history')) {
          return new Response(
            JSON.stringify({
              deployment_id: 'dep-1',
              hours: 24,
              limit: 100,
              ordering: 'oldest_to_newest',
              items: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(
          JSON.stringify({
            hours: 24,
            deployment: { id: 'dep-1', name: 'd' },
            model: {},
            gpu_count: 0,
            gpu_assignments: [],
            configuration: { settings: {} },
            invocations: { request_count: 0 },
            runtime_analytics: {
              deployment_id: 'dep-1',
              snapshot_count: 0,
              interval_count: 0,
              boundaries: {
                reset_boundary_count: 0,
                identity_unknown_interval_count: 0,
              },
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    await getRuntimeHistory('dep-1', { hours: 12, limit: 100 })
    await getCapacityProfile('dep-1', 12)
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls[0]).toContain(
      '/api/v1/observability/runtime/deployments/dep-1/history',
    )
    expect(urls[0]).toContain('hours=12')
    expect(urls[0]).toContain('limit=100')
    expect(urls[1]).toContain(
      '/api/v1/observability/runtime/deployments/dep-1/capacity-profile',
    )
    expect(urls.some((u) => u.includes('/analytics'))).toBe(false)
  })
})
