import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, __testBuildUrl, apiGet } from '../api/client'

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('encodes query parameters', () => {
    const url = __testBuildUrl('/api/v1/nodes', {
      status: 'ONLINE',
      page: 1,
      page_size: 1,
      unused: null,
    })
    expect(url).toBe('/api/v1/nodes?status=ONLINE&page=1&page_size=1')
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
})
