import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createClient,
  getClient,
  getClientRuntimePolicy,
  listClients,
  updateClient,
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

  it('loads client detail and runtime-policy via GET', async () => {
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
  })

  it('POSTs create with caller Idempotency-Key and preserves case', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'c-new',
          client_key: 'AlZi',
          display_name: '사내 지식 AI 검색',
          description: 'RAG',
          is_active: true,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    const result = await createClient(
      {
        client_key: 'AlZi',
        display_name: '사내 지식 AI 검색',
        description: 'RAG',
      },
      { idempotencyKey: 'idem-create-1' },
    )
    expect(result.client_key).toBe('AlZi')
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe(
      'idem-create-1',
    )
    expect(JSON.parse(String(init.body))).toEqual({
      client_key: 'AlZi',
      display_name: '사내 지식 AI 검색',
      description: 'RAG',
    })
  })

  it('PATCHes only provided fields and sends null description', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'c1',
          client_key: 'alzi',
          display_name: 'Renamed',
          description: null,
          is_active: false,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-08T00:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await updateClient(
      'c1',
      { display_name: 'Renamed', description: null, is_active: false },
      { idempotencyKey: 'idem-patch-1' },
    )
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit
    expect(init.method).toBe('PATCH')
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe(
      'idem-patch-1',
    )
    expect(JSON.parse(String(init.body))).toEqual({
      display_name: 'Renamed',
      description: null,
      is_active: false,
    })
  })
})
