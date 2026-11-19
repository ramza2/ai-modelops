import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchDashboard } from '../api/dashboard'

afterEach(() => vi.unstubAllGlobals())

const makeNode = (id: number) => ({
  id: `node-${id}`,
  name: `Node ${id}`,
  status: 'ONLINE',
})

function respond(json: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(json),
  } as Response
}

/** No real API, Node Agent, or model calls. */
function mockDashboardApi(nodeCount: number, failingNode = -1) {
  const nodes = Array.from({ length: nodeCount }, (_, i) => makeNode(i))
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/resources/refresh') || init?.method === 'POST') {
      throw Error('Dashboard must not mutate GPU resources')
    }
    if (url === '/health') return respond({ status: 'ok' })
    if (url === '/ready') return respond({ status: 'READY' })
    if (url.startsWith('/api/v1/nodes/') && url.endsWith('/resources/latest')) {
      const match = url.match(/node-(\d+)/)
      const n = match ? Number(match[1]) : -1
      return n === failingNode
        ? respond({ error: { message: 'snapshot unavailable' } }, 503)
        : respond({
            node_id: `node-${n}`, source: 'persisted_snapshot',
            is_live: false, host: null, gpus: [],
          })
    }
    if (url.startsWith('/api/v1/nodes?')) {
      if (url.includes('status=ONLINE')) {
        return respond({ total: nodeCount, items: nodes.slice(0, 1) })
      }
      const size = Number(new URL(url, 'http://mock.invalid').searchParams.get('page_size'))
      return respond({ total: nodeCount, items: nodes.slice(0, size) })
    }
    if (url.startsWith('/api/v1/observability/invocations/summary')) {
      return respond({ group_by: 'deployment', hours: 24, items: [] })
    }
    return respond({ total: 0, items: [] })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('fetchDashboard persisted GPU summary', () => {
  it('adds read-only latest snapshot without changing KPI count or forcing collection', async () => {
    const requests = mockDashboardApi(1)
    const snap = await fetchDashboard()
    expect(snap.nodesTotal).toBe(1)
    expect(snap.nodesOnline).toBe(1)
    expect(snap.gpuNodes).toHaveLength(1)
    expect(snap.gpuNodes?.[0]).toMatchObject({
      nodeId: 'node-0',
      error: null,
      resources: { source: 'persisted_snapshot', is_live: false },
    })
    const urls = requests.mock.calls.map(([url]) => String(url))
    expect(urls.filter((u) => u.endsWith('/resources/latest'))).toHaveLength(1)
    expect(urls.some((u) => u.includes('/resources/refresh'))).toBe(false)
    expect(requests.mock.calls.every(([, opts]) => opts?.method === 'GET')).toBe(true)
  })

  it('bounds resource queries at five and isolates one failed server', async () => {
    const requests = mockDashboardApi(7, 1)
    const snap = await fetchDashboard()
    expect(snap.nodesTotal).toBe(7)
    expect(snap.gpuNodes).toHaveLength(5)
    expect(snap.gpuTruncated).toBe(true)
    expect(snap.gpuNodes?.[1]).toMatchObject({
      nodeId: 'node-1', resources: null, error: 'snapshot unavailable',
    })
    expect(snap.gpuNodes?.[0].resources?.source).toBe('persisted_snapshot')
    expect(snap.health?.status).toBe('ok')
    const urls = requests.mock.calls.map(([url]) => String(url))
    expect(urls.filter((u) => u.endsWith('/resources/latest'))).toHaveLength(5)
  })
})
