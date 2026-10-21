import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  Deployment,
  LifecycleOperation,
  ModelVersion,
  NodeDetail,
} from '../api/types'
import { DeploymentDetailPage } from '../pages/DeploymentDetailPage'
import * as deploymentsApi from '../api/deployments'
import * as modelsApi from '../api/models'
import * as nodesApi from '../api/nodes'
import * as operationsApi from '../api/operations'
import type { OperationDetail } from '../api/types'
import { operationPollConfig } from '../utils/operationLifecycle'

function makeDeployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: 'd1',
    name: 'chat-prod',
    model_version_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    node_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    deployment_type: 'MANAGED',
    desired_state: 'STOPPED',
    runtime_status: 'CREATED',
    health_status: 'UNKNOWN',
    container_id: 'cid1234567890abcdef',
    container_name: 'demo-container',
    upstream_base_url: 'http://internal-placeholder:8000',
    runtime_port: 8000,
    deployment_config: {
      max_num_seqs: 8,
      scheduling_policy: 'priority',
      secret_or_unknown: 'must-not-render',
    },
    gpu_assignments: [
      {
        gpu_device_id: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
        device_order: 0,
        expected_vram_mb: 16384,
        created_at: '2026-10-01T00:00:00Z',
      },
    ],
    last_started_at: null,
    last_stopped_at: null,
    last_health_at: null,
    status_reason: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    retired_at: null,
    ...overrides,
  }
}

function makeVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
  return {
    id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    model_id: 'm1',
    version_label: '1.0.0',
    source_repository: null,
    source_revision: null,
    quantization: null,
    dtype: null,
    runtime_type: 'VLLM',
    runtime_image: 'example/runtime:tag',
    runtime_image_digest: null,
    served_model_name: 'served',
    expected_idle_vram_mb: null,
    expected_peak_vram_mb: null,
    default_max_model_len: null,
    runtime_config: {},
    archived_at: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makeNode(overrides: Partial<NodeDetail> = {}): NodeDetail {
  return {
    id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    name: 'gpu-node-a',
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
    updated_at: '2026-10-06T01:02:03Z',
    gpus: [],
    ...overrides,
  }
}

function makeOp(
  overrides: Partial<LifecycleOperation> = {},
): LifecycleOperation {
  return {
    id: 'op-1',
    operation_type: 'START',
    status: 'QUEUED',
    target_deployment_id: 'd1',
    current_step: 'PREPARE_ARTIFACTS',
    created_at: '2026-10-07T00:00:00Z',
    started_at: null,
    finished_at: null,
    error: null,
    ...overrides,
  }
}

function renderDetail(path = '/deployments/d1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/deployments/:deploymentId"
          element={<DeploymentDetailPage />}
        />
        <Route path="/deployments" element={<div>Deployments list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

function makeOpDetail(
  overrides: Partial<OperationDetail> = {},
): OperationDetail {
  return {
    id: 'op-1',
    operation_type: 'START',
    status: 'RUNNING',
    switch_strategy: null,
    endpoint_alias_id: null,
    source_deployment_id: null,
    target_deployment_id: 'd1',
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

describe('DeploymentDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(deploymentsApi, 'startDeployment')
    vi.spyOn(deploymentsApi, 'stopDeployment')
    vi.spyOn(deploymentsApi, 'restartDeployment')
    vi.spyOn(deploymentsApi, 'listDeployments')
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.spyOn(nodesApi, 'getNode')
    vi.spyOn(operationsApi, 'getOperation')
  })

  afterEach(() => {
    cleanup()
    operationPollConfig.ms = 4000
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
    vi.restoreAllMocks()
  })

  it('renders identity, refs, GPU assignments, and allowlisted config', async () => {
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'chat-prod' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '1.0.0' })).toHaveAttribute(
      'href',
      '/model-versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    )
    expect(screen.getByRole('link', { name: 'gpu-node-a' })).toHaveAttribute(
      'href',
      '/nodes/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    )
    const monitoringLinks = screen.getAllByRole('link', {
      name: '모델 실행·자원 모니터링 보기',
    })
    expect(monitoringLinks.length).toBeGreaterThanOrEqual(1)
    for (const link of monitoringLinks) {
      expect(link).toHaveAttribute('href', '/observability/deployments/d1')
    }
    expect(screen.getByText('16.0 GiB')).toBeInTheDocument()
    expect(screen.getByText('deployment_config.max_num_seqs')).toBeInTheDocument()
    expect(screen.getByText('8')).toBeInTheDocument()
    expect(screen.getByText('priority')).toBeInTheDocument()
    expect(screen.queryByText('must-not-render')).not.toBeInTheDocument()
    expect(screen.getByText(/기타 설정 1개/)).toBeInTheDocument()
    const configHeading = screen.getByRole('heading', {
      level: 2,
      name: '모델 배포 실행 설정',
    })
    const configSection = configHeading.closest('section')
    expect(configSection?.querySelector('.config-kv-grid')).not.toBeNull()
    expect(configSection?.querySelector('.meta-grid')).toBeNull()
    expect(
      configSection?.querySelector('dt')?.textContent,
    ).toMatch(/^deployment_config\./)
  })

  it('shows Start/Stop/Restart for MANAGED non-retired only', async () => {
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    renderDetail()
    expect(await screen.findByRole('button', { name: '모델 시작' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '모델 중지' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '모델 재시작' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '모델 종료·정리 절차' })).toHaveAttribute(
      'href',
      '/deployments/d1/decommission',
    )
    cleanup()

    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ deployment_type: 'IMPORTED' }),
    )
    renderDetail()
    expect(
      await screen.findByText(/IMPORTED 모델 배포에는 시작·중지·재시작/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '모델 시작' })).not.toBeInTheDocument()
    cleanup()

    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ retired_at: '2026-09-01T00:00:00Z' }),
    )
    renderDetail()
    expect(
      await screen.findByText(/퇴역\(Retired\)된 모델 배포에는 시작·중지·재시작/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '모델 시작' })).not.toBeInTheDocument()
  })

  it('enqueues Start, shows Operation, and re-reads Deployment', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment)
      .mockResolvedValueOnce(makeDeployment())
      .mockResolvedValueOnce(
        makeDeployment({ desired_state: 'RUNNING', runtime_status: 'CREATED' }),
      )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockResolvedValue(makeOp())
    renderDetail()
    await screen.findByRole('button', { name: '모델 시작' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    await waitFor(() => {
      expect(deploymentsApi.startDeployment).toHaveBeenCalledWith('d1')
    })
    expect(await screen.findByText('op-1')).toBeInTheDocument()
    expect(screen.getByText('PREPARE_ARTIFACTS')).toBeInTheDocument()
    await waitFor(() => {
      expect(deploymentsApi.getDeployment).toHaveBeenCalledTimes(2)
    })
    expect(screen.getByText(/\(RUNNING\)/)).toBeInTheDocument()
  })

  it('keeps Deployment snapshot on lifecycle 409 and shows error', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockRejectedValue(
      new ApiError('An active lifecycle operation already exists', {
        status: 409,
      }),
    )
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'chat-prod' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    expect(
      await screen.findByText(/active lifecycle operation already exists/i),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'chat-prod' }),
    ).toBeInTheDocument()
    expect(screen.getByText('demo-container')).toBeInTheDocument()
  })

  it('mutually excludes lifecycle buttons while enqueue is in flight', async () => {
    const user = userEvent.setup()
    let resolveStart!: (value: LifecycleOperation) => void
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve
        }),
    )
    renderDetail()
    await screen.findByRole('button', { name: '모델 시작' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '작업 접수 중…' })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: '모델 중지' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '모델 재시작' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()
    resolveStart(makeOp())
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '모델 시작' })).not.toBeDisabled()
    })
  })

  it('shows 404 and clears stale Deployment on refresh 404', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment)
      .mockResolvedValueOnce(makeDeployment())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'chat-prod' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('모델 배포를 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'chat-prod' }),
    ).not.toBeInTheDocument()
  })

  it('keeps stale Deployment on refresh 500', async () => {
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment)
      .mockResolvedValueOnce(makeDeployment())
      .mockRejectedValueOnce(new ApiError('boom', { status: 500 }))
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'chat-prod' })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText(/이전에 불러온 모델 배포 정보를 그대로 표시/)).toBeInTheDocument()
    })
    expect(
      screen.getByRole('heading', { level: 2, name: 'chat-prod' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('모델 배포를 찾을 수 없습니다.'),
    ).not.toBeInTheDocument()
  })

  it('does not call forbidden APIs', async () => {
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDetail()
    await screen.findByRole('heading', { level: 2, name: 'chat-prod' })
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/capacity-profile'))).toBe(false)
    expect(urls.some((u) => u.includes('/resources/latest'))).toBe(false)
    expect(
      urls.some((u) => /\/deployments\/[^/]+\/health/.test(u)),
    ).toBe(false)
    expect(urls.some((u) => u.includes('/preflights'))).toBe(false)
    expect(urls.some((u) => u.includes('/retire'))).toBe(false)
    expect(urls.some((u) => u.includes('/remove'))).toBe(false)
    expect(deploymentsApi.listDeployments).not.toHaveBeenCalled()
  })

  it('polls Operation and Deployment until terminal, then stops', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment)
      .mockResolvedValueOnce(makeDeployment())
      .mockResolvedValueOnce(
        makeDeployment({ desired_state: 'RUNNING', runtime_status: 'STARTING' }),
      )
      .mockResolvedValue(
        makeDeployment({
          desired_state: 'RUNNING',
          runtime_status: 'RUNNING',
          health_status: 'HEALTHY',
        }),
      )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockResolvedValue(
      makeOp({ status: 'QUEUED', current_step: 'PREPARE_ARTIFACTS' }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOpDetail({
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        finished_at: '2026-10-07T00:05:00Z',
      }),
    )

    renderDetail()
    await screen.findByRole('button', { name: '모델 시작' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    expect(await screen.findByRole('link', { name: 'op-1' })).toHaveAttribute(
      'href',
      '/operations/op-1',
    )
    expect(screen.getByText(/자동 갱신/)).toBeInTheDocument()

    expect(await screen.findByText('FINALIZE')).toBeInTheDocument()
    expect(screen.getByText(/\(HEALTHY\)/)).toBeInTheDocument()
    expect(screen.queryByText(/자동 갱신/)).not.toBeInTheDocument()

    const callsAfterTerminal = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterTerminal,
    )
    expect(deploymentsApi.startDeployment).toHaveBeenCalledTimes(1)
  })

  it('keeps newer manual refresh when an older poll resolves late (same Deployment)', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    let resolveSlowPollOp!: (value: OperationDetail) => void
    let depCalls = 0
    let opCalls = 0

    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockResolvedValue(
      makeOp({ id: 'op-1', status: 'RUNNING', current_step: 'START_CONTAINER' }),
    )
    vi.mocked(deploymentsApi.getDeployment).mockImplementation(async () => {
      depCalls += 1
      if (depCalls <= 2) {
        // initial load + post-enqueue refresh
        return makeDeployment({
          desired_state: depCalls === 1 ? 'STOPPED' : 'RUNNING',
          runtime_status: depCalls === 1 ? 'CREATED' : 'STARTING',
        })
      }
      // manual refresh (and any later polls after refresh wins the epoch)
      return makeDeployment({
        desired_state: 'RUNNING',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
        status_reason: 'from-refresh',
      })
    })
    vi.mocked(operationsApi.getOperation).mockImplementation(() => {
      opCalls += 1
      if (opCalls === 1) {
        return new Promise((resolve) => {
          resolveSlowPollOp = resolve
        })
      }
      return Promise.resolve(
        makeOpDetail({
          status: 'RUNNING',
          current_step: 'WAIT_TARGET_HEALTH',
        }),
      )
    })

    renderDetail()
    await screen.findByRole('button', { name: '모델 시작' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    expect(await screen.findByRole('link', { name: 'op-1' })).toBeInTheDocument()

    await waitFor(() => {
      expect(opCalls).toBeGreaterThanOrEqual(1)
    })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('from-refresh')).toBeInTheDocument()
    expect(screen.getByText(/\(HEALTHY\)/)).toBeInTheDocument()

    resolveSlowPollOp(
      makeOpDetail({
        status: 'RUNNING',
        current_step: 'STALE_POLL_STEP',
        error: { code: 'S', message: 'stale-poll' },
      }),
    )
    await waitFor(() => {
      expect(screen.getByText('from-refresh')).toBeInTheDocument()
    })
    expect(screen.queryByText('STALE_POLL_STEP')).not.toBeInTheDocument()
    expect(screen.queryByText('stale-poll')).not.toBeInTheDocument()
  })

  it('ignores a late poll response after Operation id changes', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    let resolveLate!: (value: OperationDetail) => void
    let opAPolls = 0
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.startDeployment).mockResolvedValue(
      makeOp({ id: 'op-a', status: 'RUNNING' }),
    )
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-b',
        status: 'QUEUED',
        operation_type: 'STOP',
        current_step: 'STOP_CONTAINER',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockImplementation((id) => {
      if (id === 'op-a') {
        opAPolls += 1
        return new Promise((resolve) => {
          resolveLate = resolve
        })
      }
      return Promise.resolve(
        makeOpDetail({
          id: 'op-b',
          operation_type: 'STOP',
          status: 'QUEUED',
          current_step: 'STOP_CONTAINER',
        }),
      )
    })

    renderDetail()
    await screen.findByRole('button', { name: '모델 시작' })
    await user.click(screen.getByRole('button', { name: '모델 시작' }))
    expect(await screen.findByRole('link', { name: 'op-a' })).toBeInTheDocument()

    await waitFor(() => {
      expect(opAPolls).toBeGreaterThan(0)
    })
    await user.click(screen.getByRole('button', { name: '모델 중지' }))
    expect(await screen.findByRole('link', { name: 'op-b' })).toBeInTheDocument()

    resolveLate(
      makeOpDetail({
        id: 'op-a',
        status: 'FAILED',
        current_step: 'START_CONTAINER',
        error: { code: 'X', message: 'stale' },
      }),
    )
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'op-b' })).toBeInTheDocument()
    })
    expect(screen.queryByText('stale')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'op-a' })).not.toBeInTheDocument()
  })

  it('keeps polling after STOP SUCCEEDED until runtime_status is STOPPED', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    let stopSyncPhase: 'active' | 'lag' | 'done' = 'active'
    vi.mocked(deploymentsApi.getDeployment).mockImplementation(async () => {
      if (stopSyncPhase === 'done') {
        return makeDeployment({
          desired_state: 'STOPPED',
          runtime_status: 'STOPPED',
          health_status: 'UNKNOWN',
        })
      }
      return makeDeployment({
        desired_state: stopSyncPhase === 'lag' ? 'STOPPED' : 'RUNNING',
        runtime_status: 'RUNNING',
        health_status: stopSyncPhase === 'lag' ? 'UNKNOWN' : 'HEALTHY',
      })
    })
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-stop',
        operation_type: 'STOP',
        status: 'QUEUED',
        current_step: 'STOP_CONTAINER',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockImplementation(async () => {
      if (stopSyncPhase === 'active') {
        stopSyncPhase = 'lag'
      }
      return makeOpDetail({
        id: 'op-stop',
        operation_type: 'STOP',
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        // Recent finished_at so stop_wait is not immediately timed out.
        finished_at: new Date().toISOString(),
      })
    })

    renderDetail()
    await screen.findByRole('button', { name: '모델 중지' })
    await user.click(screen.getByRole('button', { name: '모델 중지' }))
    expect(
      await screen.findByRole('link', { name: 'op-stop' }),
    ).toBeInTheDocument()

    expect(await screen.findByTestId('stop-sync-wait')).toBeInTheDocument()
    // Health stays Backend value — not invented as STOPPED/HEALTHY.
    expect(screen.getByText(/알 수 없음/)).toBeInTheDocument()

    stopSyncPhase = 'done'
    await waitFor(() => {
      expect(screen.queryByTestId('stop-sync-wait')).not.toBeInTheDocument()
    })
    // Runtime Status badge (not Desired State) should show STOPPED.
    expect(screen.getAllByText(/중지/).length).toBeGreaterThan(0)

    const callsAfterStopped = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterStopped,
    )
  })

  it('does not enter stop-sync wait when STOP Operation fails', async () => {
    operationPollConfig.ms = 40
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        desired_state: 'RUNNING',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-stop-fail',
        operation_type: 'STOP',
        status: 'QUEUED',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOpDetail({
        id: 'op-stop-fail',
        operation_type: 'STOP',
        status: 'FAILED',
        current_step: 'STOP_CONTAINER',
        finished_at: new Date().toISOString(),
        error: { code: 'STOP_FAILED', message: 'container still up' },
      }),
    )

    renderDetail()
    await user.click(await screen.findByRole('button', { name: '모델 중지' }))
    expect(
      await screen.findByRole('link', { name: 'op-stop-fail' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(/container still up/),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('stop-sync-wait')).not.toBeInTheDocument()
    expect(
      screen.queryByText(/STOP 상태 동기화 대기 시간 초과/),
    ).not.toBeInTheDocument()

    const callsAfterFail = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterFail,
    )
  })

  it('ends stop-sync wait after timeout and offers manual refresh', async () => {
    operationPollConfig.ms = 40
    operationPollConfig.healthWaitMaxMs = 80
    const user = userEvent.setup()
    const finishedAt = new Date(Date.now() - 200).toISOString()
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        desired_state: 'STOPPED',
        runtime_status: 'RUNNING',
        health_status: 'DEGRADED',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-stop-lag',
        operation_type: 'STOP',
        status: 'QUEUED',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOpDetail({
        id: 'op-stop-lag',
        operation_type: 'STOP',
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        finished_at: finishedAt,
      }),
    )

    renderDetail()
    await user.click(await screen.findByRole('button', { name: '모델 중지' }))
    expect(
      await screen.findByRole('link', { name: 'op-stop-lag' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(/STOP 상태 동기화 대기 시간 초과/),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/저하/).length).toBeGreaterThan(0)
    expect(
      screen.getByRole('button', { name: '다시 시도' }),
    ).toBeInTheDocument()

    const callsAfterTimeout = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterTimeout,
    )
  })

  it('times out stop-sync wait when finished_at is null using fallback clock', async () => {
    operationPollConfig.ms = 40
    operationPollConfig.healthWaitMaxMs = 80
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        desired_state: 'STOPPED',
        runtime_status: 'RUNNING',
        health_status: 'UNKNOWN',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-stop-null-finished',
        operation_type: 'STOP',
        status: 'QUEUED',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOpDetail({
        id: 'op-stop-null-finished',
        operation_type: 'STOP',
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        finished_at: null,
      }),
    )

    renderDetail()
    await user.click(await screen.findByRole('button', { name: '모델 중지' }))
    expect(
      await screen.findByRole('link', { name: 'op-stop-null-finished' }),
    ).toBeInTheDocument()
    expect(await screen.findByTestId('stop-sync-wait')).toBeInTheDocument()
    expect(
      await screen.findByText(/STOP 상태 동기화 대기 시간 초과/),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('stop-sync-wait')).not.toBeInTheDocument()
    expect(screen.getByText(/알 수 없음/)).toBeInTheDocument()

    const callsAfterTimeout = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterTimeout,
    )
  })

  it('times out stop-sync wait for invalid or future finished_at', async () => {
    operationPollConfig.ms = 40
    operationPollConfig.healthWaitMaxMs = 80
    const user = userEvent.setup()
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        desired_state: 'STOPPED',
        runtime_status: 'RUNNING',
        health_status: 'DEGRADED',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue(
      makeOp({
        id: 'op-stop-future',
        operation_type: 'STOP',
        status: 'QUEUED',
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOpDetail({
        id: 'op-stop-future',
        operation_type: 'STOP',
        status: 'SUCCEEDED',
        current_step: 'FINALIZE',
        // Far-future stamp must not freeze elapsed at 0 forever.
        finished_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      }),
    )

    renderDetail()
    await user.click(await screen.findByRole('button', { name: '모델 중지' }))
    expect(
      await screen.findByRole('link', { name: 'op-stop-future' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(/STOP 상태 동기화 대기 시간 초과/),
    ).toBeInTheDocument()

    const callsAfterTimeout = vi.mocked(operationsApi.getOperation).mock.calls
      .length
    await new Promise((r) => setTimeout(r, 120))
    expect(vi.mocked(operationsApi.getOperation).mock.calls.length).toBe(
      callsAfterTimeout,
    )
  })
})
