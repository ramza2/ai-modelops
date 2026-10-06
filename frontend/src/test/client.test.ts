import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  __testBuildUrl,
  apiGet,
  apiPost,
  buildApiUrl,
} from '../api/client'

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('builds same-origin relative URL with no base', () => {
    expect(
      buildApiUrl('', '/api/v1/nodes', {
        status: 'ONLINE',
        page: 1,
        page_size: 1,
        unused: null,
      }),
    ).toBe('/api/v1/nodes?status=ONLINE&page=1&page_size=1')
    expect(__testBuildUrl('/api/v1/nodes', { page: 1 })).toBe(
      '/api/v1/nodes?page=1',
    )
  })

  it('builds absolute URL from absolute base', () => {
    expect(
      buildApiUrl('https://example.test', '/api/v1/nodes', { page: 1 }),
    ).toBe('https://example.test/api/v1/nodes?page=1')
  })

  it('strips trailing slash on absolute base (no //api)', () => {
    expect(
      buildApiUrl('https://example.test/', '/api/v1/nodes', { page: 2 }),
    ).toBe('https://example.test/api/v1/nodes?page=2')
  })

  it('supports relative API prefix', () => {
    expect(buildApiUrl('/admin-api', '/api/v1/nodes')).toBe(
      '/admin-api/api/v1/nodes',
    )
    expect(buildApiUrl('/admin-api/', 'api/v1/nodes')).toBe(
      '/admin-api/api/v1/nodes',
    )
  })

  it('returns JSON on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    const body = await apiGet<{ ok: boolean }>('/health')
    expect(body.ok).toBe(true)
  })

  it('parses ModelOps error envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            error: {
              message: 'Invalid status.',
              type: 'modelops_error',
              code: 'VALIDATION_ERROR',
            },
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    await expect(apiGet('/api/v1/operations')).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      code: 'VALIDATION_ERROR',
      message: 'Invalid status.',
    } satisfies Partial<ApiError>)
  })

  it('falls back for non-JSON errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('plain failure', { status: 502 })),
    )
    await expect(apiGet('/ready')).rejects.toMatchObject({
      status: 502,
      message: 'plain failure',
    })
  })

  it('propagates AbortSignal', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.signal).toBeDefined()
      return new Response('{}', { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const ac = new AbortController()
    await apiGet('/health', { signal: ac.signal })
    expect(fetchMock).toHaveBeenCalled()
  })

  it('POSTs without body and omits Content-Type', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.method).toBe('POST')
      expect(init?.body).toBeUndefined()
      const headers = new Headers(init?.headers)
      expect(headers.get('Content-Type')).toBeNull()
      expect(headers.get('Accept')).toBe('application/json')
      return new Response(
        JSON.stringify({ node_id: 'n1', host: null, gpus: [] }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const body = await apiPost<{ node_id: string }>(
      '/api/v1/nodes/n1/resources/refresh',
    )
    expect(body.node_id).toBe('n1')
  })

  it('POSTs JSON body with Content-Type', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.method).toBe('POST')
      expect(init?.body).toBe(JSON.stringify({ name: 'x' }))
      const headers = new Headers(init?.headers)
      expect(headers.get('Content-Type')).toBe('application/json')
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    await apiPost('/api/v1/example', { body: { name: 'x' } })
    expect(fetchMock).toHaveBeenCalled()
  })

  it('parses ModelOps error envelope on POST', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            error: {
              message: 'Node Agent unreachable.',
              type: 'modelops_error',
              code: 'UPSTREAM_UNAVAILABLE',
            },
          }),
          { status: 502, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    await expect(
      apiPost('/api/v1/nodes/n1/resources/refresh'),
    ).rejects.toMatchObject({
      name: 'ApiError',
      status: 502,
      code: 'UPSTREAM_UNAVAILABLE',
      message: 'Node Agent unreachable.',
    } satisfies Partial<ApiError>)
  })

  it('propagates AbortSignal on POST', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.signal).toBeDefined()
      return new Response('{}', { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const ac = new AbortController()
    await apiPost('/api/v1/nodes/n1/resources/refresh', {
      signal: ac.signal,
    })
    expect(fetchMock).toHaveBeenCalled()
  })

  it('POSTs against absolute API base', async () => {
    expect(
      buildApiUrl(
        'https://api.example.test',
        '/api/v1/nodes/n1/resources/refresh',
      ),
    ).toBe('https://api.example.test/api/v1/nodes/n1/resources/refresh')
  })
})
