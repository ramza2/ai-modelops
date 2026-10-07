import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getDeployment,
  listDeployments,
  restartDeployment,
  startDeployment,
  stopDeployment,
} from '../api/deployments'

describe('deployments API client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists deployments with supported filters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await listDeployments({
      deploymentType: 'MANAGED',
      runtimeStatus: 'RUNNING',
      healthStatus: 'HEALTHY',
      retired: false,
      nodeId: '11111111-1111-1111-1111-111111111111',
      page: 2,
      pageSize: 20,
    })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).toContain('/api/v1/deployments')
    expect(url).toContain('deployment_type=MANAGED')
    expect(url).toContain('runtime_status=RUNNING')
    expect(url).toContain('health_status=HEALTHY')
    expect(url).toContain('retired=false')
    expect(url).toContain('node_id=11111111-1111-1111-1111-111111111111')
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=20')
  })

  it('omits unset optional filters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await listDeployments({ page: 1 })
    const url = String(fetchSpy.mock.calls[0]?.[0])
    expect(url).not.toContain('deployment_type=')
    expect(url).not.toContain('retired=')
    expect(url).not.toContain('node_id=')
  })

  it('posts lifecycle enqueue paths', async () => {
    const opBody = {
      id: 'op1',
      operation_type: 'START',
      status: 'QUEUED',
      target_deployment_id: 'd1',
      current_step: 'PREPARE_ARTIFACTS',
      created_at: '2026-10-01T00:00:00Z',
      started_at: null,
      finished_at: null,
      error: null,
    }
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input) => {
        const url = String(input)
        if (url.includes('/start') || url.includes('/stop') || url.includes('/restart')) {
          return new Response(JSON.stringify(opBody), {
            status: 202,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return new Response(
          JSON.stringify({ id: 'd1', name: 'demo' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    await startDeployment('d1')
    await stopDeployment('d1')
    await restartDeployment('d1')
    await getDeployment('d1')
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    const methods = fetchSpy.mock.calls.map((c) => (c[1] as RequestInit).method)
    const idempotencyKeys = fetchSpy.mock.calls.slice(0, 3).map((c) =>
      new Headers((c[1] as RequestInit).headers).get('Idempotency-Key'),
    )
    expect(urls[0]).toContain('/api/v1/deployments/d1/start')
    expect(urls[1]).toContain('/api/v1/deployments/d1/stop')
    expect(urls[2]).toContain('/api/v1/deployments/d1/restart')
    expect(urls[3]).toContain('/api/v1/deployments/d1')
    expect(methods.slice(0, 3)).toEqual(['POST', 'POST', 'POST'])
    expect(methods[3]).toBe('GET')
    expect(idempotencyKeys.every(Boolean)).toBe(true)
    expect(new Set(idempotencyKeys).size).toBe(3)
  })
})
