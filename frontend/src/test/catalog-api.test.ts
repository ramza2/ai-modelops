import { afterEach, describe, expect, it, vi } from 'vitest'
import { analyzeHfResourceFit, listHfCatalog } from '../api/catalog'

describe('catalog api', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('lists catalog with filters and has_more contract', async () => {
    const payload = {
      items: [],
      page: 1,
      page_size: 20,
      has_more: true,
      total: null,
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(payload),
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)
    const result = await listHfCatalog({
      q: 'llama',
      modelType: 'LLM',
      fitOnly: true,
      nodeId: 'n1',
      page: 2,
    })
    expect(fetchMock).toHaveBeenCalled()
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/v1/catalog/huggingface/models')
    expect(url).toContain('q=llama')
    expect(url).toContain('model_type=LLM')
    expect(url).toContain('fit_only=true')
    expect(url).toContain('node_id=n1')
    expect(url).toContain('page=2')
    expect(result.has_more).toBe(true)
    expect(result.total).toBeNull()
  })

  it('posts resource-fit body', async () => {
    const payload = {
      repository_id: 'org/x',
      result: 'FIT',
      advisory_only: true,
      gpu_results: [],
      suggested_gpu_device_ids: ['g0'],
      assumptions: [],
      warnings: [],
      reasons: [],
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(payload),
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)
    await analyzeHfResourceFit({
      repositoryId: 'org/x',
      nodeId: 'n1',
      modelType: 'LLM',
      tensorParallel: 2,
    })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      repository_id: 'org/x',
      revision: null,
      node_id: 'n1',
      model_type: 'LLM',
      tensor_parallel: 2,
    })
  })
})
