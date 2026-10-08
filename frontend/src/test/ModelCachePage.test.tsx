import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { ModelCacheEntry, NodeSummary, Paginated } from '../api/types'
import { ModelCachePage } from '../pages/ModelCachePage'
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
})
