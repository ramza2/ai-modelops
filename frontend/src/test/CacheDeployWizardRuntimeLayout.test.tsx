import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type {
  ModelCacheEntry,
  ModelSummary,
  ModelVersion,
  NodeDetail,
} from '../api/types'
import { CacheDeployWizardPage } from '../pages/CacheDeployWizardPage'
import * as cacheDeployApi from '../api/cacheDeploy'
import * as modelsApi from '../api/models'
import * as nodesApi from '../api/nodes'

function makeCache(): ModelCacheEntry {
  return {
    id: 'cache-1',
    node_id: 'node-1',
    node_name: 'gpu-node',
    model_artifact_id: 'art-1',
    repository_id: 'org/very-long-served-model-name-for-layout',
    resolved_revision: 'abc123',
    model_id: 'model-1',
    model_slug: 'demo-model',
    model_version_id: 'ver-1',
    status: 'READY',
    local_path: '/data/modelops/models/org/demo/abc123',
    size_bytes: 1000,
    error_message: null,
    prepared_at: '2026-10-01T00:00:00Z',
    last_verified_at: '2026-10-01T00:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    is_deployment: false,
    note: null,
  }
}

function makeVersion(): ModelVersion {
  return {
    id: 'ver-1',
    model_id: 'model-1',
    version_label: '1.0.0',
    source_repository: 'org/demo',
    source_revision: 'abc123',
    quantization: null,
    dtype: null,
    runtime_type: 'VLLM',
    runtime_image: 'example/runtime:tag',
    runtime_image_digest: null,
    served_model_name: 'org/very-long-served-model-name-for-layout',
    expected_idle_vram_mb: null,
    expected_peak_vram_mb: null,
    default_max_model_len: null,
    runtime_config: {},
    archived_at: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  }
}

function makeModel(): ModelSummary {
  return {
    id: 'model-1',
    slug: 'demo-model',
    name: 'Demo',
    model_type: 'LLM',
    provider: null,
    source_type: 'HUGGINGFACE',
    license_name: null,
    description: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  }
}

function makeNode(): NodeDetail {
  return {
    id: 'node-1',
    name: 'gpu-node',
    hostname: 'host-a',
    agent_base_url: 'http://agent-placeholder:8100',
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
    gpus: [
      {
        id: 'gpu-0',
        node_id: 'node-1',
        gpu_uuid: 'GPU-0',
        device_index: 0,
        model_name: 'Mock GPU',
        vram_total_mb: 16384,
        compute_capability: null,
        safety_margin_mb: 1024,
        status: 'AVAILABLE',
        last_seen_at: null,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-01T00:00:00Z',
      },
    ],
  }
}

describe('CacheDeployWizardPage Runtime layout (P1-5A)', () => {
  beforeEach(() => {
    vi.spyOn(cacheDeployApi, 'getCacheEntry').mockResolvedValue(makeCache())
    vi.spyOn(modelsApi, 'getModelVersion').mockResolvedValue(makeVersion())
    vi.spyOn(modelsApi, 'getModel').mockResolvedValue(makeModel())
    vi.spyOn(nodesApi, 'getNode').mockResolvedValue(makeNode())
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders Runtime fields in form-grid without changing control labels or types', async () => {
    render(
      <MemoryRouter
        initialEntries={['/models/cache/cache-1/deploy?step=runtime']}
      >
        <Routes>
          <Route
            path="/models/cache/:cacheId/deploy"
            element={<CacheDeployWizardPage />}
          />
        </Routes>
      </MemoryRouter>,
    )

    expect(
      await screen.findByRole('heading', { level: 2, name: '2. Runtime' }),
    ).toBeInTheDocument()

    const grid = document.querySelector('.form-grid')
    expect(grid).not.toBeNull()
    expect(grid?.querySelectorAll('label').length).toBeGreaterThanOrEqual(10)
    expect(grid?.querySelectorAll('.form-grid__wide').length).toBe(3)

    expect(screen.getByText('max_model_len')).toBeInTheDocument()
    expect(screen.getByText('max_num_seqs')).toBeInTheDocument()
    expect(screen.getByText('gpu_memory_utilization')).toBeInTheDocument()
    expect(screen.getByText('Served model name')).toBeInTheDocument()
    const parser = screen.getByLabelText('Reasoning Parser')
    expect(parser).toHaveValue('')
    expect(
      screen.queryByText(/Reasoning Parser는 Runtime 시작 옵션/),
    ).not.toBeInTheDocument()

    const numberInputs = grid?.querySelectorAll('input[type="number"]')
    expect(numberInputs?.length).toBeGreaterThanOrEqual(4)
    const served = screen.getByDisplayValue(
      'org/very-long-served-model-name-for-layout',
    )
    expect(served.tagName).toBe('INPUT')
    expect(served.closest('label')).toHaveClass('form-grid__wide')

    await userEvent.selectOptions(parser, 'qwen3')
    expect(parser).toHaveValue('qwen3')
    expect(
      screen.getByText(/Reasoning Parser는 Runtime 시작 옵션/),
    ).toBeInTheDocument()
  })
})
