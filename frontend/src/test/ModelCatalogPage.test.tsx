import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  HfCatalogModel,
  HfCatalogPage,
  HfDownloadJob,
  NodeSummary,
  ResourceFitAnalysis,
} from '../api/types'
import { ModelCatalogPage } from '../pages/ModelCatalogPage'
import * as catalogApi from '../api/catalog'
import * as downloadsApi from '../api/downloads'
import * as nodesApi from '../api/nodes'

function pageResult(
  items: HfCatalogModel[],
  opts: { page?: number; hasMore?: boolean } = {},
): HfCatalogPage<HfCatalogModel> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    has_more: opts.hasMore ?? false,
    total: null,
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

function makeFitAnalysis(
  overrides: Partial<ResourceFitAnalysis> &
    Pick<ResourceFitAnalysis, 'repository_id'>,
): ResourceFitAnalysis {
  return {
    revision: 'abc',
    node_id: 'node-1',
    result: 'FIT',
    estimated_required_vram_mb: 2500,
    estimated_download_size_bytes: 2_000_000_000,
    quantization_hint: null,
    dtype_hint: 'float16',
    disk_free_mb: 100000,
    disk_ok: true,
    tensor_parallel: 1,
    gpu_results: [
      {
        gpu_device_id: 'gpu-0',
        gpu_index: 0,
        name: 'GPU-0',
        vram_total_mb: 16384,
        vram_free_mb: 12000,
        safety_margin_mb: 1024,
        estimated_required_vram_mb: 2500,
        result: 'FIT',
        reasons: ['GPU0 has spare VRAM'],
      },
      {
        gpu_device_id: 'gpu-1',
        gpu_index: 1,
        name: 'GPU-1',
        vram_total_mb: 16384,
        vram_free_mb: 2800,
        safety_margin_mb: 1024,
        estimated_required_vram_mb: 2500,
        result: 'TIGHT',
        reasons: ['GPU1 thin headroom'],
      },
    ],
    suggested_gpu_device_ids: ['gpu-0'],
    assumptions: ['Assumes single-GPU placement'],
    warnings: ['Advisory only'],
    reasons: ['Overall FIT on at least one GPU'],
    advisory_only: true,
    ...overrides,
  }
}

function makeJob(
  overrides: Partial<HfDownloadJob> & Pick<HfDownloadJob, 'job_id' | 'repository_id' | 'status'>,
): HfDownloadJob {
  return {
    agent_job_id: 'agent-1',
    node_id: 'node-1',
    model_artifact_id: 'art-1',
    node_model_cache_id: 'cache-1',
    requested_revision: 'abc',
    resolved_revision: 'abc',
    bytes_downloaded: null,
    total_bytes: null,
    progress_percent: null,
    local_path: null,
    error_code: null,
    error_message: null,
    cache_status: 'PREPARING',
    size_bytes: null,
    source_uri: 'hf://org/model',
    created_at: '2026-10-01T00:00:00Z',
    started_at: null,
    finished_at: null,
    updated_at: '2026-10-01T00:00:00Z',
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
    vi.spyOn(downloadsApi, 'startHfDownload')
    vi.spyOn(downloadsApi, 'getHfDownload')
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

  it('renders catalog rows, fit badges, enabled Download when node selected', async () => {
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

    const downloadButtons = screen.getAllByRole('button', { name: 'Download' })
    const deployButtons = screen.getAllByRole('button', { name: 'Deploy' })
    expect(downloadButtons.every((b) => !(b as HTMLButtonElement).disabled)).toBe(
      true,
    )
    expect(deployButtons.every((b) => (b as HTMLButtonElement).disabled)).toBe(
      true,
    )
  })

  it('polls active download until READY and shows cache metadata', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/dl-model' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-1',
        repository_id: 'org/dl-model',
        status: 'DOWNLOADING',
        progress_percent: 10,
      }),
    )
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-1',
        repository_id: 'org/dl-model',
        status: 'READY',
        local_path: '/data/hf/org-dl-model',
        resolved_revision: 'rev-ready',
        size_bytes: 4096,
        progress_percent: 100,
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/dl-model')).closest('tr')
    expect(row).not.toBeNull()
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: 'Download' }),
    )

    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalledWith({
        repositoryId: 'org/dl-model',
        revision: 'abc',
        nodeId: 'node-1',
        modelType: 'LLM',
      })
    })

    await waitFor(() => {
      expect(screen.getByText('/data/hf/org-dl-model')).toBeInTheDocument()
    })
    expect(screen.getByText(/rev rev-ready/)).toBeInTheDocument()
    expect(downloadsApi.getHfDownload).toHaveBeenCalled()
  })

  it('shows FAILED state with retry', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/fail-model' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-fail',
        repository_id: 'org/fail-model',
        status: 'FAILED',
        error_message: 'Hub timeout',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/fail-model')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: 'Download' }),
    )
    expect(await screen.findByText('Hub timeout')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry download' }))
    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalledTimes(2)
    })
  })

  it('disables Download on disk insufficient but not VRAM-only insufficient', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({ repository_id: 'org/vram-bad' }),
        makeItem({ repository_id: 'org/disk-bad' }),
      ]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockImplementation(
      async ({ repositoryId }) => {
        if (repositoryId === 'org/vram-bad') {
          return {
            repository_id: repositoryId,
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
            suggested_gpu_device_ids: [],
            assumptions: [],
            warnings: [],
            reasons: ['Free VRAM too low'],
            advisory_only: true,
          }
        }
        return {
          repository_id: repositoryId,
          revision: 'abc',
          node_id: 'node-1',
          result: 'INSUFFICIENT',
          estimated_required_vram_mb: 2000,
          estimated_download_size_bytes: 40_000_000_000,
          quantization_hint: null,
          dtype_hint: 'float16',
          disk_free_mb: 100,
          disk_ok: false,
          tensor_parallel: 1,
          gpu_results: [],
          suggested_gpu_device_ids: [],
          assumptions: [],
          warnings: [],
          reasons: ['Disk full'],
          advisory_only: true,
        }
      },
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/vram-bad')).toBeInTheDocument()

    const vramRow = screen.getByText('org/vram-bad').closest('tr') as HTMLElement
    const diskRow = screen.getByText('org/disk-bad').closest('tr') as HTMLElement

    await userEvent.click(within(vramRow).getByRole('button', { name: 'Fit 분석' }))
    await userEvent.click(within(diskRow).getByRole('button', { name: 'Fit 분석' }))

    await waitFor(() => {
      expect(
        within(vramRow).getByRole('button', { name: 'Download' }),
      ).not.toBeDisabled()
    })
    expect(
      within(diskRow).getByRole('button', { name: 'Download' }),
    ).toBeDisabled()
  })

  it('shows loading then error state', async () => {
    let resolveCatalog: ((v: HfCatalogPage<HfCatalogModel>) => void) | null = null
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
    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({
        repository_id: 'org/analyze-me',
        result: 'INSUFFICIENT',
        estimated_required_vram_mb: 20000,
        gpu_results: [],
        suggested_gpu_device_ids: [],
        assumptions: [],
        warnings: [],
        reasons: ['Free VRAM too low'],
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/analyze-me')).toBeInTheDocument()
    const row = screen.getByText('org/analyze-me').closest('tr')
    expect(row).not.toBeNull()
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: 'Fit 분석' }),
    )
    const detail = await screen.findByTestId('fit-detail-org/analyze-me')
    expect(within(detail).getAllByText(/부족/).length).toBeGreaterThan(0)
    expect(within(detail).getByText('Free VRAM too low')).toBeInTheDocument()
    expect(
      within(row as HTMLElement).getByRole('button', { name: 'Download' }),
    ).not.toBeDisabled()
  })

  it('shows loading then auto-expands GPU detail; supports collapse and re-analyze', async () => {
    let resolveFit: ((v: ResourceFitAnalysis) => void) | null = null
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({
          repository_id: 'org/fit-ux',
          resource_fit: { result: 'TIGHT', reasons: ['List summary only'] },
        }),
      ]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFit = resolve
        }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/fit-ux')).closest(
      'tr',
    ) as HTMLElement
    expect(within(row).getByText('List summary only')).toBeInTheDocument()
    expect(within(row).getByText('목록 요약')).toBeInTheDocument()

    await userEvent.click(within(row).getByRole('button', { name: 'Fit 분석' }))
    expect(row).toHaveAttribute('data-fit-loading', 'true')
    expect(within(row).getByText(/Fit 분석 중/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: '분석 중…' })).toBeDisabled()

    resolveFit!(
      makeFitAnalysis({
        repository_id: 'org/fit-ux',
        result: 'FIT',
        warnings: ['Do not pool VRAM'],
      }),
    )

    const detail = await screen.findByTestId('fit-detail-org/fit-ux')
    expect(within(detail).getByText('종합 결과')).toBeInTheDocument()
    expect(within(detail).getByText('index 0')).toBeInTheDocument()
    expect(within(detail).getByText('index 1')).toBeInTheDocument()
    expect(within(detail).getByText('GPU1 thin headroom')).toBeInTheDocument()
    expect(within(detail).getByText('Do not pool VRAM')).toBeInTheDocument()
    expect(within(detail).getByText('Assumes single-GPU placement')).toBeInTheDocument()
    expect(within(detail).getByText('Suggested GPU (참고)')).toBeInTheDocument()
    expect(within(detail).getByText('gpu-0')).toBeInTheDocument()
    expect(within(detail).getByText(/서버 완료 시각이 아닙니다/)).toBeInTheDocument()
    expect(within(row).getByText('상세 분석')).toBeInTheDocument()
    expect(within(row).getByText('List summary only')).toBeInTheDocument()

    await userEvent.click(within(row).getByRole('button', { name: '접기' }))
    expect(screen.queryByTestId('fit-detail-org/fit-ux')).not.toBeInTheDocument()
    await userEvent.click(within(row).getByRole('button', { name: '상세 펼치기' }))
    expect(screen.getByTestId('fit-detail-org/fit-ux')).toBeInTheDocument()

    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({
        repository_id: 'org/fit-ux',
        result: 'TIGHT',
        reasons: ['Re-analyzed'],
        gpu_results: [
          {
            gpu_device_id: 'gpu-0',
            gpu_index: 0,
            name: 'GPU-0',
            vram_total_mb: 16384,
            vram_free_mb: 3000,
            safety_margin_mb: 1024,
            estimated_required_vram_mb: 2500,
            result: 'TIGHT',
            reasons: ['retry tight'],
          },
        ],
      }),
    )
    await userEvent.click(within(row).getByRole('button', { name: '재분석' }))
    expect(await screen.findByText('Re-analyzed')).toBeInTheDocument()
    expect(screen.getByText('retry tight')).toBeInTheDocument()
  })

  it('ignores stale fit responses after Node or search context changes', async () => {
    let resolveLate: ((v: ResourceFitAnalysis) => void) | null = null
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/stale-fit' })]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLate = resolve
        }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/stale-fit')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: 'Fit 분석' }))
    await waitFor(() => {
      expect(resolveLate).not.toBeNull()
    })

    await userEvent.selectOptions(screen.getByLabelText('Node'), '')
    resolveLate!(
      makeFitAnalysis({
        repository_id: 'org/stale-fit',
        result: 'FIT',
        reasons: ['stale-should-not-appear'],
      }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText('stale-should-not-appear'),
      ).not.toBeInTheDocument()
    })
    expect(screen.queryByTestId('fit-detail-org/stale-fit')).not.toBeInTheDocument()
  })

  it('keeps other model fit/download state when one fit analysis fails, and allows retry', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({ repository_id: 'org/ok-fit' }),
        makeItem({ repository_id: 'org/fail-fit' }),
      ]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockImplementation(
      async ({ repositoryId }) => {
        if (repositoryId === 'org/fail-fit') {
          throw new ApiError('Fit upstream timeout', {
            status: 503,
            code: 'DEPENDENCY_UNAVAILABLE',
          })
        }
        return makeFitAnalysis({ repository_id: repositoryId })
      },
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-ok',
        repository_id: 'org/ok-fit',
        status: 'DOWNLOADING',
        progress_percent: 20,
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const okRow = (await screen.findByText('org/ok-fit')).closest(
      'tr',
    ) as HTMLElement
    const failRow = screen.getByText('org/fail-fit').closest('tr') as HTMLElement

    await userEvent.click(within(okRow).getByRole('button', { name: 'Download' }))
    await waitFor(() => {
      expect(within(okRow).getByText(/Progress/)).toBeInTheDocument()
    })

    await userEvent.click(within(okRow).getByRole('button', { name: 'Fit 분석' }))
    expect(await screen.findByTestId('fit-detail-org/ok-fit')).toBeInTheDocument()

    await userEvent.click(within(failRow).getByRole('button', { name: 'Fit 분석' }))
    expect(await screen.findByTestId('fit-error-org/fail-fit')).toHaveTextContent(
      'Fit upstream timeout',
    )
    expect(screen.getByTestId('fit-detail-org/ok-fit')).toBeInTheDocument()
    expect(within(okRow).getByText(/Progress/)).toBeInTheDocument()
    expect(screen.queryByText('카탈로그 오류')).not.toBeInTheDocument()

    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({
        repository_id: 'org/fail-fit',
        result: 'TIGHT',
        reasons: ['recovered'],
      }),
    )
    await userEvent.click(within(failRow).getByRole('button', { name: 'Fit 분석' }))
    expect(await screen.findByTestId('fit-detail-org/fail-fit')).toBeInTheDocument()
    expect(screen.getByText('recovered')).toBeInTheDocument()
    expect(screen.queryByTestId('fit-error-org/fail-fit')).not.toBeInTheDocument()
  })

  it('enables Next when has_more is true and navigates to page 2', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockImplementation(async (params = {}) => {
      if ((params.page ?? 1) === 1) {
        return pageResult([makeItem({ repository_id: 'org/page-1' })], {
          page: 1,
          hasMore: true,
        })
      }
      return pageResult([makeItem({ repository_id: 'org/page-2' })], {
        page: 2,
        hasMore: false,
      })
    })

    renderAt('/models/catalog')
    expect(await screen.findByText('org/page-1')).toBeInTheDocument()
    const next = screen.getByRole('button', { name: '다음' })
    expect(next).not.toBeDisabled()
    await userEvent.click(next)
    expect(await screen.findByText('org/page-2')).toBeInTheDocument()
    await waitFor(() => {
      expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 }),
      )
    })
    expect(screen.getByRole('button', { name: '다음' })).toBeDisabled()
  })

  it('requires explicit type selection when catalog model_type is unknown', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({
          repository_id: 'org/unknown-type',
          model_type: null,
          pipeline_tag: 'other',
        }),
      ]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-unk',
        repository_id: 'org/unknown-type',
        status: 'READY',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/unknown-type')).closest('tr')
    expect(row).not.toBeNull()
    const download = within(row as HTMLElement).getByRole('button', {
      name: 'Download',
    })
    expect(download).toBeDisabled()
    expect(download).toHaveAttribute(
      'title',
      'Select LLM/VLM/EMBEDDING before Download',
    )

    await userEvent.selectOptions(
      within(row as HTMLElement).getByLabelText('Type for org/unknown-type'),
      'EMBEDDING',
    )
    expect(download).not.toBeDisabled()
    await userEvent.click(download)
    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalledWith({
        repositoryId: 'org/unknown-type',
        revision: 'abc',
        nodeId: 'node-1',
        modelType: 'EMBEDDING',
      })
    })
  })
})
