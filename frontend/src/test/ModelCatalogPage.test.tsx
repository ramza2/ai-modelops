import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { HfCatalogModel, NodeSummary, Paginated } from '../api/types'
import { ModelCatalogPage } from '../pages/ModelCatalogPage'
import * as catalogApi from '../api/catalog'
import * as nodesApi from '../api/nodes'

function pageResult(
  items: HfCatalogModel[],
  opts: { page?: number; total?: number } = {},
): Paginated<HfCatalogModel> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    total: opts.total ?? items.length,
  }
}

function makeItem(
  overrides: Partial<HfCatalogModel> & Pick<HfCatalogModel, 'repository_id'>,
): HfCatalogModel {
  return {
    revision: 'abc',
    pipeline_tag: 'text-generation',
    model_type: 'LLM',
    architectures: ['LlamaForCausalLM'],
    tags: ['text-generation'],
    quantization_hint: null,
    dtype_hint: 'float16',
    gated: false,
    private: false,
    downloads: 10,
    likes: 1,
    estimated_download_size_bytes: 2_000_000_000,
    estimated_required_vram_mb: 2500,
    ...overrides,
  }
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/models/catalog" element={<ModelCatalogPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ModelCatalogPage', () => {
  beforeEach(() => {
    vi.spyOn(catalogApi, 'listHfCatalog')
    vi.spyOn(catalogApi, 'analyzeHfResourceFit')
    vi.spyOn(nodesApi, 'listNodes')
    vi.mocked(nodesApi.listNodes).mockResolvedValue({
      items: [
        {
          id: 'node-1',
          name: 'gpu-a',
          hostname: 'host-a',
          agent_base_url: 'http://host.docker.internal:8100',
          environment: 'local',
          region: null,
          status: 'ONLINE',
          last_heartbeat_at: null,
          cpu_model: null,
          ram_total_mb: null,
          disk_total_mb: null,
          labels_json: null,
          created_at: '2026-10-01T00:00:00Z',
          updated_at: '2026-10-01T00:00:00Z',
        } satisfies NodeSummary,
      ],
      page: 1,
      page_size: 100,
      total: 1,
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders catalog rows, fit badges, and disabled next-step actions', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({
          repository_id: 'org/fit-model',
          resource_fit: { result: 'FIT', reasons: ['GPU0 has spare VRAM'] },
        }),
        makeItem({
          repository_id: 'org/tight-model',
          resource_fit: { result: 'TIGHT', reasons: ['Thin headroom'] },
        }),
      ]),
    )

    renderAt('/models/catalog?node_id=node-1')

    expect(await screen.findByText('org/fit-model')).toBeInTheDocument()
    expect(screen.getByText('org/tight-model')).toBeInTheDocument()
    expect(screen.getAllByText(/적합/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/빠듯/).length).toBeGreaterThan(0)
    expect(screen.getByText('GPU0 has spare VRAM')).toBeInTheDocument()
    expect(screen.getByText('(FIT)')).toBeInTheDocument()
    expect(screen.getByText('(TIGHT)')).toBeInTheDocument()

    const downloadButtons = screen.getAllByRole('button', { name: 'Download' })
    const deployButtons = screen.getAllByRole('button', { name: 'Deploy' })
    expect(downloadButtons.every((b) => (b as HTMLButtonElement).disabled)).toBe(
      true,
    )
    expect(deployButtons.every((b) => (b as HTMLButtonElement).disabled)).toBe(
      true,
    )
  })

  it('shows loading then error state', async () => {
    let resolveCatalog: ((v: Paginated<HfCatalogModel>) => void) | null = null
    vi.mocked(catalogApi.listHfCatalog).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCatalog = resolve
        }),
    )
    renderAt('/models/catalog')
    expect(
      await screen.findByText('Hugging Face 카탈로그를 불러오는 중…'),
    ).toBeInTheDocument()

    resolveCatalog!(pageResult([]))
    await waitFor(() => {
      expect(
        screen.queryByText('Hugging Face 카탈로그를 불러오는 중…'),
      ).not.toBeInTheDocument()
    })

    vi.mocked(catalogApi.listHfCatalog).mockRejectedValue(
      new ApiError('Hub timeout', { status: 503, code: 'DEPENDENCY_UNAVAILABLE' }),
    )
    await userEvent.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('Hub timeout')).toBeInTheDocument()
  })

  it('toggles fit_only filter only when a node is selected', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(pageResult([]))
    renderAt('/models/catalog')
    const checkbox = await screen.findByLabelText('설치 가능만')
    expect(checkbox).toBeDisabled()

    await userEvent.selectOptions(screen.getByLabelText('Node'), 'node-1')
    await waitFor(() => {
      expect(screen.getByLabelText('설치 가능만')).not.toBeDisabled()
    })
    await userEvent.click(screen.getByLabelText('설치 가능만'))
    await waitFor(() => {
      expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
        expect.objectContaining({ fitOnly: true, nodeId: 'node-1' }),
      )
    })
  })

  it('runs per-row fit analysis when requested', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/analyze-me' })]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue({
      repository_id: 'org/analyze-me',
      revision: 'abc',
      node_id: 'node-1',
      result: 'INSUFFICIENT',
      estimated_required_vram_mb: 20000,
      estimated_download_size_bytes: 40_000_000_000,
      quantization_hint: null,
      dtype_hint: 'float16',
      disk_free_mb: 100000,
      disk_ok: true,
      tensor_parallel: 1,
      gpu_results: [],
      assumptions: [],
      warnings: [],
      reasons: ['Free VRAM too low'],
      advisory_only: true,
    })

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/analyze-me')).toBeInTheDocument()
    const row = screen.getByText('org/analyze-me').closest('tr')
    expect(row).not.toBeNull()
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: 'Fit 분석' }),
    )
    expect(await screen.findByText(/부족/)).toBeInTheDocument()
    expect(screen.getByText('Free VRAM too low')).toBeInTheDocument()
  })
})
