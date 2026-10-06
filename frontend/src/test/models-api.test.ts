import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getModel,
  getModelVersion,
  listModelArtifacts,
  listModels,
  listModelVersions,
} from '../api/models'

describe('models API module', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('maps listModels query params', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain('/api/v1/models?')
      expect(url).toContain('q=qwen')
      expect(url).toContain('provider=acme')
      expect(url).toContain('model_type=LLM')
      expect(url).toContain('is_active=true')
      expect(url).toContain('page=2')
      expect(url).toContain('page_size=20')
      return new Response(
        JSON.stringify({ items: [], page: 2, page_size: 20, total: 0 }),
        { status: 200 },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    await listModels({
      q: 'qwen',
      provider: 'acme',
      modelType: 'LLM',
      isActive: true,
      page: 2,
      pageSize: 20,
    })
    expect(fetchMock).toHaveBeenCalled()
  })

  it('omits ALL-style filters from listModels', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).not.toContain('model_type=')
      expect(url).not.toContain('is_active=')
      expect(url).not.toContain('q=')
      expect(url).not.toContain('provider=')
      return new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200 },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    await listModels({ page: 1 })
  })

  it('gets model and version', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/models/m1') && !String(url).includes('versions')) {
          return new Response(
            JSON.stringify({
              id: 'm1',
              slug: 's',
              name: 'n',
              model_type: 'LLM',
              provider: null,
              source_type: 'HF',
              license_name: null,
              description: null,
              is_active: true,
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-01T00:00:00Z',
            }),
            { status: 200 },
          )
        }
        return new Response(
          JSON.stringify({
            id: 'v1',
            model_id: 'm1',
            version_label: 'v1',
            source_repository: null,
            source_revision: null,
            quantization: null,
            dtype: null,
            runtime_type: 'VLLM',
            runtime_image: 'img',
            runtime_image_digest: null,
            served_model_name: 'served',
            expected_idle_vram_mb: null,
            expected_peak_vram_mb: null,
            default_max_model_len: null,
            runtime_config: {},
            archived_at: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          }),
          { status: 200 },
        )
      }),
    )
    expect((await getModel('m1')).id).toBe('m1')
    expect((await getModelVersion('v1')).id).toBe('v1')
  })

  it('maps include_archived for listModelVersions', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain('include_archived=true')
      expect(url).toContain('page=1')
      return new Response(
        JSON.stringify({ items: [], page: 1, page_size: 20, total: 0 }),
        { status: 200 },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    await listModelVersions('m1', { includeArchived: true })
  })

  it('paginates artifacts and propagates AbortSignal', async () => {
    const ac = new AbortController()
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(init?.signal).toBe(ac.signal)
      expect(String(_url)).toContain('page=3')
      expect(String(_url)).toContain('page_size=20')
      return new Response(
        JSON.stringify({ items: [], page: 3, page_size: 20, total: 0 }),
        { status: 200 },
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    await listModelArtifacts('v1', { page: 3, signal: ac.signal })
  })
})
