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
import {
  CATALOG_DOWNLOAD_POLL_MS,
  ModelCatalogPage,
} from '../pages/ModelCatalogPage'
import * as catalogApi from '../api/catalog'
import * as downloadsApi from '../api/downloads'
import * as nodesApi from '../api/nodes'
import {
  catalogDownloadDraftKey,
  writeCatalogDownloadDraft,
} from '../utils/catalogDownloadDraft'

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
    // jsdom lacks scrollIntoView; polyfill so Fit reveal layout effect is safe.
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: vi.fn(),
    })
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
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
    sessionStorage.clear()
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
    expect(screen.getAllByText(/여유 부족/).length).toBeGreaterThan(0)
    expect(
      screen.getByRole('list', { name: '사용 안내' }),
    ).toHaveTextContent(/모델 검색 및 대상 서버 선택/)
    expect(screen.getByText(/현재 조회 서버:/)).toBeInTheDocument()
    expect(
      screen.getAllByText(/검색어\(키워드\)만 「검색」버튼을 눌러야 적용/)
        .length,
    ).toBeGreaterThan(0)

    const glossary = screen.getByText('용어 및 상태 안내').closest(
      'details',
    ) as HTMLDetailsElement
    expect(glossary).toBeTruthy()
    await userEvent.click(screen.getByText('용어 및 상태 안내'))
    expect(within(glossary).getByText('Fit 판정 기준')).toBeVisible()
    expect(within(glossary).getByText('GPU 적합성')).toBeVisible()
    expect(within(glossary).getByText('다운로드 상태')).toBeVisible()
    expect(within(glossary).getByText('설치 가능만')).toBeVisible()

    const fitBadge = screen.getByTitle(/자원 적합\(FIT\)/)
    expect(fitBadge).toHaveTextContent(/\(FIT\)/)
    expect(fitBadge.getAttribute('title')).toMatch(/배포 성공을 보장하지 않습니다/)
    // Inner status code must not replace the beginner title.
    expect(fitBadge.querySelector('[title="FIT"]')).toBeNull()

    const downloadButtons = screen.getAllByRole('button', { name: '다운로드' })
    expect(downloadButtons.every((b) => !(b as HTMLButtonElement).disabled)).toBe(
      true,
    )
    expect(screen.queryByRole('button', { name: '배포 설정' })).not.toBeInTheDocument()
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
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
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
    expect(screen.getByText(/버전 rev-ready/)).toBeInTheDocument()
    expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    // Default makeJob cache_status is PREPARING — Wizard link must not appear.
    expect(
      screen.getByRole('link', { name: '캐시 목록 확인' }),
    ).toHaveAttribute('href', '/models/cache?node_id=node-1')
    expect(
      screen.queryByRole('link', { name: '배포 설정' }),
    ).not.toBeInTheDocument()
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
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    expect(await screen.findByText('Hub timeout')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '다시 다운로드' }))
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

    await userEvent.click(within(vramRow).getByRole('button', { name: 'GPU 적합성 분석' }))
    await userEvent.click(within(diskRow).getByRole('button', { name: 'GPU 적합성 분석' }))

    await waitFor(() => {
      expect(
        within(vramRow).getByRole('button', { name: '다운로드' }),
      ).not.toBeDisabled()
    })
    expect(
      within(diskRow).getByRole('button', { name: '다운로드' }),
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

    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), 'node-1')
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
      within(row as HTMLElement).getByRole('button', { name: 'GPU 적합성 분석' }),
    )
    const detail = await screen.findByTestId('fit-detail-org/analyze-me')
    expect(within(detail).getAllByText(/부족/).length).toBeGreaterThan(0)
    expect(within(detail).getByText('Free VRAM too low')).toBeInTheDocument()
    expect(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
  })

  it('shows loading then auto-expands GPU detail; supports collapse and re-analyze', async () => {
    let resolveFit: ((v: ResourceFitAnalysis) => void) | null = null
    const scrollIntoView = Element.prototype.scrollIntoView as ReturnType<
      typeof vi.fn
    >
    scrollIntoView.mockClear()
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

    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))
    expect(row).toHaveAttribute('data-fit-loading', 'true')
    expect(
      within(row).getByText(/선택한 서버의 GPU 자원을 확인하는 중/),
    ).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: '분석 중…' })).toBeDisabled()

    resolveFit!(
      makeFitAnalysis({
        repository_id: 'org/fit-ux',
        result: 'FIT',
        warnings: ['Do not pool VRAM'],
      }),
    )

    const detail = await screen.findByTestId('fit-detail-org/fit-ux')
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalled()
    })
    expect(
      scrollIntoView.mock.calls.some((call) => {
        const opts = call[0] as ScrollIntoViewOptions | undefined
        return opts?.block === 'nearest'
      }),
    ).toBe(true)
    expect(
      within(detail).getByRole('heading', { name: /GPU 적합성 상세 분석/ }),
    ).toHaveFocus()
    expect(within(detail).getByText('종합 판정')).toBeInTheDocument()
    expect(within(detail).getByText('GPU 0')).toBeInTheDocument()
    expect(within(detail).getByText('GPU 1')).toBeInTheDocument()
    expect(
      within(detail).getAllByText('현재 여유 GPU 메모리').length,
    ).toBeGreaterThan(0)
    expect(within(detail).getAllByText('안전 여유').length).toBeGreaterThan(0)
    expect(screen.getByTestId('fit-gpu-cards-org/fit-ux')).toBeInTheDocument()
    expect(screen.getByTestId('fit-gpu-card-org/fit-ux-0')).toBeInTheDocument()
    expect(screen.getByTestId('fit-gpu-card-org/fit-ux-1')).toBeInTheDocument()
    expect(within(detail).getByText('GPU1 thin headroom')).toBeInTheDocument()
    expect(within(detail).getByText('Do not pool VRAM')).toBeInTheDocument()
    expect(within(detail).getByText('Assumes single-GPU placement')).toBeInTheDocument()
    expect(within(detail).getByText('추천 GPU 및 판단 근거')).toBeInTheDocument()
    expect(within(detail).getByText(/추천 GPU \(참고/)).toBeInTheDocument()
    expect(
      within(row).getByTestId('fit-success-org/fit-ux'),
    ).toHaveTextContent(/GPU 적합성 분석 완료/)
    expect(
      within(detail).getAllByText(/Cache Deploy Wizard/).length,
    ).toBeGreaterThan(0)
    expect(within(detail).getAllByText('gpu-0').length).toBeGreaterThan(0)
    expect(
      within(detail).getAllByText(/서버 완료 시각이 아닙니다|서버 완료 시각 아님/)
        .length,
    ).toBeGreaterThan(0)
    expect(within(row).getByText('분석 완료')).toBeInTheDocument()
    expect(screen.getByTestId('fit-row-status-org/fit-ux')).toBeInTheDocument()
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
    expect(screen.getAllByText('retry tight').length).toBeGreaterThan(0)
  })

  it('shows analyze failure with explicit retry that re-runs Fit', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/fit-fail' })]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit)
      .mockRejectedValueOnce(
        new ApiError('Node Agent unreachable', {
          status: 503,
          code: 'UNAVAILABLE',
        }),
      )
      .mockResolvedValue(
        makeFitAnalysis({
          repository_id: 'org/fit-fail',
          result: 'FIT',
          reasons: ['recovered'],
        }),
      )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/fit-fail')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))
    expect(await screen.findByTestId('fit-error-org/fit-fail')).toHaveTextContent(
      'Node Agent unreachable',
    )
    expect(within(row).getByText('분석 실패')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: '재시도' })).toBeInTheDocument()

    await userEvent.click(within(row).getByRole('button', { name: '재시도' }))
    expect(await screen.findByTestId('fit-detail-org/fit-fail')).toBeInTheDocument()
    expect(within(row).getByText('분석 완료')).toBeInTheDocument()
    expect(screen.getByText('recovered')).toBeInTheDocument()
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
    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))
    await waitFor(() => {
      expect(resolveLate).not.toBeNull()
    })

    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), '')
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

  it('drops revision-A Fit detail and disk block when list refreshes to revision B; ignores late A', async () => {
    let listCalls = 0
    let resolveLateA: ((v: ResourceFitAnalysis) => void) | null = null
    vi.mocked(catalogApi.listHfCatalog).mockImplementation(async () => {
      listCalls += 1
      if (listCalls === 1) {
        return pageResult([
          makeItem({ repository_id: 'org/rev-swap', revision: 'rev-a' }),
        ])
      }
      return pageResult([
        makeItem({ repository_id: 'org/rev-swap', revision: 'rev-b' }),
      ])
    })
    vi.mocked(catalogApi.analyzeHfResourceFit)
      .mockResolvedValueOnce(
        makeFitAnalysis({
          repository_id: 'org/rev-swap',
          revision: 'rev-a',
          result: 'INSUFFICIENT',
          disk_ok: false,
          disk_free_mb: 10,
          reasons: ['Disk full on rev-a'],
          gpu_results: [],
          suggested_gpu_device_ids: [],
          warnings: [],
          assumptions: [],
        }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveLateA = resolve
          }),
      )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/rev-swap')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))

    expect(await screen.findByTestId('fit-detail-org/rev-swap')).toBeInTheDocument()
    expect(screen.getByText('Disk full on rev-a')).toBeInTheDocument()
    expect(
      within(row).getByRole('button', { name: '다운로드' }),
    ).toBeDisabled()
    expect(
      within(row).getByRole('button', { name: '다운로드' }),
    ).toHaveAttribute(
      'title',
      '디스크 공간이 부족합니다(GPU 메모리 부족과 별개). 다운로드를 시작할 수 없습니다.',
    )

    // Start a second analyze for A that will resolve after the list moves to B.
    await userEvent.click(within(row).getByRole('button', { name: '재분석' }))
    await waitFor(() => {
      expect(resolveLateA).not.toBeNull()
    })

    await userEvent.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(listCalls).toBeGreaterThanOrEqual(2)
    })
    await waitFor(() => {
      expect(
        screen.queryByTestId('fit-detail-org/rev-swap'),
      ).not.toBeInTheDocument()
    })
    expect(screen.queryByText('Disk full on rev-a')).not.toBeInTheDocument()

    const rowB = screen.getByText('org/rev-swap').closest('tr') as HTMLElement
    expect(
      within(rowB).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
    expect(
      within(rowB).getByRole('button', { name: 'GPU 적합성 분석' }),
    ).toBeInTheDocument()

    resolveLateA!(
      makeFitAnalysis({
        repository_id: 'org/rev-swap',
        revision: 'rev-a',
        result: 'INSUFFICIENT',
        disk_ok: false,
        disk_free_mb: 10,
        reasons: ['late-rev-a-must-not-apply'],
        gpu_results: [],
      }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText('late-rev-a-must-not-apply'),
      ).not.toBeInTheDocument()
    })
    expect(screen.queryByTestId('fit-detail-org/rev-swap')).not.toBeInTheDocument()
    expect(
      within(rowB).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
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
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
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

    await userEvent.click(within(okRow).getByRole('button', { name: '다운로드' }))
    await waitFor(() => {
      expect(within(okRow).getByText(/진행률 20%/)).toBeInTheDocument()
    })

    await userEvent.click(within(okRow).getByRole('button', { name: 'GPU 적합성 분석' }))
    expect(await screen.findByTestId('fit-detail-org/ok-fit')).toBeInTheDocument()

    await userEvent.click(within(failRow).getByRole('button', { name: 'GPU 적합성 분석' }))
    expect(await screen.findByTestId('fit-error-org/fail-fit')).toHaveTextContent(
      'Fit upstream timeout',
    )
    expect(screen.getByTestId('fit-detail-org/ok-fit')).toBeInTheDocument()
    expect(within(okRow).getByText(/진행률 20%/)).toBeInTheDocument()
    expect(screen.queryByText('카탈로그 오류')).not.toBeInTheDocument()

    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({
        repository_id: 'org/fail-fit',
        result: 'TIGHT',
        reasons: ['recovered'],
      }),
    )
    await userEvent.click(within(failRow).getByRole('button', { name: '재시도' }))
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
      name: '다운로드',
    })
    expect(download).toBeDisabled()
    expect(download).toHaveAttribute(
      'title',
      '모델 유형이 미확정입니다. LLM/VLM/EMBEDDING을 선택한 뒤 다운로드하세요.',
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

  it('persists Job id on start and restores via GET only after remount (F5)', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/restore-model' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-restore-1',
        repository_id: 'org/restore-model',
        status: 'DOWNLOADING',
        progress_percent: 25,
        bytes_downloaded: 512,
        total_bytes: 2048,
      }),
    )
    // Active poll uses the same GET mock until remount; keep progress in sync
    // with the POST payload so assertions are stable.
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-restore-1',
        repository_id: 'org/restore-model',
        status: 'DOWNLOADING',
        progress_percent: 25,
        bytes_downloaded: 512,
        total_bytes: 2048,
      }),
    )

    const { unmount } = renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/restore-model')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalledTimes(1)
    })
    expect(
      sessionStorage.getItem(
        catalogDownloadDraftKey({
          nodeId: 'node-1',
          repositoryId: 'org/restore-model',
          requestedRevision: 'abc',
        }),
      ),
    ).toContain('job-restore-1')
    await waitFor(() => {
      expect(screen.getByText(/진행률 25%/)).toBeInTheDocument()
    })
    expect(screen.getByText(/512 B \/ 2\.0 KiB/)).toBeInTheDocument()

    unmount()
    vi.mocked(downloadsApi.startHfDownload).mockClear()
    vi.mocked(downloadsApi.getHfDownload).mockClear()
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-restore-1',
        repository_id: 'org/restore-model',
        status: 'READY',
        local_path: '/data/hf/restore',
        resolved_revision: 'abc',
        size_bytes: 2048,
        progress_percent: 100,
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
        'job-restore-1',
        expect.anything(),
      )
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
    expect(await screen.findByText('/data/hf/restore')).toBeInTheDocument()
  })

  it('restores FAILED/CANCELED without auto POST and allows explicit Retry', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/fail-restore' })]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-fail-restore',
      nodeId: 'node-1',
      repositoryId: 'org/fail-restore',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-fail-restore',
        repository_id: 'org/fail-restore',
        status: 'FAILED',
        error_message: 'disk full',
      }),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-fail-retry',
        repository_id: 'org/fail-restore',
        status: 'QUEUED',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/fail-restore')).closest(
      'tr',
    ) as HTMLElement
    expect(await within(row).findByText('disk full')).toBeInTheDocument()
    expect(within(row).getByText('다운로드 실패')).toBeInTheDocument()
    expect(within(row).getByText(/다운로드 실패 원인/)).toBeInTheDocument()
    expect(row.querySelector('[data-download-state="failed"]')).not.toBeNull()
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: '다시 다운로드' }))
    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalledTimes(1)
    })
  })

  it('shows CANCELED as 다운로드 취소됨 without failure-cause framing', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/canceled' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-canceled',
        repository_id: 'org/canceled',
        status: 'CANCELED',
        error_message: 'canceled by operator',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/canceled')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: '다운로드' }))

    expect(
      await within(row).findByText('다운로드 취소됨'),
    ).toBeInTheDocument()
    expect(row.querySelector('[data-download-state="canceled"]')).not.toBeNull()
    expect(row.querySelector('[data-download-state="failed"]')).toBeNull()
    expect(within(row).queryByText('다운로드 실패')).not.toBeInTheDocument()
    expect(
      within(row).queryByText(/다운로드 실패 원인/),
    ).not.toBeInTheDocument()
    expect(within(row).queryByText('canceled by operator')).not.toBeInTheDocument()
    expect(
      within(row).getByRole('button', { name: '다시 다운로드' }),
    ).toBeInTheDocument()
  })

  it('isolates download tracking by node and requested_revision', async () => {
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
        },
        {
          id: 'node-2',
          name: 'gpu-b',
          hostname: 'host-b',
          agent_base_url: 'http://host.docker.internal:8101',
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
        },
      ],
      page: 1,
      page_size: 100,
      total: 2,
    })
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({ repository_id: 'org/shared', revision: 'rev-a' }),
      ]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-node2',
      nodeId: 'node-2',
      repositoryId: 'org/shared',
      requestedRevision: 'rev-a',
      savedAt: Date.now(),
    })
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-rev-b',
      nodeId: 'node-1',
      repositoryId: 'org/shared',
      requestedRevision: 'rev-b',
      savedAt: Date.now(),
    })
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-node1-reva',
      nodeId: 'node-1',
      repositoryId: 'org/shared',
      requestedRevision: 'rev-a',
      savedAt: Date.now(),
    })
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async (jobId) => {
      if (jobId === 'job-node1-reva') {
        return makeJob({
          job_id: 'job-node1-reva',
          repository_id: 'org/shared',
          node_id: 'node-1',
          requested_revision: 'rev-a',
          status: 'READY',
          local_path: '/data/hf/node1-reva',
        })
      }
      if (jobId === 'job-node2') {
        return makeJob({
          job_id: 'job-node2',
          repository_id: 'org/shared',
          node_id: 'node-2',
          requested_revision: 'rev-a',
          status: 'READY',
          local_path: '/data/hf/node2',
        })
      }
      return makeJob({
        job_id: jobId,
        repository_id: 'org/shared',
        node_id: 'node-1',
        requested_revision: 'rev-b',
        status: 'READY',
        local_path: '/data/hf/rev-b',
      })
    })

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('/data/hf/node1-reva')).toBeInTheDocument()
    expect(screen.queryByText('/data/hf/node2')).not.toBeInTheDocument()
    expect(screen.queryByText('/data/hf/rev-b')).not.toBeInTheDocument()
    expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
      'job-node1-reva',
      expect.anything(),
    )
    expect(downloadsApi.getHfDownload).not.toHaveBeenCalledWith(
      'job-node2',
      expect.anything(),
    )
  })

  it('blocks Download while restoring and does not POST', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/restoring' })]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-slow-restore',
      nodeId: 'node-1',
      repositoryId: 'org/restoring',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    let resolveGet!: (job: HfDownloadJob) => void
    const getPromise = new Promise<HfDownloadJob>((resolve) => {
      resolveGet = resolve
    })
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(getPromise)

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/restoring')).closest('tr')
    expect(row).not.toBeNull()
    await waitFor(() => {
      expect(
        within(row as HTMLElement).getByRole('button', { name: '복원 중…' }),
      ).toBeDisabled()
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()

    resolveGet(
      makeJob({
        job_id: 'job-slow-restore',
        repository_id: 'org/restoring',
        status: 'VERIFYING',
        progress_percent: 90,
      }),
    )
    await waitFor(() => {
      expect(screen.getByText(/진행률 90%/)).toBeInTheDocument()
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
  })

  it('keeps last good download status on poll failure and recovers on retry', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/poll-warn' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-poll-warn',
        repository_id: 'org/poll-warn',
        status: 'DOWNLOADING',
        progress_percent: 15,
      }),
    )
    let allowPollSuccess = false
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async () => {
      if (!allowPollSuccess) {
        throw new ApiError('upstream unavailable', { status: 503 })
      }
      return makeJob({
        job_id: 'job-poll-warn',
        repository_id: 'org/poll-warn',
        status: 'DOWNLOADING',
        progress_percent: 55,
      })
    })

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/poll-warn')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    await waitFor(() => {
      expect(screen.getByText(/진행률 15%/)).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(
        screen.getByText(/다운로드 상태 갱신에 실패했습니다/),
      ).toBeInTheDocument()
      expect(screen.getByText(/upstream unavailable/)).toBeInTheDocument()
    })
    expect(screen.getByText(/진행률 15%/)).toBeInTheDocument()

    allowPollSuccess = true
    await userEvent.click(
      screen.getByRole('button', { name: '상태 다시 조회' }),
    )
    await waitFor(() => {
      expect(screen.getByText(/진행률 55%/)).toBeInTheDocument()
    })
  })

  it('clears unusable draft on 404 restore without auto POST', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/missing-job' })]),
    )
    const key = catalogDownloadDraftKey({
      nodeId: 'node-1',
      repositoryId: 'org/missing-job',
      requestedRevision: 'abc',
    })
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-gone',
      nodeId: 'node-1',
      repositoryId: 'org/missing-job',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    vi.mocked(downloadsApi.getHfDownload).mockRejectedValue(
      new ApiError('not found', { status: 404 }),
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/missing-job')).toBeInTheDocument()
    await waitFor(() => {
      expect(
        screen.getByText(/저장된 다운로드 Job을 찾을 수 없어 추적을 정리/),
      ).toBeInTheDocument()
    })
    expect(sessionStorage.getItem(key)).toBeNull()
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
  })

  it('ignores corrupt sessionStorage drafts without breaking the page', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/corrupt-draft' })]),
    )
    sessionStorage.setItem(
      catalogDownloadDraftKey({
        nodeId: 'node-1',
        repositoryId: 'org/corrupt-draft',
        requestedRevision: 'abc',
      }),
      '{not-json',
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/corrupt-draft')).toBeInTheDocument()
    expect(
      within(
        (screen.getByText('org/corrupt-draft').closest('tr') as HTMLElement),
      ).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
    expect(downloadsApi.getHfDownload).not.toHaveBeenCalled()
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
  })

  it('does not start overlapping poll GETs when a response is slower than the poll interval', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/slow-poll' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-slow-poll',
        repository_id: 'org/slow-poll',
        status: 'DOWNLOADING',
        progress_percent: 5,
      }),
    )
    let resolveFirst!: (job: HfDownloadJob) => void
    const first = new Promise<HfDownloadJob>((resolve) => {
      resolveFirst = resolve
    })
    let callCount = 0
    let inflight = 0
    let maxInflight = 0
    let sawAbortDuringFirst = false
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(
      async (_jobId, signal) => {
        callCount += 1
        inflight += 1
        maxInflight = Math.max(maxInflight, inflight)
        try {
          if (callCount === 1) {
            const aborted = await Promise.race([
              first.then((job) => ({ kind: 'job' as const, job })),
              new Promise<{ kind: 'abort' }>((resolve) => {
                if (!signal) return
                if (signal.aborted) {
                  resolve({ kind: 'abort' })
                  return
                }
                signal.addEventListener(
                  'abort',
                  () => resolve({ kind: 'abort' }),
                  { once: true },
                )
              }),
            ])
            if (aborted.kind === 'abort') {
              sawAbortDuringFirst = true
              throw new DOMException('Aborted', 'AbortError')
            }
            return aborted.job
          }
          return makeJob({
            job_id: 'job-slow-poll',
            repository_id: 'org/slow-poll',
            status: 'DOWNLOADING',
            progress_percent: 70,
          })
        } finally {
          inflight -= 1
        }
      },
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/slow-poll')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalledTimes(1)
    })
    await new Promise((r) =>
      setTimeout(r, CATALOG_DOWNLOAD_POLL_MS + 400),
    )
    expect(downloadsApi.getHfDownload).toHaveBeenCalledTimes(1)
    expect(maxInflight).toBe(1)
    expect(sawAbortDuringFirst).toBe(false)

    resolveFirst(
      makeJob({
        job_id: 'job-slow-poll',
        repository_id: 'org/slow-poll',
        status: 'DOWNLOADING',
        progress_percent: 40,
      }),
    )
    await waitFor(
      () => {
        expect(screen.getByText(/진행률 40%/)).toBeInTheDocument()
      },
      { timeout: CATALOG_DOWNLOAD_POLL_MS + 2000 },
    )
    await waitFor(
      () => {
        expect(
          vi.mocked(downloadsApi.getHfDownload).mock.calls.length,
        ).toBeGreaterThan(1)
      },
      { timeout: CATALOG_DOWNLOAD_POLL_MS + 2000 },
    )
    expect(maxInflight).toBe(1)
  }, 15000)

  it('stops polling after terminal READY', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/terminal-poll' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-terminal',
        repository_id: 'org/terminal-poll',
        status: 'DOWNLOADING',
        progress_percent: 10,
      }),
    )
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-terminal',
        repository_id: 'org/terminal-poll',
        status: 'READY',
        local_path: '/data/hf/terminal',
        progress_percent: 100,
        size_bytes: 100,
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/terminal-poll')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    expect(await screen.findByText('/data/hf/terminal')).toBeInTheDocument()
    const callsAfterReady = vi.mocked(downloadsApi.getHfDownload).mock.calls
      .length
    await new Promise((r) =>
      setTimeout(r, CATALOG_DOWNLOAD_POLL_MS + 400),
    )
    expect(vi.mocked(downloadsApi.getHfDownload).mock.calls.length).toBe(
      callsAfterReady,
    )
  }, 10000)

  it('ignores late poll responses after unmount', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/late-unmount' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-late',
        repository_id: 'org/late-unmount',
        status: 'DOWNLOADING',
        progress_percent: 1,
      }),
    )
    let resolveLate!: (job: HfDownloadJob) => void
    const late = new Promise<HfDownloadJob>((resolve) => {
      resolveLate = resolve
    })
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(late)

    const view = renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/late-unmount')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    })
    view.unmount()
    resolveLate(
      makeJob({
        job_id: 'job-late',
        repository_id: 'org/late-unmount',
        status: 'DOWNLOADING',
        progress_percent: 99,
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText(/진행률 99%/)).not.toBeInTheDocument()
  })

  it('clears download tracking on Node change without applying prior Node poll', async () => {
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
        },
        {
          id: 'node-2',
          name: 'gpu-b',
          hostname: 'host-b',
          agent_base_url: 'http://host.docker.internal:8101',
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
        },
      ],
      page: 1,
      page_size: 100,
      total: 2,
    })
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/node-switch' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-node1',
        repository_id: 'org/node-switch',
        node_id: 'node-1',
        status: 'DOWNLOADING',
        progress_percent: 12,
      }),
    )
    let resolvePoll!: (job: HfDownloadJob) => void
    const pollPromise = new Promise<HfDownloadJob>((resolve) => {
      resolvePoll = resolve
    })
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(pollPromise)

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/node-switch')).closest('tr')
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: '다운로드' }),
    )
    await waitFor(() => {
      expect(downloadsApi.startHfDownload).toHaveBeenCalled()
    })

    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), 'node-2')
    await waitFor(() => {
      expect(
        within(
          (screen.getByText('org/node-switch').closest('tr') as HTMLElement),
        ).getByRole('button', { name: '다운로드' }),
      ).not.toBeDisabled()
    })
    resolvePoll(
      makeJob({
        job_id: 'job-node1',
        repository_id: 'org/node-switch',
        node_id: 'node-1',
        status: 'DOWNLOADING',
        progress_percent: 88,
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText(/진행률 88%/)).not.toBeInTheDocument()
    expect(screen.queryByText(/진행률 12%/)).not.toBeInTheDocument()
  })

  it('keeps Download locked after restore 503 until retry succeeds', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/restore-503' })]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-503',
      nodeId: 'node-1',
      repositoryId: 'org/restore-503',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    let allowRestore = false
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async () => {
      if (!allowRestore) {
        throw new ApiError('restore upstream unavailable', { status: 503 })
      }
      return makeJob({
        job_id: 'job-503',
        repository_id: 'org/restore-503',
        status: 'DOWNLOADING',
        progress_percent: 33,
      })
    })

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/restore-503')).closest(
      'tr',
    ) as HTMLElement
    await waitFor(() => {
      expect(
        screen.getByText(/다운로드 Job 복원 조회에 실패했습니다/),
      ).toBeInTheDocument()
      expect(screen.getByText(/restore upstream unavailable/)).toBeInTheDocument()
    })
    expect(
      within(row).getByRole('button', { name: '조회 실패' }),
    ).toBeDisabled()
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
    expect(
      sessionStorage.getItem(
        catalogDownloadDraftKey({
          nodeId: 'node-1',
          repositoryId: 'org/restore-503',
          requestedRevision: 'abc',
        }),
      ),
    ).toContain('job-503')

    allowRestore = true
    await userEvent.click(
      screen.getByRole('button', { name: '상태 다시 조회' }),
    )
    await waitFor(() => {
      expect(screen.getByText(/진행률 33%/)).toBeInTheDocument()
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
  })

  it('auto-reloads catalog on Node change and blocks Fit/Download on the stale list', async () => {
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
        },
        {
          id: 'node-2',
          name: 'gpu-b',
          hostname: 'host-b',
          agent_base_url: 'http://host.docker.internal:8101',
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
        },
      ],
      page: 1,
      page_size: 100,
      total: 2,
    })
    let resolveNode2!: (page: HfCatalogPage<HfCatalogModel>) => void
    const node2Promise = new Promise<HfCatalogPage<HfCatalogModel>>((resolve) => {
      resolveNode2 = resolve
    })
    vi.mocked(catalogApi.listHfCatalog).mockImplementation(async (params = {}) => {
      if (params.nodeId === 'node-2') return node2Promise
      return pageResult([
        makeItem({
          repository_id: 'org/node1-only',
          resource_fit: {
            result: 'FIT',
            reasons: [
              'GPU0 has spare VRAM on node-1 with a very long advisory reason that should not stretch the table forever',
            ],
          },
        }),
      ])
    })
    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({ repository_id: 'org/node1-only' }),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-stale-node',
        repository_id: 'org/node1-only',
        status: 'QUEUED',
      }),
    )

    renderAt('/models/catalog?q=llama&model_type=LLM&page=2&node_id=node-1')
    const row = (await screen.findByText('org/node1-only')).closest(
      'tr',
    ) as HTMLElement
    expect(
      within(row).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
    const reason = within(row).getByTitle(
      /GPU0 has spare VRAM on node-1 with a very long advisory reason/,
    )
    expect(reason.textContent).toContain('…')
    expect(reason.textContent?.length ?? 0).toBeLessThan(
      'GPU0 has spare VRAM on node-1 with a very long advisory reason that should not stretch the table forever'
        .length,
    )
    expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
      expect.objectContaining({
        q: 'llama',
        modelType: 'LLM',
        page: 2,
        nodeId: 'node-1',
      }),
    )

    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), 'node-2')
    expect(
      await screen.findByText(/서버 변경 적용 중/),
    ).toBeInTheDocument()
    expect(within(row).getByText('목록 갱신 중…')).toBeInTheDocument()
    expect(
      within(row).getByRole('button', { name: 'GPU 적합성 분석' }),
    ).toBeDisabled()
    expect(
      within(row).getByRole('button', { name: '다운로드' }),
    ).toBeDisabled()
    // Node change alone reloads catalog — keeps search/type, resets page=1.
    expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
      expect.objectContaining({
        q: 'llama',
        modelType: 'LLM',
        page: 1,
        nodeId: 'node-2',
      }),
    )

    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))
    await userEvent.click(within(row).getByRole('button', { name: '다운로드' }))
    expect(catalogApi.analyzeHfResourceFit).not.toHaveBeenCalled()
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()

    resolveNode2(
      pageResult([
        makeItem({
          repository_id: 'org/node2-only',
          resource_fit: { result: 'TIGHT', reasons: ['node-2 headroom'] },
        }),
      ]),
    )
    expect(await screen.findByText('org/node2-only')).toBeInTheDocument()
    expect(screen.queryByText('org/node1-only')).not.toBeInTheDocument()
    expect(screen.queryByText(/서버 변경 적용 중/)).not.toBeInTheDocument()
    expect(screen.getByText(/현재 조회 서버:/)).toBeInTheDocument()
    expect(
      within(
        (screen.getByText('org/node2-only').closest('tr') as HTMLElement),
      ).getByRole('button', { name: '다운로드' }),
    ).not.toBeDisabled()
  })

  it('ignores a late catalog response after a faster Node switch', async () => {
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
        },
        {
          id: 'node-2',
          name: 'gpu-b',
          hostname: 'host-b',
          agent_base_url: 'http://host.docker.internal:8101',
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
        },
      ],
      page: 1,
      page_size: 100,
      total: 2,
    })
    let resolveNode1Reload!: (page: HfCatalogPage<HfCatalogModel>) => void
    const node1Reload = new Promise<HfCatalogPage<HfCatalogModel>>((resolve) => {
      resolveNode1Reload = resolve
    })
    let node1Calls = 0
    vi.mocked(catalogApi.listHfCatalog).mockImplementation(async (params = {}) => {
      if (params.nodeId === 'node-2') {
        return pageResult([makeItem({ repository_id: 'org/fast-node2' })])
      }
      node1Calls += 1
      if (node1Calls === 1) {
        return pageResult([makeItem({ repository_id: 'org/initial-node1' })])
      }
      return node1Reload
    })

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/initial-node1')).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), '')
    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), 'node-1')
    await waitFor(() => {
      expect(node1Calls).toBeGreaterThan(1)
    })
    await userEvent.selectOptions(screen.getByLabelText(/대상 서버/), 'node-2')
    expect(await screen.findByText('org/fast-node2')).toBeInTheDocument()

    resolveNode1Reload(
      pageResult([makeItem({ repository_id: 'org/late-node1' })]),
    )
    await new Promise((r) => setTimeout(r, 30))
    expect(screen.queryByText('org/late-node1')).not.toBeInTheDocument()
    expect(screen.getByText('org/fast-node2')).toBeInTheDocument()
  })

  it('keeps Fit action button labels on a single line via nowrap class', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/nowrap' })]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({ repository_id: 'org/nowrap' }),
    )
    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/nowrap')).closest(
      'tr',
    ) as HTMLElement
    expect(row.querySelector('.catalog-row-actions')).not.toBeNull()
    await userEvent.click(within(row).getByRole('button', { name: 'GPU 적합성 분석' }))
    expect(await screen.findByTestId('fit-detail-org/nowrap')).toBeInTheDocument()
    const expand = within(row).getByRole('button', { name: '접기' })
    expect(expand.className).toContain('btn')
    expect(row.querySelector('.catalog-row-actions')).not.toBeNull()
  })

  it('links READY + cache READY jobs to the Deploy Wizard without POST', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/ready-cache' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-ready-cache',
        repository_id: 'org/ready-cache',
        status: 'READY',
        node_model_cache_id: 'cache-ready-42',
        cache_status: 'READY',
        local_path: '/data/hf/ready',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/ready-cache')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: '다운로드' }))
    const wizard = await screen.findByRole('link', { name: '배포 설정' })
    expect(wizard).toHaveAttribute(
      'href',
      '/models/cache/cache-ready-42/deploy',
    )
    expect(
      screen.queryByRole('link', { name: '캐시 목록 확인' }),
    ).not.toBeInTheDocument()
    expect(
      within(row).getAllByText(
        /파일 다운로드 완료 ≠ 모델 실행 중 ≠ API 연결 완료/,
      ).length,
    ).toBeGreaterThan(0)
    expect(
      within(row).getAllByText(/모델 파일 준비 완료/).length,
    ).toBeGreaterThan(0)

    expect(
      within(row).getByRole('button', { name: '파일 준비 완료' }),
    ).toBeDisabled()
    await userEvent.click(within(row).getByRole('button', { name: '파일 준비 완료' }))
    expect(downloadsApi.startHfDownload).toHaveBeenCalledTimes(1)
  })

  it('shows 캐시 목록 확인 when READY but cache is not confirmed READY', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/ready-nocache' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-ready-nocache',
        repository_id: 'org/ready-nocache',
        status: 'READY',
        node_model_cache_id: null,
        cache_status: null,
        local_path: '/data/hf/nocache',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/ready-nocache')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: '다운로드' }))
    expect(
      await screen.findByRole('link', { name: '캐시 목록 확인' }),
    ).toHaveAttribute('href', '/models/cache?node_id=node-1')
    expect(
      screen.queryByRole('link', { name: '배포 설정' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByText(/캐시가 아직 READY로 확인되지 않았습니다/),
    ).toBeInTheDocument()
  })

  it('restores READY wizard link after F5 without re-POSTing Download', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/f5-ready' })]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-f5-ready',
      nodeId: 'node-1',
      repositoryId: 'org/f5-ready',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-f5-ready',
        repository_id: 'org/f5-ready',
        status: 'READY',
        node_model_cache_id: 'cache-f5',
        cache_status: 'READY',
        local_path: '/data/hf/f5',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(
      await screen.findByRole('link', { name: '배포 설정' }),
    ).toHaveAttribute('href', '/models/cache/cache-f5/deploy')
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
    expect(
      screen.getByRole('button', { name: '파일 준비 완료' }),
    ).toBeDisabled()
  })

  it('does not offer wizard link for a READY job from a different node', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/wrong-node' })]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-wrong-node',
      nodeId: 'node-1',
      repositoryId: 'org/wrong-node',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-wrong-node',
        repository_id: 'org/wrong-node',
        node_id: 'node-other',
        status: 'READY',
        node_model_cache_id: 'cache-other',
        cache_status: 'READY',
        local_path: '/data/hf/other',
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/wrong-node')).toBeInTheDocument()
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    })
    expect(
      screen.queryByRole('link', { name: '배포 설정' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: '캐시 목록 확인' }),
    ).not.toBeInTheDocument()
  })

  it('blocks Download for later draft rows while earlier restores are still in flight', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([
        makeItem({ repository_id: 'org/first' }),
        makeItem({ repository_id: 'org/second' }),
      ]),
    )
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-first',
      nodeId: 'node-1',
      repositoryId: 'org/first',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    writeCatalogDownloadDraft(sessionStorage, {
      schema: 1,
      jobId: 'job-second',
      nodeId: 'node-1',
      repositoryId: 'org/second',
      requestedRevision: 'abc',
      savedAt: Date.now(),
    })
    let resolveFirst!: (job: HfDownloadJob) => void
    const firstPromise = new Promise<HfDownloadJob>((resolve) => {
      resolveFirst = resolve
    })
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async (jobId) => {
      if (jobId === 'job-first') return firstPromise
      return makeJob({
        job_id: 'job-second',
        repository_id: 'org/second',
        status: 'READY',
        local_path: '/data/hf/second',
      })
    })

    renderAt('/models/catalog?node_id=node-1')
    const firstRow = (await screen.findByText('org/first')).closest(
      'tr',
    ) as HTMLElement
    const secondRow = screen.getByText('org/second').closest('tr') as HTMLElement
    await waitFor(() => {
      expect(
        within(firstRow).getByRole('button', { name: '복원 중…' }),
      ).toBeDisabled()
      expect(
        within(secondRow).getByRole('button', { name: '복원 중…' }),
      ).toBeDisabled()
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
    expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
      'job-first',
      expect.anything(),
    )
    expect(downloadsApi.getHfDownload).not.toHaveBeenCalledWith(
      'job-second',
      expect.anything(),
    )

    resolveFirst(
      makeJob({
        job_id: 'job-first',
        repository_id: 'org/first',
        status: 'READY',
        local_path: '/data/hf/first',
      }),
    )
    await waitFor(() => {
      expect(screen.getByText('/data/hf/first')).toBeInTheDocument()
      expect(screen.getByText('/data/hf/second')).toBeInTheDocument()
    })
    expect(downloadsApi.startHfDownload).not.toHaveBeenCalled()
  })

  it('shows pending keyword hint until Search is pressed; other filters apply immediately', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/kw' })]),
    )
    renderAt('/models/catalog?node_id=node-1')
    expect(await screen.findByText('org/kw')).toBeInTheDocument()
    expect(
      screen.queryByText(/검색 버튼을 누르면 입력한 검색어가 적용됩니다/),
    ).not.toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('검색어'), 'qwen')
    expect(
      screen.getByText(/검색 버튼을 누르면 입력한 검색어가 적용됩니다/),
    ).toBeInTheDocument()
    expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
      expect.objectContaining({ q: null, nodeId: 'node-1' }),
    )

    await userEvent.click(screen.getByRole('button', { name: '검색' }))
    await waitFor(() => {
      expect(catalogApi.listHfCatalog).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'qwen', nodeId: 'node-1' }),
      )
    })
    expect(
      screen.queryByText(/검색 버튼을 누르면 입력한 검색어가 적용됩니다/),
    ).not.toBeInTheDocument()
  })

  it('shows progress-missing copy without inventing percent; poll failure is not download failure', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/prog' })]),
    )
    vi.mocked(downloadsApi.startHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-prog',
        repository_id: 'org/prog',
        status: 'DOWNLOADING',
        progress_percent: null,
        bytes_downloaded: null,
        total_bytes: null,
      }),
    )
    vi.mocked(downloadsApi.getHfDownload).mockRejectedValue(
      new ApiError('poll boom', { status: 503, code: 'DEPENDENCY_UNAVAILABLE' }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/prog')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: '다운로드' }))

    expect(
      await within(row).findByText(/다운로드 진행 중 \(자동 갱신\)/),
    ).toBeInTheDocument()
    expect(
      within(row).getByText(/진행률 정보 없음 · 상태는 자동 갱신 중/),
    ).toBeInTheDocument()
    expect(within(row).queryByText(/진행률 \d+%/)).not.toBeInTheDocument()

    await waitFor(() => {
      expect(
        within(row).getAllByText(/다운로드 상태 조회 실패/).length,
      ).toBeGreaterThan(0)
    })
    expect(
      within(row).queryByText((content, el) => {
        return (
          el?.classList.contains('catalog-download-status__headline--error') ===
            true && content === '다운로드 실패'
        )
      }),
    ).not.toBeInTheDocument()
    expect(
      row.querySelector('[data-download-state="failed"]'),
    ).toBeNull()
    expect(
      within(row).getByText(/다운로드 자체와 별개|다운로드 자체의 실패가 아닙니다/),
    ).toBeInTheDocument()
  })

  it('shows 정보 없음 for missing Fit GPU metrics instead of zero', async () => {
    vi.mocked(catalogApi.listHfCatalog).mockResolvedValue(
      pageResult([makeItem({ repository_id: 'org/sparse-fit' })]),
    )
    vi.mocked(catalogApi.analyzeHfResourceFit).mockResolvedValue(
      makeFitAnalysis({
        repository_id: 'org/sparse-fit',
        result: 'UNKNOWN',
        estimated_required_vram_mb: null,
        disk_free_mb: null,
        disk_ok: null,
        gpu_results: [
          {
            gpu_device_id: 'gpu-x',
            gpu_index: null,
            name: null,
            // Defensive UI path: treat absent telemetry as unknown, never as 0.
            vram_total_mb: null as unknown as number,
            vram_free_mb: null as unknown as number,
            safety_margin_mb: null as unknown as number,
            estimated_required_vram_mb: null,
            result: 'UNKNOWN',
            reasons: [],
          },
        ],
        suggested_gpu_device_ids: [],
        assumptions: [],
        warnings: [],
        reasons: ['Insufficient telemetry'],
      }),
    )

    renderAt('/models/catalog?node_id=node-1')
    const row = (await screen.findByText('org/sparse-fit')).closest(
      'tr',
    ) as HTMLElement
    await userEvent.click(
      within(row).getByRole('button', { name: 'GPU 적합성 분석' }),
    )
    const detail = await screen.findByTestId('fit-detail-org/sparse-fit')
    expect(within(detail).getAllByText('정보 없음').length).toBeGreaterThan(2)
    expect(
      within(detail).getAllByText(/판단 정보 부족/).length,
    ).toBeGreaterThan(0)
    expect(within(detail).queryByText('0 MiB')).not.toBeInTheDocument()
  })
})
