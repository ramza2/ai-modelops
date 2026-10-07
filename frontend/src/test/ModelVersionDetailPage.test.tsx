import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  ModelArtifact,
  ModelDetail,
  ModelVersion,
  Paginated,
} from '../api/types'
import { ModelVersionDetailPage } from '../pages/ModelVersionDetailPage'
import * as modelsApi from '../api/models'

function formatIntMatcher(n: number): string {
  return new Intl.NumberFormat().format(n)
}

function makeModel(overrides: Partial<ModelDetail> = {}): ModelDetail {
  return {
    id: 'm1',
    slug: 'chat-a',
    name: 'Chat A',
    model_type: 'LLM',
    provider: 'example',
    source_type: 'HF',
    license_name: null,
    description: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makeVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
  return {
    id: 'v1',
    model_id: 'm1',
    version_label: '1.0.0',
    source_repository: 'example/model',
    source_revision: 'revision-placeholder',
    quantization: 'AWQ',
    dtype: 'auto',
    runtime_type: 'VLLM',
    runtime_image: 'example/runtime:tag',
    runtime_image_digest: 'sha256:abcdef0123456789deadbeef',
    served_model_name: 'served',
    expected_idle_vram_mb: 8192,
    expected_peak_vram_mb: 16384,
    default_max_model_len: 8192,
    runtime_config: {
      max_model_len: 16384,
      max_num_seqs: 4,
      tensor_parallel_size: 2,
      gpu_memory_utilization: 0.9,
      dtype: 'float16',
      quantization: 'other-value',
      scheduling_policy: 'priority',
      secret_or_unknown: 'must-not-render',
    },
    archived_at: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makeArtifact(
  overrides: Partial<ModelArtifact> & Pick<ModelArtifact, 'id'>,
): ModelArtifact {
  return {
    model_version_id: 'v1',
    artifact_type: 'WEIGHTS',
    source_uri: 'hf://example/model',
    revision: 'revision-placeholder',
    checksum: 'abcdef1234567890zzzz',
    size_bytes: 1048576,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function pageResult(
  items: ModelArtifact[],
  opts: { page?: number; total?: number } = {},
): Paginated<ModelArtifact> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    total: opts.total ?? items.length,
  }
}

function renderDetail(path = '/model-versions/v1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/model-versions/:versionId"
          element={<ModelVersionDetailPage />}
        />
        <Route path="/models/:modelId" element={<div>Model detail</div>} />
        <Route path="/models" element={<div>Models list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ModelVersionDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.spyOn(modelsApi, 'getModel')
    vi.spyOn(modelsApi, 'listModelArtifacts')
    vi.spyOn(modelsApi, 'listModels')
    vi.spyOn(modelsApi, 'listModelVersions')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders identity, parent nav, expected resources, and allowlisted runtime config', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))

    renderDetail()

    expect(
      await screen.findByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Chat A' })).toHaveAttribute(
      'href',
      '/models/m1',
    )
    expect(screen.getByText('8.0 GiB')).toBeInTheDocument()
    expect(screen.getByText('16.0 GiB')).toBeInTheDocument()
    expect(screen.getByText(formatIntMatcher(8192))).toBeInTheDocument()
    expect(screen.getByText('16384')).toBeInTheDocument()
    expect(screen.getByText('runtime_config.max_model_len')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('0.9')).toBeInTheDocument()
    expect(screen.getByText('priority')).toBeInTheDocument()
    expect(screen.getByText('float16')).toBeInTheDocument()
    expect(screen.getByText('other-value')).toBeInTheDocument()
    // top-level dtype/quant still visible separately
    expect(screen.getAllByText('auto').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('AWQ').length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText('must-not-render')).not.toBeInTheDocument()
    expect(screen.getByText(/기타 설정 1개/)).toBeInTheDocument()
  })

  it('uses definition wording and avoids effective/actual labels', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(
      await screen.findByRole('heading', {
        level: 2,
        name: 'Version Runtime Configuration',
      }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/저장된 정의값/).length).toBeGreaterThan(0)
    expect(
      screen.getAllByText(/Deployment Capacity Profile/).length,
    ).toBeGreaterThan(0)
    expect(screen.queryByText(/Effective Runtime Config/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Actual Runtime Config/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Current Runtime Config/i)).not.toBeInTheDocument()
  })

  it('handles malformed runtime config values safely', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({
        runtime_config: {
          max_num_seqs: { nested: true },
          gpu_memory_utilization: Number.NaN,
          scheduling_policy: 'fcfs',
        },
      }),
    )
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(await screen.findByText('유효하지 않은 복합 값')).toBeInTheDocument()
    expect(screen.getByText('유효하지 않은 값')).toBeInTheDocument()
    expect(screen.getByText('fcfs')).toBeInTheDocument()
  })

  it('shows archive state and artifact table/empty/error', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({ archived_at: '2026-09-01T00:00:00Z' }),
    )
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(
      pageResult([makeArtifact({ id: 'a1' })]),
    )
    renderDetail()
    expect(await screen.findByText('Archived')).toBeInTheDocument()
    expect(screen.getByText('WEIGHTS')).toBeInTheDocument()
    expect(screen.getByText('1.0 MiB')).toBeInTheDocument()
    cleanup()

    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(
      await screen.findByText('등록된 Artifact가 없습니다.'),
    ).toBeInTheDocument()
    cleanup()

    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockRejectedValue(
      new ApiError('artifacts down', { status: 503 }),
    )
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()
    expect(screen.getByText('artifacts down')).toBeInTheDocument()
  })

  it('keeps Version visible when parent Model fails', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockRejectedValue(
      new ApiError('parent fail', { status: 500 }),
    )
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()
    expect(screen.getByText('parent fail')).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: /Model m1|Model [a-f0-9]/i }),
    ).toBeInTheDocument()
  })

  it('shows Version 404 with back link', async () => {
    vi.mocked(modelsApi.getModelVersion).mockRejectedValue(
      new ApiError('gone', { status: 404 }),
    )
    vi.mocked(modelsApi.listModelArtifacts).mockRejectedValue(
      new ApiError('gone', { status: 404 }),
    )
    renderDetail()
    expect(
      await screen.findByText('Model Version을 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: '← Models / Versions' }),
    ).toHaveAttribute('href', '/models')
  })

  it('keeps stale Version and shows refresh error on Version GET 500', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.getModelVersion)
      .mockResolvedValueOnce(makeVersion())
      .mockRejectedValueOnce(new ApiError('version refresh boom', { status: 500 }))
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(
      pageResult([makeArtifact({ id: 'a1' })]),
    )
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Chat A' })).toBeInTheDocument()
    expect(screen.getByText('WEIGHTS')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText(/기존 Version 정보를 표시하고 있습니다/)).toBeInTheDocument()
    })
    expect(screen.getByText(/version refresh boom/)).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Chat A' })).toBeInTheDocument()
    expect(screen.getByText('WEIGHTS')).toBeInTheDocument()
    expect(
      screen.queryByText('Model Version을 찾을 수 없습니다.'),
    ).not.toBeInTheDocument()
  })

  it('clears stale Version and shows not-found on Version GET 404 refresh', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.getModelVersion)
      .mockResolvedValueOnce(makeVersion())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts)
      .mockResolvedValueOnce(pageResult([makeArtifact({ id: 'a1' })]))
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: '1.0.0' }),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('Model Version을 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: '1.0.0' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('WEIGHTS')).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: '← Models / Versions' }),
    ).toHaveAttribute('href', '/models')
  })

  it('does not call Capacity Profile, Deployments, or runtime observation', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(modelsApi.listModelArtifacts).mockResolvedValue(pageResult([]))
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: '1.0.0' })
    expect(modelsApi.listModels).not.toHaveBeenCalled()
    expect(modelsApi.listModelVersions).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(modelsApi.getModel).toHaveBeenCalled()
    })
  })
})
