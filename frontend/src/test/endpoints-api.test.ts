import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createPreflightPreview,
  getEndpoint,
  listEndpointRoutes,
  listEndpoints,
  switchEndpoint,
} from '../api/endpoints'

describe('endpoints API client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists endpoints with supported filters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await listEndpoints({
      q: 'chat',
      apiType: 'CHAT',
      isEnabled: true,
      trafficState: 'SERVING',
      page: 2,
      pageSize: 20,
    })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).toContain('/api/v1/endpoints')
    expect(url).toContain('q=chat')
    expect(url).toContain('api_type=CHAT')
    expect(url).toContain('is_enabled=true')
    expect(url).toContain('traffic_state=SERVING')
    expect(url).toContain('page=2')
  })

  it('loads detail and routes', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/routes')) {
          return new Response(JSON.stringify({ items: [], total: 0 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return new Response(
          JSON.stringify({ id: 'e1', alias: 'chat', active_route: null }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    await getEndpoint('e1')
    await listEndpointRoutes('e1')
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls[0]).toContain('/api/v1/endpoints/e1')
    expect(urls[1]).toContain('/api/v1/endpoints/e1/routes')
  })

  it('posts preflight and switch with unique Idempotency-Key', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/preflights')) {
          return new Response(
            JSON.stringify({
              id: 'pf1',
              result: 'HOT_SWITCH_AVAILABLE',
              gpu_results: [],
              preview_only: true,
              worker_must_revalidate: true,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(
          JSON.stringify({
            id: 'op1',
            operation_id: 'op1',
            operation_type: 'SWITCH',
            switch_strategy: 'HOT',
            status: 'QUEUED',
            current_step: 'VALIDATE',
            created_at: '2026-10-07T00:00:00Z',
            error: null,
          }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    await createPreflightPreview('e1', 'd2')
    await switchEndpoint('e1', {
      targetDeploymentId: 'd2',
      strategy: 'HOT',
      reason: 'test',
    })
    await switchEndpoint('e1', {
      targetDeploymentId: 'd2',
      strategy: 'COLD',
    })
    const preflightCall = fetchSpy.mock.calls[0]
    expect(String(preflightCall?.[0])).toContain('/api/v1/preflights')
    expect((preflightCall?.[1] as RequestInit).method).toBe('POST')
    const switchCalls = fetchSpy.mock.calls.slice(1)
    expect(switchCalls).toHaveLength(2)
    for (const call of switchCalls) {
      expect(String(call[0])).toContain('/api/v1/endpoints/e1/switch')
      expect((call[1] as RequestInit).method).toBe('POST')
    }
    const keys = switchCalls.map((c) =>
      new Headers((c[1] as RequestInit).headers).get('Idempotency-Key'),
    )
    expect(keys.every(Boolean)).toBe(true)
    expect(new Set(keys).size).toBe(2)
    const body = JSON.parse(String((switchCalls[0]?.[1] as RequestInit).body))
    expect(body.strategy).toBe('HOT')
    expect(body.target_deployment_id).toBe('d2')
  })
})
