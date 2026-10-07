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

describe('DeploymentDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(deploymentsApi, 'startDeployment')
    vi.spyOn(deploymentsApi, 'stopDeployment')
    vi.spyOn(deploymentsApi, 'restartDeployment')
    vi.spyOn(deploymentsApi, 'listDeployments')
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.spyOn(nodesApi, 'getNode')
  })

  afterEach(() => {
    cleanup()
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
    expect(screen.getByText('16.0 GiB')).toBeInTheDocument()
    expect(screen.getByText('deployment_config.max_num_seqs')).toBeInTheDocument()
    expect(screen.getByText('8')).toBeInTheDocument()
    expect(screen.getByText('priority')).toBeInTheDocument()
    expect(screen.queryByText('must-not-render')).not.toBeInTheDocument()
    expect(screen.getByText(/기타 설정 1개/)).toBeInTheDocument()
  })

  it('shows Start/Stop/Restart for MANAGED non-retired only', async () => {
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    renderDetail()
    expect(await screen.findByRole('button', { name: 'Start' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restart' })).toBeInTheDocument()
    cleanup()

    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ deployment_type: 'IMPORTED' }),
    )
    renderDetail()
    expect(
      await screen.findByText(/IMPORTED Deployment에는 lifecycle/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument()
    cleanup()

    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ retired_at: '2026-09-01T00:00:00Z' }),
    )
    renderDetail()
    expect(
      await screen.findByText(/Retired Deployment에는 lifecycle/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument()
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
    await screen.findByRole('button', { name: 'Start' })
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => {
      expect(deploymentsApi.startDeployment).toHaveBeenCalledWith('d1')
    })
    expect(await screen.findByText('op-1')).toBeInTheDocument()
    expect(screen.getByText('PREPARE_ARTIFACTS')).toBeInTheDocument()
    await waitFor(() => {
      expect(deploymentsApi.getDeployment).toHaveBeenCalledTimes(2)
    })
    expect(screen.getByText(/RUNNING/)).toBeInTheDocument()
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
    await user.click(screen.getByRole('button', { name: 'Start' }))
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
    await screen.findByRole('button', { name: 'Start' })
    await user.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Enqueue 중…' })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: 'Stop' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Restart' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '새로고침' })).toBeDisabled()
    resolveStart(makeOp())
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Start' })).not.toBeDisabled()
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
      await screen.findByText('Deployment를 찾을 수 없습니다.'),
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
      expect(screen.getByText(/기존 Deployment 정보를 표시/)).toBeInTheDocument()
    })
    expect(
      screen.getByRole('heading', { level: 2, name: 'chat-prod' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Deployment를 찾을 수 없습니다.'),
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
})
