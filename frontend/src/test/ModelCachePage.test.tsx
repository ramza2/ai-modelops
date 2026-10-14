import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  HfDownloadJob,
  ModelCacheEntry,
  NodeSummary,
  Paginated,
} from '../api/types'
import {
  CACHE_DOWNLOAD_POLL_MS,
  ModelCachePage,
} from '../pages/ModelCachePage'
import * as downloadsApi from '../api/downloads'
import * as nodesApi from '../api/nodes'

function cachePage(
  items: ModelCacheEntry[],
  total?: number,
): Paginated<ModelCacheEntry> {
  return {
    items,
    page: 1,
    page_size: 20,
    total: total ?? items.length,
  }
}

function makeCache(
  overrides: Partial<ModelCacheEntry> & Pick<ModelCacheEntry, 'id'>,
): ModelCacheEntry {
  return {
    node_id: 'node-1',
    node_name: 'gpu-a',
    model_artifact_id: 'art-1',
    repository_id: 'org/model',
    resolved_revision: 'abc123',
    model_id: null,
    model_slug: null,
    model_version_id: null,
    status: 'READY',
    local_path: '/data/models/org-model',
    size_bytes: 1_000_000,
    error_message: null,
    prepared_at: '2026-10-01T00:00:00Z',
    last_verified_at: '2026-10-01T00:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-02T00:00:00Z',
    is_deployment: false,
    note: 'Downloaded cache is not a running deployment.',
    active_download_job_id: null,
    ...overrides,
  }
}

function makeJob(
  overrides: Partial<HfDownloadJob> & Pick<HfDownloadJob, 'job_id' | 'status'>,
): HfDownloadJob {
  return {
    agent_job_id: 'agent-1',
    node_id: 'node-1',
    model_artifact_id: 'art-1',
    node_model_cache_id: 'cache-prep',
    repository_id: 'org/model',
    requested_revision: 'main',
    resolved_revision: 'abc123',
    bytes_downloaded: 500,
    total_bytes: 1000,
    progress_percent: 50,
    local_path: null,
    error_code: null,
    error_message: null,
    cache_status: 'PREPARING',
    size_bytes: null,
    source_uri: 'hf://org/model',
    created_at: '2026-10-01T00:00:00Z',
    started_at: '2026-10-01T00:00:01Z',
    finished_at: null,
    updated_at: '2026-10-01T00:00:02Z',
    ...overrides,
  }
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/models/cache" element={<ModelCachePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ModelCachePage', () => {
  beforeEach(() => {
    vi.spyOn(downloadsApi, 'listModelCaches')
    vi.spyOn(downloadsApi, 'purgeModelCache')
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
        } satisfies NodeSummary,
      ],
      page: 1,
      page_size: 100,
      total: 2,
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('lists caches and purges after confirmation', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([makeCache({ id: 'cache-1' })]),
    )
    vi.mocked(downloadsApi.purgeModelCache).mockResolvedValue({
      id: 'cache-1',
      status: 'MISSING',
      purged: true,
      repository_id: 'org/model',
      resolved_revision: 'abc123',
      registry_retained: true,
    })

    renderAt('/models/cache')
    expect(await screen.findByText('org/model')).toBeInTheDocument()
    expect(
      screen.getByText(/Downloaded ≠ Deployed ≠ Published/),
    ).toBeInTheDocument()
    const deploy = screen.getByRole('link', { name: 'Deploy' })
    expect(deploy).toHaveAttribute('href', '/models/cache/cache-1/deploy')

    await userEvent.click(screen.getByRole('button', { name: 'Purge' }))
    expect(
      await screen.findByRole('dialog', { name: /Purge 확인/i }),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Purge 확인' }))
    await waitFor(() => {
      expect(downloadsApi.purgeModelCache).toHaveBeenCalledWith('cache-1')
    })
  })

  it('disables Deploy for non-READY caches', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([makeCache({ id: 'cache-prep', status: 'PREPARING' })]),
    )
    renderAt('/models/cache')
    const deploy = await screen.findByRole('link', { name: 'Deploy' })
    expect(deploy).toHaveAttribute('aria-disabled', 'true')
    expect(deploy).toHaveAttribute(
      'title',
      'Deploy requires READY cache',
    )
  })

  it('shows purge error and keeps dialog open', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([makeCache({ id: 'cache-2' })]),
    )
    vi.mocked(downloadsApi.purgeModelCache).mockRejectedValue(
      new ApiError('Cache in use', { status: 409, code: 'CONFLICT' }),
    )

    renderAt('/models/cache')
    const row = (await screen.findByText('org/model')).closest('tr')
    expect(row).not.toBeNull()
    await userEvent.click(
      within(row as HTMLElement).getByRole('button', { name: 'Purge' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Purge 확인' }))
    expect(await screen.findByText('Cache in use')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('polls PREPARING active job then refreshes list to READY', async () => {
    let resolveJob!: (job: HfDownloadJob) => void
    const pendingJob = new Promise<HfDownloadJob>((resolve) => {
      resolveJob = resolve
    })
    let listCalls = 0
    const preparing = cachePage([
      makeCache({
        id: 'cache-prep',
        status: 'PREPARING',
        local_path: null,
        active_download_job_id: 'job-1',
        size_bytes: null,
      }),
    ])
    const ready = cachePage([
      makeCache({
        id: 'cache-prep',
        status: 'READY',
        active_download_job_id: null,
        local_path: '/data/models/org-model',
      }),
    ])
    vi.mocked(downloadsApi.listModelCaches).mockImplementation(async () => {
      listCalls += 1
      // initial load + first poll re-list stay PREPARING so progress can render
      return listCalls <= 2 ? preparing : ready
    })
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(pendingJob)

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
        'job-1',
        expect.any(AbortSignal),
      )
    })

    resolveJob(
      makeJob({
        job_id: 'job-1',
        status: 'DOWNLOADING',
        progress_percent: 40,
        node_model_cache_id: 'cache-prep',
      }),
    )
    expect(
      await screen.findByTestId('cache-download-progress-cache-prep'),
    ).toHaveTextContent('진행률 40%')

    await waitFor(
      () => {
        expect(screen.getByTitle('READY')).toBeInTheDocument()
      },
      { timeout: CACHE_DOWNLOAD_POLL_MS + 2000 },
    )
    const deploy = screen.getByRole('link', { name: 'Deploy' })
    expect(deploy).not.toHaveAttribute('aria-disabled', 'true')
  })

  it('reflects PREPARING → FAILED from list after job sync', async () => {
    let resolveJob!: (job: HfDownloadJob) => void
    const pendingJob = new Promise<HfDownloadJob>((resolve) => {
      resolveJob = resolve
    })
    vi.mocked(downloadsApi.listModelCaches)
      .mockResolvedValueOnce(
        cachePage([
          makeCache({
            id: 'cache-fail',
            status: 'PREPARING',
            active_download_job_id: 'job-fail',
            local_path: null,
          }),
        ]),
      )
      .mockResolvedValue(
        cachePage([
          makeCache({
            id: 'cache-fail',
            status: 'FAILED',
            active_download_job_id: null,
            error_message: 'disk full',
            local_path: null,
          }),
        ]),
      )
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(pendingJob)

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    resolveJob(
      makeJob({ job_id: 'job-fail', status: 'FAILED', error_message: 'disk full' }),
    )
    await waitFor(() => {
      expect(screen.getByTitle('FAILED')).toBeInTheDocument()
    })
    expect(screen.getByText('disk full')).toBeInTheDocument()
  })

  it('ignores stale poll responses after Node filter change', async () => {
    let resolveLate!: (job: HfDownloadJob) => void
    const late = new Promise<HfDownloadJob>((resolve) => {
      resolveLate = resolve
    })
    vi.mocked(downloadsApi.listModelCaches).mockImplementation(
      async (params = {}) => {
        if (params.nodeId === 'node-2') {
          return cachePage([
            makeCache({
              id: 'cache-b',
              node_id: 'node-2',
              node_name: 'gpu-b',
              repository_id: 'org/other',
              status: 'READY',
            }),
          ])
        }
        return cachePage([
          makeCache({
            id: 'cache-prep',
            status: 'PREPARING',
            active_download_job_id: 'job-stale',
            local_path: null,
          }),
        ])
      },
    )
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(late)

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    })

    await userEvent.selectOptions(screen.getByLabelText('Node'), 'node-2')
    expect(await screen.findByText('org/other')).toBeInTheDocument()
    expect(screen.queryByTitle('PREPARING')).not.toBeInTheDocument()

    resolveLate(
      makeJob({
        job_id: 'job-stale',
        status: 'DOWNLOADING',
        progress_percent: 99,
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('진행률 99%')).not.toBeInTheDocument()
    expect(screen.getByText('org/other')).toBeInTheDocument()
  })

  it('does not overlap poll cycles while a slow getHfDownload is in flight', async () => {
    let resolveFirst!: (job: HfDownloadJob) => void
    const first = new Promise<HfDownloadJob>((resolve) => {
      resolveFirst = resolve
    })
    let getCalls = 0
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-slow',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async () => {
      getCalls += 1
      if (getCalls === 1) return first
      return makeJob({ job_id: 'job-slow', status: 'DOWNLOADING' })
    })

    renderAt('/models/cache')
    await waitFor(() => {
      expect(getCalls).toBe(1)
    })
    await new Promise((r) => setTimeout(r, CACHE_DOWNLOAD_POLL_MS + 400))
    expect(getCalls).toBe(1)

    resolveFirst(makeJob({ job_id: 'job-slow', status: 'DOWNLOADING' }))
    await waitFor(
      () => {
        expect(getCalls).toBeGreaterThanOrEqual(2)
      },
      { timeout: CACHE_DOWNLOAD_POLL_MS + 2000 },
    )
  }, 15000)

  it('stops polling once the cache leaves PREPARING', async () => {
    let resolveJob!: (job: HfDownloadJob) => void
    const pendingJob = new Promise<HfDownloadJob>((resolve) => {
      resolveJob = resolve
    })
    vi.mocked(downloadsApi.listModelCaches)
      .mockResolvedValueOnce(
        cachePage([
          makeCache({
            id: 'cache-prep',
            status: 'PREPARING',
            active_download_job_id: 'job-done',
            local_path: null,
          }),
        ]),
      )
      .mockResolvedValue(
        cachePage([
          makeCache({
            id: 'cache-prep',
            status: 'READY',
            active_download_job_id: null,
          }),
        ]),
      )
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(pendingJob)

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    resolveJob(makeJob({ job_id: 'job-done', status: 'READY' }))
    await waitFor(() => {
      expect(screen.getByTitle('READY')).toBeInTheDocument()
    })
    const callsAfterReady = vi.mocked(downloadsApi.getHfDownload).mock.calls.length
    await new Promise((r) => setTimeout(r, CACHE_DOWNLOAD_POLL_MS + 400))
    expect(vi.mocked(downloadsApi.getHfDownload).mock.calls.length).toBe(
      callsAfterReady,
    )
  })

  it('restores PREPARING job tracking after F5 via list active_download_job_id', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-restore',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-restore',
        status: 'DOWNLOADING',
        progress_percent: 12,
      }),
    )

    renderAt('/models/cache')
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
        'job-restore',
        expect.any(AbortSignal),
      )
    })
    expect(
      await screen.findByTestId('cache-download-progress-cache-prep'),
    ).toHaveTextContent('진행률 12%')
  })

  it('keeps last good rows on poll failure and recovers via 재조회', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-err',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload)
      .mockRejectedValueOnce(
        new ApiError('temporary', { status: 503, code: 'UNAVAILABLE' }),
      )
      .mockResolvedValue(
        makeJob({
          job_id: 'job-err',
          status: 'DOWNLOADING',
          progress_percent: 33,
        }),
      )

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    expect(
      await screen.findByText(/다운로드 상태 갱신에 실패/),
    ).toBeInTheDocument()
    expect(screen.getByTitle('PREPARING')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '다시 시도' }))
    await waitFor(() => {
      expect(
        screen.queryByText(/다운로드 상태 갱신에 실패/),
      ).not.toBeInTheDocument()
    })
  })

  it('does not invent READY when active job is 404', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-missing',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockRejectedValue(
      new ApiError('not found', { status: 404, code: 'NOT_FOUND' }),
    )

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    expect(
      await screen.findByText(/READY로 추정하지 않습니다/),
    ).toBeInTheDocument()
    expect(screen.queryByTitle('READY')).not.toBeInTheDocument()
  })

  it('resumes poll after purging a READY row while another PREPARING job remains', async () => {
    const preparing = makeCache({
      id: 'cache-prep',
      repository_id: 'org/preparing',
      status: 'PREPARING',
      active_download_job_id: 'job-keep',
      local_path: null,
    })
    const ready = makeCache({
      id: 'cache-ready',
      repository_id: 'org/ready',
      status: 'READY',
      active_download_job_id: null,
    })
    let purged = false
    vi.mocked(downloadsApi.listModelCaches).mockImplementation(async () =>
      cachePage(purged ? [preparing] : [preparing, ready]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({
        job_id: 'job-keep',
        status: 'DOWNLOADING',
        progress_percent: 10,
        node_model_cache_id: 'cache-prep',
      }),
    )
    vi.mocked(downloadsApi.purgeModelCache).mockImplementation(async () => {
      purged = true
      return {
        id: 'cache-ready',
        status: 'MISSING',
        purged: true,
        repository_id: 'org/ready',
        resolved_revision: 'abc123',
        registry_retained: true,
      }
    })

    renderAt('/models/cache')
    expect(await screen.findByText('org/preparing')).toBeInTheDocument()
    expect(await screen.findByText('org/ready')).toBeInTheDocument()
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalledWith(
        'job-keep',
        expect.any(AbortSignal),
      )
    })
    const callsBeforePurge = vi.mocked(downloadsApi.getHfDownload).mock.calls
      .length

    const readyRow = screen.getByText('org/ready').closest('tr')
    expect(readyRow).not.toBeNull()
    await userEvent.click(
      within(readyRow as HTMLElement).getByRole('button', { name: 'Purge' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Purge 확인' }))
    await waitFor(() => {
      expect(downloadsApi.purgeModelCache).toHaveBeenCalledWith('cache-ready')
    })
    await waitFor(() => {
      expect(screen.queryByText('org/ready')).not.toBeInTheDocument()
    })

    await waitFor(
      () => {
        expect(
          vi.mocked(downloadsApi.getHfDownload).mock.calls.length,
        ).toBeGreaterThan(callsBeforePurge)
      },
      { timeout: CACHE_DOWNLOAD_POLL_MS + 2000 },
    )
  }, 15000)

  it('resumes poll after Purge failure without changing signature', async () => {
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-resume',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({ job_id: 'job-resume', status: 'DOWNLOADING', progress_percent: 5 }),
    )
    vi.mocked(downloadsApi.purgeModelCache).mockRejectedValue(
      new ApiError('busy', { status: 409, code: 'CONFLICT' }),
    )

    renderAt('/models/cache')
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    })
    const callsBefore = vi.mocked(downloadsApi.getHfDownload).mock.calls.length

    await userEvent.click(screen.getByRole('button', { name: 'Purge' }))
    await userEvent.click(screen.getByRole('button', { name: 'Purge 확인' }))
    expect(await screen.findByText('busy')).toBeInTheDocument()

    await waitFor(
      () => {
        expect(
          vi.mocked(downloadsApi.getHfDownload).mock.calls.length,
        ).toBeGreaterThan(callsBefore)
      },
      { timeout: CACHE_DOWNLOAD_POLL_MS + 2000 },
    )
  }, 15000)

  it('aborts slow poll work when Purge starts and Node filter changes', async () => {
    let resolveSlow!: (job: HfDownloadJob) => void
    const slow = new Promise<HfDownloadJob>((resolve) => {
      resolveSlow = resolve
    })
    vi.mocked(downloadsApi.listModelCaches).mockImplementation(
      async (params = {}) => {
        if (params.nodeId === 'node-2') {
          return cachePage([
            makeCache({
              id: 'cache-b',
              node_id: 'node-2',
              node_name: 'gpu-b',
              repository_id: 'org/other',
              status: 'READY',
            }),
          ])
        }
        return cachePage([
          makeCache({
            id: 'cache-prep',
            status: 'PREPARING',
            active_download_job_id: 'job-slow-purge',
            local_path: null,
          }),
          makeCache({
            id: 'cache-ready',
            repository_id: 'org/ready-purge',
            status: 'READY',
          }),
        ])
      },
    )
    vi.mocked(downloadsApi.getHfDownload).mockReturnValue(slow)
    vi.mocked(downloadsApi.purgeModelCache).mockResolvedValue({
      id: 'cache-ready',
      status: 'MISSING',
      purged: true,
      repository_id: 'org/ready-purge',
      resolved_revision: 'abc123',
      registry_retained: true,
    })

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    await waitFor(() => {
      expect(downloadsApi.getHfDownload).toHaveBeenCalled()
    })

    const readyRow = screen.getByText('org/ready-purge').closest('tr')
    await userEvent.click(
      within(readyRow as HTMLElement).getByRole('button', { name: 'Purge' }),
    )
    await userEvent.click(screen.getByRole('button', { name: 'Purge 확인' }))
    await waitFor(() => {
      expect(downloadsApi.purgeModelCache).toHaveBeenCalled()
    })

    await userEvent.selectOptions(screen.getByLabelText('Node'), 'node-2')
    expect(await screen.findByText('org/other')).toBeInTheDocument()

    resolveSlow(
      makeJob({
        job_id: 'job-slow-purge',
        status: 'DOWNLOADING',
        progress_percent: 88,
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('진행률 88%')).not.toBeInTheDocument()
    expect(screen.getByText('org/other')).toBeInTheDocument()
  })

  it('ignores older poll list when a newer manual Refresh finishes first', async () => {
    let resolvePollList!: (page: Paginated<ModelCacheEntry>) => void
    const pollList = new Promise<Paginated<ModelCacheEntry>>((resolve) => {
      resolvePollList = resolve
    })
    let listCalls = 0
    vi.mocked(downloadsApi.listModelCaches).mockImplementation(async () => {
      listCalls += 1
      if (listCalls === 1) {
        return cachePage([
          makeCache({
            id: 'cache-prep',
            status: 'PREPARING',
            active_download_job_id: 'job-race',
            local_path: null,
            size_bytes: null,
          }),
        ])
      }
      if (listCalls === 2) {
        // poll re-list — hold until after manual refresh applies
        return pollList
      }
      return cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'READY',
          active_download_job_id: null,
          local_path: '/data/models/org-model',
          size_bytes: 2_000_000,
        }),
      ])
    })
    vi.mocked(downloadsApi.getHfDownload).mockResolvedValue(
      makeJob({ job_id: 'job-race', status: 'DOWNLOADING', progress_percent: 20 }),
    )

    renderAt('/models/cache')
    expect(await screen.findByTitle('PREPARING')).toBeInTheDocument()
    await waitFor(() => {
      expect(listCalls).toBeGreaterThanOrEqual(2)
    })

    await userEvent.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByTitle('READY')).toBeInTheDocument()
    })

    resolvePollList(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-race',
          local_path: null,
          size_bytes: null,
        }),
      ]),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByTitle('READY')).toBeInTheDocument()
    expect(screen.queryByTitle('PREPARING')).not.toBeInTheDocument()
  })

  it('keeps a single in-flight poll cycle under long-running getHfDownload', async () => {
    let resolveFirst!: (job: HfDownloadJob) => void
    const first = new Promise<HfDownloadJob>((resolve) => {
      resolveFirst = resolve
    })
    let getCalls = 0
    let maxConcurrent = 0
    let concurrent = 0
    vi.mocked(downloadsApi.listModelCaches).mockResolvedValue(
      cachePage([
        makeCache({
          id: 'cache-prep',
          status: 'PREPARING',
          active_download_job_id: 'job-single',
          local_path: null,
        }),
      ]),
    )
    vi.mocked(downloadsApi.getHfDownload).mockImplementation(async () => {
      getCalls += 1
      concurrent += 1
      maxConcurrent = Math.max(maxConcurrent, concurrent)
      try {
        if (getCalls === 1) return first
        return makeJob({ job_id: 'job-single', status: 'DOWNLOADING' })
      } finally {
        concurrent -= 1
      }
    })

    renderAt('/models/cache')
    await waitFor(() => {
      expect(getCalls).toBe(1)
    })
    await new Promise((r) => setTimeout(r, CACHE_DOWNLOAD_POLL_MS * 2 + 200))
    expect(getCalls).toBe(1)
    expect(maxConcurrent).toBe(1)

    resolveFirst(makeJob({ job_id: 'job-single', status: 'DOWNLOADING' }))
    await waitFor(
      () => {
        expect(getCalls).toBeGreaterThanOrEqual(2)
      },
      { timeout: CACHE_DOWNLOAD_POLL_MS + 2000 },
    )
    expect(maxConcurrent).toBe(1)
  }, 20000)
})
