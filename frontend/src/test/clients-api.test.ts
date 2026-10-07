import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getClient,
  getClientRuntimePolicy,
  listClients,
} from '../api/clients'

describe('clients API client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists clients with q/is_active/pagination', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await listClients({ q: 'alzi', isActive: true, page: 2, pageSize: 20 })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).toContain('/api/v1/clients')
    expect(url).toContain('q=alzi')
    expect(url).toContain('is_active=true')
    expect(url).toContain('page=2')
    expect((fetchSpy.mock.calls[0]?.[1] as RequestInit).method).toBe('GET')
  })

  it('loads client detail and runtime-policy via GET only', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/runtime-policy')) {
          return new Response(
            JSON.stringify({
              client_id: 'c1',
              client_key: 'alzi',
              policy: null,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(
          JSON.stringify({
            id: 'c1',
            client_key: 'alzi',
            display_name: 'ALZI',
            description: null,
            is_active: true,
            created_at: '2026-10-01T00:00:00Z',
            updated_at: '2026-10-01T00:00:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    await getClient('c1')
    await getClientRuntimePolicy('c1')
    const methods = fetchSpy.mock.calls.map(
      (c) => (c[1] as RequestInit).method || 'GET',
    )
    expect(methods.every((m) => m === 'GET')).toBe(true)
    expect(
      fetchSpy.mock.calls.every(
        (c) => !['POST', 'PATCH', 'PUT', 'DELETE'].includes(
          String((c[1] as RequestInit).method || 'GET'),
        ),
      ),
    ).toBe(true)
  })
})
