import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getHfDownload,
  listModelCaches,
  purgeModelCache,
  startHfDownload,
} from '../api/downloads'

describe('downloads api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('starts HF download with snake_case body', async () => {
    const payload = { job_id: 'j1', status: 'QUEUED', repository_id: 'org/m' }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      text: async () => JSON.stringify(payload),
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)
    await startHfDownload({
      repositoryId: 'org/m',
      revision: 'main',
      nodeId: 'n1',
      modelType: 'LLM',
    })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      repository_id: 'org/m',
      revision: 'main',
      node_id: 'n1',
      model_type: 'LLM',
    })
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/v1/catalog/huggingface/downloads')
  })

  it('gets download job by id', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ job_id: 'j2', status: 'DOWNLOADING' }),
      json: async () => ({ job_id: 'j2', status: 'DOWNLOADING' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await getHfDownload('j2')
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      '/api/v1/catalog/huggingface/downloads/j2',
    )
  })

  it('lists model caches with pagination filters', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({ items: [], page: 2, page_size: 10, total: 0 }),
      json: async () => ({ items: [], page: 2, page_size: 10, total: 0 }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await listModelCaches({ nodeId: 'node-x', page: 2, pageSize: 10 })
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/v1/model-cache')
    expect(url).toContain('node_id=node-x')
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=10')
  })

  it('purges model cache with optional force', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ id: 'c1', purged: true }),
      json: async () => ({ id: 'c1', purged: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await purgeModelCache('cache-1', { force: true })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('DELETE')
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/v1/model-cache/cache-1')
    expect(url).toContain('force=true')
  })
})
