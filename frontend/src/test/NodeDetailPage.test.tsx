import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  GPUDevice,
  NodeDetail,
  NodeResourcesLatest,
} from '../api/types'
import { NodeDetailPage } from '../pages/NodeDetailPage'
import * as nodesApi from '../api/nodes'

function makeGpu(
  overrides: Partial<GPUDevice> &
    Pick<GPUDevice, 'id' | 'device_index' | 'model_name'>,
): GPUDevice {
  return {
    node_id: 'node-1',
    gpu_uuid: `GPU-UUID-${overrides.device_index}`,
    vram_total_mb: 16384,
    compute_capability: '8.6',
    safety_margin_mb: 1024,
    status: 'AVAILABLE',
    last_seen_at: '2026-10-06T01:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:00:00Z',
    ...overrides,
  }
}

function makeNode(overrides: Partial<NodeDetail> = {}): NodeDetail {
  return {
    id: 'node-1',
    name: 'gpu-node-a',
    hostname: 'host-a.example.test',
    agent_base_url: 'http://node-agent.example.test:9100',
    environment: 'dev',
    region: 'local',
    status: 'ONLINE',
    last_heartbeat_at: '2026-10-06T01:02:03Z',
    cpu_model: 'Test CPU',
    ram_total_mb: 65536,
    disk_total_mb: 512000,
    labels_json: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    gpus: [],
    ...overrides,
  }
}

function makeResources(
  overrides: Partial<NodeResourcesLatest> = {},
): NodeResourcesLatest {
  return {
    node_id: 'node-1',
    host: {
      sampled_at: '2026-10-06T01:00:00Z',
      cpu_utilization_pct: 12.5,
      ram_total_mb: 65536,
      ram_used_mb: 16384,
      ram_free_mb: 49152,
      disk_total_mb: 512000,
      disk_used_mb: 128000,
      disk_free_mb: 384000,
    },
    gpus: [],
    ...overrides,
  }
}

function renderDetail(path = '/nodes/node-1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
        <Route path="/nodes" element={<div>Nodes list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('NodeDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(nodesApi, 'getNode')
    vi.spyOn(nodesApi, 'getNodeResources')
    vi.spyOn(nodesApi, 'refreshNodeResources')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders metadata and Host resource metrics', async () => {
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(makeResources())

    renderDetail()

    expect(
      await screen.findByRole('heading', { level: 2, name: 'gpu-node-a' }),
    ).toBeInTheDocument()
    expect(screen.getByText('host-a.example.test')).toBeInTheDocument()
    expect(screen.getByText(/온라인/)).toBeInTheDocument()
    expect(screen.getByText('CPU Utilization')).toBeInTheDocument()
    expect(screen.getByText('12.5%')).toBeInTheDocument()
    expect(screen.getByText(/최근 수집 시각/)).toBeInTheDocument()
    expect(screen.queryByText('2026-10-06T01:02:03Z')).not.toBeInTheDocument()
  })

  it('renders multiple GPU cards with per-GPU VRAM', async () => {
    const gpu0 = makeGpu({ id: 'g0', device_index: 0, model_name: 'GPU-A' })
    const gpu1 = makeGpu({ id: 'g1', device_index: 1, model_name: 'GPU-B' })
    vi.mocked(nodesApi.getNode).mockResolvedValue(
      makeNode({ gpus: [gpu0, gpu1] }),
    )
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({
        gpus: [
          {
            gpu: gpu0,
            snapshot: {
              sampled_at: '2026-10-06T01:00:00Z',
              vram_total_mb: 16384,
              vram_used_mb: 8192,
              vram_free_mb: 8192,
              gpu_utilization_pct: 40,
              memory_utilization_pct: 50,
              temperature_c: 42,
              power_w: 80.5,
            },
          },
          {
            gpu: gpu1,
            snapshot: {
              sampled_at: '2026-10-06T01:00:00Z',
              vram_total_mb: 16384,
              vram_used_mb: 8192,
              vram_free_mb: 8192,
              gpu_utilization_pct: 10,
              memory_utilization_pct: 20,
              temperature_c: 38,
              power_w: 60,
            },
          },
        ],
      }),
    )

    renderDetail()

    expect(await screen.findByText(/GPU 0 · GPU-A/)).toBeInTheDocument()
    expect(screen.getByText(/GPU 1 · GPU-B/)).toBeInTheDocument()
    expect(screen.getAllByText(/8\.0 GiB \/ 16\.0 GiB/).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/Temperature:\s*42\.0 °C/)).toBeInTheDocument()
    expect(screen.getByText(/Power:\s*80\.5 W/)).toBeInTheDocument()
  })

  it('does not aggregate free VRAM across GPUs', async () => {
    const gpu0 = makeGpu({ id: 'g0', device_index: 0, model_name: 'GPU-A' })
    const gpu1 = makeGpu({ id: 'g1', device_index: 1, model_name: 'GPU-B' })
    vi.mocked(nodesApi.getNode).mockResolvedValue(
      makeNode({ gpus: [gpu0, gpu1] }),
    )
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({
        gpus: [
          {
            gpu: gpu0,
            snapshot: {
              sampled_at: '2026-10-06T01:00:00Z',
              vram_total_mb: 16384,
              vram_used_mb: 8192,
              vram_free_mb: 8192,
              gpu_utilization_pct: null,
              memory_utilization_pct: null,
              temperature_c: null,
              power_w: null,
            },
          },
          {
            gpu: gpu1,
            snapshot: {
              sampled_at: '2026-10-06T01:00:00Z',
              vram_total_mb: 16384,
              vram_used_mb: 8192,
              vram_free_mb: 8192,
              gpu_utilization_pct: null,
              memory_utilization_pct: null,
              temperature_c: null,
              power_w: null,
            },
          },
        ],
      }),
    )

    renderDetail()
    await screen.findByText(/GPU 0 · GPU-A/)

    expect(screen.queryByText(/16\.0 GiB available/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Total Available VRAM/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Combined Free VRAM/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Deployable VRAM/i)).not.toBeInTheDocument()
    expect(screen.getAllByLabelText(/GPU 0|GPU 1/)).toHaveLength(2)
  })

  it('shows zero-GPU empty state without changing backend status', async () => {
    vi.mocked(nodesApi.getNode).mockResolvedValue(
      makeNode({ status: 'ONLINE', gpus: [] }),
    )
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({ gpus: [] }),
    )

    renderDetail()
    expect(await screen.findByText('등록된 GPU가 없습니다.')).toBeInTheDocument()
    expect(screen.getByText(/온라인/)).toBeInTheDocument()
  })

  it('shows null GPU snapshot message', async () => {
    const gpu0 = makeGpu({ id: 'g0', device_index: 0, model_name: 'GPU-A' })
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode({ gpus: [gpu0] }))
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({
        gpus: [{ gpu: gpu0, snapshot: null }],
      }),
    )

    renderDetail()
    expect(
      await screen.findByText('아직 수집된 GPU 자원 정보가 없습니다.'),
    ).toBeInTheDocument()
  })

  it('keeps metadata when resource GET fails', async () => {
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(nodesApi.getNodeResources).mockRejectedValue(
      new ApiError('resource down', { status: 503 }),
    )

    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'gpu-node-a' }),
    ).toBeInTheDocument()
    expect(screen.getByText('resource down')).toBeInTheDocument()
  })

  it('keeps prior resources when metadata refresh fails', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.getNode)
      .mockResolvedValueOnce(makeNode())
      .mockRejectedValueOnce(new ApiError('meta fail', { status: 500 }))
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(makeResources())

    renderDetail()
    expect(await screen.findByText('12.5%')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(screen.getByText('meta fail')).toBeInTheDocument()
    })
    expect(screen.getByText('12.5%')).toBeInTheDocument()
  })

  it('shows 404 with back link', async () => {
    vi.mocked(nodesApi.getNode).mockRejectedValue(
      new ApiError('not found', { status: 404 }),
    )
    vi.mocked(nodesApi.getNodeResources).mockRejectedValue(
      new ApiError('not found', { status: 404 }),
    )

    renderDetail()
    expect(
      await screen.findByText('Node를 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    const back = screen.getByRole('link', { name: /Node 목록으로 돌아가기/ })
    expect(back).toHaveAttribute('href', '/nodes')
  })

  it('uses POST refresh response directly and re-fetches node metadata', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.getNode)
      .mockResolvedValueOnce(makeNode({ status: 'DEGRADED' }))
      .mockResolvedValueOnce(makeNode({ status: 'ONLINE' }))
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({
        host: {
          sampled_at: '2026-10-06T00:00:00Z',
          cpu_utilization_pct: 1,
          ram_total_mb: 65536,
          ram_used_mb: 1000,
          ram_free_mb: 64536,
          disk_total_mb: 512000,
          disk_used_mb: 1000,
          disk_free_mb: 511000,
        },
      }),
    )
    vi.mocked(nodesApi.refreshNodeResources).mockResolvedValue(
      makeResources({
        host: {
          sampled_at: '2026-10-06T02:00:00Z',
          cpu_utilization_pct: 55,
          ram_total_mb: 65536,
          ram_used_mb: 20000,
          ram_free_mb: 45536,
          disk_total_mb: 512000,
          disk_used_mb: 2000,
          disk_free_mb: 510000,
        },
      }),
    )

    renderDetail()
    expect(await screen.findByText('1.0%')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '리소스 갱신' }))
    expect(await screen.findByText('55.0%')).toBeInTheDocument()
    expect(nodesApi.refreshNodeResources).toHaveBeenCalledTimes(1)
    // no redundant immediate resources GET after POST
    expect(nodesApi.getNodeResources).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(nodesApi.getNode).toHaveBeenCalledTimes(2)
    })
    expect(screen.getByText(/온라인/)).toBeInTheDocument()
  })

  it('preserves prior snapshot on POST failure', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(makeResources())
    vi.mocked(nodesApi.refreshNodeResources).mockRejectedValue(
      new ApiError('agent unreachable', { status: 502 }),
    )

    renderDetail()
    expect(await screen.findByText('12.5%')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '리소스 갱신' }))
    expect(await screen.findByText('agent unreachable')).toBeInTheDocument()
    expect(screen.getByText('12.5%')).toBeInTheDocument()
  })

  it('prevents concurrent resource refresh clicks', async () => {
    const user = userEvent.setup()
    let resolveRefresh!: (v: NodeResourcesLatest) => void
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(makeResources())
    vi.mocked(nodesApi.refreshNodeResources).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve
        }),
    )

    renderDetail()
    await screen.findByText('12.5%')

    const btn = screen.getByRole('button', { name: '리소스 갱신' })
    await user.click(btn)
    expect(await screen.findByRole('button', { name: '갱신 중…' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '갱신 중…' }))
    expect(nodesApi.refreshNodeResources).toHaveBeenCalledTimes(1)

    resolveRefresh(makeResources())
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: '리소스 갱신' }),
      ).not.toBeDisabled()
    })
  })

  it('renders unknown GPU status safely', async () => {
    const gpu0 = makeGpu({
      id: 'g0',
      device_index: 0,
      model_name: 'GPU-A',
      status: 'FUTURE_STATUS',
    })
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode({ gpus: [gpu0] }))
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({ gpus: [{ gpu: gpu0, snapshot: null }] }),
    )

    renderDetail()
    expect(
      await screen.findByTitle('FUTURE_STATUS'),
    ).toBeInTheDocument()
  })

  it('shows empty host snapshot guidance', async () => {
    vi.mocked(nodesApi.getNode).mockResolvedValue(makeNode())
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(
      makeResources({ host: null }),
    )

    renderDetail()
    expect(
      await screen.findByText(/아직 수집된 Host 자원 정보가 없습니다/),
    ).toBeInTheDocument()
  })

  it('warns when metadata re-fetch fails after successful POST', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.getNode)
      .mockResolvedValueOnce(makeNode({ status: 'DEGRADED' }))
      .mockRejectedValueOnce(new ApiError('meta reload failed', { status: 500 }))
    vi.mocked(nodesApi.getNodeResources).mockResolvedValue(makeResources())
    vi.mocked(nodesApi.refreshNodeResources).mockResolvedValue(
      makeResources({
        host: {
          sampled_at: '2026-10-06T03:00:00Z',
          cpu_utilization_pct: 77,
          ram_total_mb: 65536,
          ram_used_mb: 1000,
          ram_free_mb: 64536,
          disk_total_mb: 512000,
          disk_used_mb: 1000,
          disk_free_mb: 511000,
        },
      }),
    )

    renderDetail()
    await screen.findByText('12.5%')
    await user.click(screen.getByRole('button', { name: '리소스 갱신' }))
    expect(await screen.findByText('77.0%')).toBeInTheDocument()
    expect(screen.getByText(/자원 수집은 성공했지만/)).toBeInTheDocument()
    expect(screen.getByText(/저하/)).toBeInTheDocument()
  })
})
