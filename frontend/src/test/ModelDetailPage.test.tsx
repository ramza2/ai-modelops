import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { ModelDetail, ModelVersion, Paginated } from '../api/types'
import { ModelDetailPage } from '../pages/ModelDetailPage'
import * as modelsApi from '../api/models'

function makeModel(overrides: Partial<ModelDetail> = {}): ModelDetail {
  return {
    id: 'm1',
    slug: 'chat-a',
    name: 'Chat A',
    model_type: 'LLM',
    provider: 'example',
    source_type: 'HF',
    license_name: 'Apache-2.0',
    description: 'A demo model',
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makeVersion(
  overrides: Partial<ModelVersion> & Pick<ModelVersion, 'id' | 'version_label'>,
): ModelVersion {
  return {
    model_id: 'm1',
    source_repository: 'example/model',
    source_revision: 'revision-placeholder',
    quantization: 'AWQ',
    dtype: 'auto',
    runtime_type: 'VLLM',
    runtime_image: 'example/runtime:tag',
    runtime_image_digest: null,
    served_model_name: 'served',
    expected_idle_vram_mb: 8192,
    expected_peak_vram_mb: 16384,
    default_max_model_len: 8192,
    runtime_config: {},
    archived_at: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function pageResult(
  items: ModelVersion[],
  opts: { page?: number; total?: number } = {},
): Paginated<ModelVersion> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    total: opts.total ?? items.length,
  }
}

function LocationProbe() {
  const [params] = useSearchParams()
  return <div data-testid="location-search">{params.toString()}</div>
}

function renderDetail(path = '/models/m1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/models/:modelId" element={<ModelDetailPage />} />
        <Route path="/models" element={<div>Models list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ModelDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(modelsApi, 'getModel')
    vi.spyOn(modelsApi, 'listModelVersions')
    vi.spyOn(modelsApi, 'listModelArtifacts')
    vi.spyOn(modelsApi, 'getModelVersion')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders metadata, versions, and links', async () => {
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(
      pageResult([
        makeVersion({ id: 'v1', version_label: '1.0.0' }),
        makeVersion({
          id: 'v2',
          version_label: '0.9.0',
          archived_at: '2026-09-01T00:00:00Z',
        }),
      ]),
    )
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat A' }),
    ).toBeInTheDocument()
    expect(screen.getByText('A demo model')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '1.0.0' })).toHaveAttribute(
      'href',
      '/model-versions/v1',
    )
    expect(modelsApi.listModelVersions).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ includeArchived: false }),
    )
    expect(screen.queryByText('2026-10-06T01:02:03Z')).not.toBeInTheDocument()
  })

  it('toggles include_archived and resets page', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(pageResult([]))
    renderDetail('/models/m1?page=2')
    await screen.findByLabelText('Archived 포함')
    await user.click(screen.getByLabelText('Archived 포함'))
    await waitFor(() => {
      const last = vi.mocked(modelsApi.listModelVersions).mock.calls.at(-1)
      expect(last?.[1]?.includeArchived).toBe(true)
      expect(last?.[1]?.page).toBe(1)
    })
    expect(screen.getByTestId('location-search')).toHaveTextContent(
      'include_archived=true',
    )
  })

  it('shows active-empty wording and archived display', async () => {
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(
      await screen.findByText(/표시할 활성 Model Version이 없습니다/),
    ).toBeInTheDocument()
  })

  it('keeps metadata when versions fail; keeps versions when metadata refresh fails', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockRejectedValue(
      new ApiError('versions down', { status: 503 }),
    )
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat A' }),
    ).toBeInTheDocument()
    expect(screen.getByText('versions down')).toBeInTheDocument()
    cleanup()

    vi.mocked(modelsApi.getModel)
      .mockResolvedValueOnce(makeModel())
      .mockRejectedValueOnce(new ApiError('meta fail', { status: 500 }))
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(
      pageResult([makeVersion({ id: 'v1', version_label: 'keep-v' })]),
    )
    renderDetail()
    expect(await screen.findByRole('link', { name: 'keep-v' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText('meta fail')).toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: 'keep-v' })).toBeInTheDocument()
  })

  it('shows 404 Model state', async () => {
    vi.mocked(modelsApi.getModel).mockRejectedValue(
      new ApiError('missing', { status: 404 }),
    )
    vi.mocked(modelsApi.listModelVersions).mockRejectedValue(
      new ApiError('missing', { status: 404 }),
    )
    renderDetail()
    expect(
      await screen.findByText('Model을 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: '← Models / Versions' }),
    ).toHaveAttribute('href', '/models')
  })

  it('clears stale Model and shows not-found on Model GET 404 refresh', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.getModel)
      .mockResolvedValueOnce(makeModel())
      .mockRejectedValueOnce(new ApiError('missing', { status: 404 }))
    vi.mocked(modelsApi.listModelVersions)
      .mockResolvedValueOnce(
        pageResult([makeVersion({ id: 'v1', version_label: 'keep-v' })]),
      )
      .mockRejectedValueOnce(new ApiError('missing', { status: 404 }))
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Chat A' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'keep-v' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('Model을 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'Chat A' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'keep-v' })).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: '← Models / Versions' }),
    ).toHaveAttribute('href', '/models')
  })

  it('omits include_archived=false from canonical URL', async () => {
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(pageResult([]))
    renderDetail('/models/m1?include_archived=false&page=2')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent('page=2')
    })
    expect(screen.getByTestId('location-search')).not.toHaveTextContent(
      'include_archived',
    )
  })

  it('does not call Capacity Profile or Deployments APIs', async () => {
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelVersions).mockResolvedValue(pageResult([]))
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'Chat A' })
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/deployments'))).toBe(false)
    expect(urls.some((u) => u.includes('/observability/runtime'))).toBe(false)
  })
})
