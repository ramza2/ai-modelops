import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type {
  Deployment,
  ModelCacheEntry,
  ModelSummary,
  ModelVersion,
  NodeDetail,
  OperationDetail,
} from '../api/types'
import { CacheDeployWizardPage } from '../pages/CacheDeployWizardPage'
import * as cacheDeployApi from '../api/cacheDeploy'
import * as deploymentsApi from '../api/deployments'
import * as modelsApi from '../api/models'
import * as nodesApi from '../api/nodes'
import * as operationsApi from '../api/operations'
import { operationPollConfig } from '../utils/operationLifecycle'

function makeCache(overrides: Partial<ModelCacheEntry> = {}): ModelCacheEntry {
  return {
    id: 'cache-1',
    node_id: 'node-1',
    node_name: 'gpu-node',
    model_artifact_id: 'art-1',
    repository_id: 'org/demo-model',
    resolved_revision: 'abc123',
    model_id: 'model-1',
    model_slug: 'demo-model',
    model_version_id: 'ver-1',
    status: 'READY',
    local_path: '/data/modelops/models/org/demo-model/abc123',
    size_bytes: 1000,
    error_message: null,
    prepared_at: '2026-10-01T00:00:00Z',
    last_verified_at: '2026-10-01T00:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    is_deployment: false,
    note: null,
    ...overrides,
  }
}

function makeVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
  return {
    id: 'ver-1',
    model_id: 'model-1',
    version_label: '1.0.0',
    source_repository: 'org/demo-model',
    source_revision: 'abc123',
    quantization: null,
    dtype: null,
    runtime_type: 'VLLM',
    runtime_image: 'example/runtime:tag',
    runtime_image_digest: null,
    served_model_name: 'demo',
    expected_idle_vram_mb: null,
    expected_peak_vram_mb: null,
    default_max_model_len: null,
    runtime_config: {},
    archived_at: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function makeModel(overrides: Partial<ModelSummary> = {}): ModelSummary {
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
    ...overrides,
  }
}

function makeNode(overrides: Partial<NodeDetail> = {}): NodeDetail {
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
    ...overrides,
  }
}

function makeDeployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: 'dep-1',
    name: 'demo-managed',
    model_version_id: 'ver-1',
    node_id: 'node-1',
    deployment_type: 'MANAGED',
    desired_state: 'STOPPED',
    runtime_status: 'CREATED',
    health_status: 'UNKNOWN',
    container_id: null,
    container_name: 'demo-container',
    upstream_base_url: null,
    runtime_port: 8000,
    deployment_config: {},
    gpu_assignments: [],
    last_started_at: null,
    last_stopped_at: null,
    last_health_at: null,
    status_reason: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    retired_at: null,
    ...overrides,
  }
}

function makeOpDetail(overrides: Partial<OperationDetail> = {}): OperationDetail {
  return {
    id: 'op-1',
    operation_type: 'START',
    status: 'RUNNING',
    switch_strategy: null,
    endpoint_alias_id: null,
    source_deployment_id: null,
    target_deployment_id: 'dep-1',
    current_step: 'START_CONTAINER',
    cancel_requested_at: null,
    retry_of_operation_id: null,
    requested_by: null,
    request_reason: null,
    created_at: '2026-10-07T00:00:00Z',
    started_at: '2026-10-07T00:00:01Z',
    finished_at: null,
    error: null,
    steps: [],
    ...overrides,
  }
}

function renderWizard(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/models/cache/:cacheId/deploy"
          element={<CacheDeployWizardPage />}
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('CacheDeployWizardPage Start lifecycle poll (P1-4B)', () => {
  beforeEach(() => {
    operationPollConfig.ms = 40
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
    vi.spyOn(cacheDeployApi, 'getCacheEntry').mockResolvedValue(makeCache())
    vi.spyOn(modelsApi, 'getModelVersion').mockResolvedValue(makeVersion())
    vi.spyOn(modelsApi, 'getModel').mockResolvedValue(makeModel())
    vi.spyOn(nodesApi, 'getNode').mockResolvedValue(makeNode())
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(deploymentsApi, 'startDeployment')
    vi.spyOn(operationsApi, 'getOperation')
  })

  afterEach(() => {
    cleanup()
    operationPollConfig.ms = 4000
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
    vi.restoreAllMocks()
  })

  it('polls Operation then Deployment health until RUNNING+HEALTHY, then stops', async () => {
    const user = userEvent.setup()
    let opPhase: 'running' | 'succeeded' = 'running'
    let depPhase: 'starting' | 'healthy' = 'starting'

    vi.mocked(deploymentsApi.getDeployment).mockImplementation(async () => {
      if (depPhase === 'healthy') {
        return makeDeployment({
          desired_state: 'RUNNING',
          runtime_status: 'RUNNING',
          health_status: 'HEALTHY',
        })
      }
      return makeDeployment({
        desired_state: 'RUNNING',
        runtime_status: 'STARTING',
        health_status: 'UNKNOWN',
      })
    })
    vi.mocked(deploymentsApi.startDeployment).mockResolvedValue({
      id: 'op-1',
      operation_type: 'START',
      status: 'QUEUED',
      target_deployment_id: 'dep-1',
      current_step: 'PREPARE_ARTIFACTS',
      created_at: '2026-10-07T00:00:00Z',
      started_at: null,
      finished_at: null,
      error: null,
    })
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (opPhase === 'succeeded') {
        return makeOpDetail({
          status: 'SUCCEEDED',
          current_step: 'FINALIZE',
          finished_at: '2026-10-07T00:05:00Z',
        })
      }
      return makeOpDetail({
        status: 'RUNNING',
        current_step: 'START_CONTAINER',
      })
    })

    renderWizard(
      '/models/cache/cache-1/deploy?step=start&deployment_id=dep-1',
    )
    expect(
      await screen.findByRole('button', { name: 'Start Deployment' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Start Deployment' }))

    expect(await screen.findByText(/자동 갱신/)).toBeInTheDocument()
    expect(screen.getByText('START_CONTAINER')).toBeInTheDocument()

    opPhase = 'succeeded'
    await waitFor(() => {
      expect(screen.getByText(/제한적으로 상태를 확인/)).toBeInTheDocument()
    })

    depPhase = 'healthy'
    await waitFor(() => {
      expect(
        screen.getByText(/RUNNING \+ HEALTHY입니다/),
      ).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Next: Publish' })).not.toBeDisabled()

    const callsAfterReady = vi.mocked(operationsApi.getOperation).mock.calls.length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterReady,
    )
    expect(deploymentsApi.startDeployment).toHaveBeenCalledTimes(1)
  })

  it('keeps polling after SUCCEEDED until health wait, and ignores late stale Operation', async () => {
    let resolveLate!: (value: OperationDetail) => void
    let opCalls = 0

    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        desired_state: 'RUNNING',
        runtime_status: 'STARTING',
        health_status: 'UNKNOWN',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockImplementation(() => {
      opCalls += 1
      // First call is the F5/page load; hang the first silent poll.
      if (opCalls === 2) {
        return new Promise((resolve) => {
          resolveLate = resolve
        })
      }
      if (opCalls === 1) {
        return Promise.resolve(
          makeOpDetail({
            status: 'RUNNING',
            current_step: 'START_CONTAINER',
          }),
        )
      }
      return Promise.resolve(
        makeOpDetail({
          status: 'SUCCEEDED',
          current_step: 'FINALIZE',
          finished_at: '2026-10-07T00:05:00Z',
        }),
      )
    })

    renderWizard(
      '/models/cache/cache-1/deploy?step=start&deployment_id=dep-1&operation_id=op-1',
    )
    expect(await screen.findByText('START_CONTAINER')).toBeInTheDocument()
    expect(await screen.findByText(/자동 갱신/)).toBeInTheDocument()

    await waitFor(() => {
      expect(opCalls).toBeGreaterThanOrEqual(3)
    })
    expect(await screen.findByText(/제한적으로 상태를 확인/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next: Publish' })).toBeDisabled()

    resolveLate(
      makeOpDetail({
        status: 'FAILED',
        current_step: 'STALE_STEP',
        error: { code: 'S', message: 'stale-failed' },
      }),
    )
    await waitFor(() => {
      expect(screen.getByText(/제한적으로 상태를 확인/)).toBeInTheDocument()
    })
    expect(screen.queryByText('STALE_STEP')).not.toBeInTheDocument()
    expect(screen.queryByText(/stale-failed/)).not.toBeInTheDocument()
  })

  it('shows Operation failure distinctly and stops polling', async () => {
    let opCalls = 0
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      opCalls += 1
      if (opCalls === 1) {
        return makeOpDetail({
          status: 'RUNNING',
          current_step: 'START_CONTAINER',
        })
      }
      return makeOpDetail({
        status: 'FAILED',
        current_step: 'START_CONTAINER',
        error: { code: 'E', message: 'boom' },
        finished_at: '2026-10-07T00:02:00Z',
      })
    })

    renderWizard(
      '/models/cache/cache-1/deploy?step=start&deployment_id=dep-1&operation_id=op-1',
    )
    expect(await screen.findByText('START_CONTAINER')).toBeInTheDocument()
    expect(await screen.findByText(/자동 갱신/)).toBeInTheDocument()

    expect(await screen.findByText('Operation 실패')).toBeInTheDocument()
    expect(screen.getAllByText('E: boom').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByRole('button', { name: 'Next: Publish' })).toBeDisabled()

    const callsAfterFail = vi.mocked(operationsApi.getOperation).mock.calls.length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterFail,
    )
  })
})
