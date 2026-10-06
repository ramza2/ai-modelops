import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DashboardUnavailableError,
  fetchDashboard,
} from '../api/dashboard'

describe('fetchDashboard outage semantics', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('throws when every Management API request fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('down', { status: 503 })),
    )
    await expect(fetchDashboard()).rejects.toBeInstanceOf(
      DashboardUnavailableError,
    )
  })

  it('returns partial snapshot when some sources succeed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/health')) {
          return new Response(JSON.stringify({ status: 'ok' }), { status: 200 })
        }
        if (url.includes('/ready')) {
          return new Response(JSON.stringify({ status: 'ready' }), {
            status: 200,
          })
        }
        if (url.includes('/nodes')) {
          return new Response(
            JSON.stringify({ items: [], page: 1, page_size: 1, total: 2 }),
            { status: 200 },
          )
        }
        // deployments / endpoints / operations / invocations fail
        return new Response('fail', { status: 500 })
      }),
    )
    const snap = await fetchDashboard()
    expect(snap.nodesTotal).toBe(2)
    expect(snap.health?.status).toBe('ok')
    expect(snap.invocationsError).toBeTruthy()
    expect(snap.fetchedAt).toBeInstanceOf(Date)
  })
})
