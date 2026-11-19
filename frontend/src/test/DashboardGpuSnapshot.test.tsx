import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { DashboardGpuNode } from '../api/dashboard'
import { DashboardGpuSnapshot } from '../components/DashboardGpuSnapshot'

afterEach(cleanup)

const gpu = {
  id: 'gpu-0',
  node_id: 'node-a',
  gpu_uuid: 'test-uuid',
  device_index: 0,
  model_name: 'RTX A4000',
  vram_total_mb: 16384,
  compute_capability: null,
  safety_margin_mb: 1024,
  status: 'AVAILABLE',
  last_seen_at: null,
  created_at: '2020-01-01T00:00:00Z',
  updated_at: '2020-01-01T00:00:00Z',
}

const measured: DashboardGpuNode = {
  nodeId: 'node-a',
  nodeName: 'GPU 서버 A',
  error: null,
  resources: {
    node_id: 'node-a',
    source: 'persisted_snapshot',
    is_live: false,
    host: null,
    gpus: [{
      gpu,
      snapshot: {
        sampled_at: '2020-01-01T00:00:00Z',
        vram_total_mb: 16384,
        vram_used_mb: 0,
        vram_free_mb: 16384,
        gpu_utilization_pct: 0,
        memory_utilization_pct: null,
        temperature_c: null,
        power_w: null,
      },
    }, {
      gpu: { ...gpu, id: 'gpu-1', device_index: 1 },
      snapshot: null,
    }],
  },
}

function show(nodes: DashboardGpuNode[] | null, truncated = false) {
  return render(
    <MemoryRouter>
      <DashboardGpuSnapshot nodes={nodes} truncated={truncated} />
    </MemoryRouter>,
  )
}

describe('DashboardGpuSnapshot', () => {
  it('renders 0 as measured, null as missing, and old persisted timestamp', () => {
    show([measured])
    const region = screen.getByTestId('dashboard-gpu-summary')
    expect(within(region).getByText('GPU 0 · RTX A4000')).toBeInTheDocument()
    expect(within(region).getByText('0 MB / 16.0 GiB')).toBeInTheDocument()
    expect(within(region).getByText('0.0%')).toBeInTheDocument()
    expect(within(region).getByText(/오래된 저장값/)).toBeInTheDocument()
    expect(within(region).getByText(/측정값 미수집/)).toBeInTheDocument()
    expect(within(region).getByText(/실시간 GPU 상태가 아니며/)).toBeInTheDocument()
    expect(within(region).getByRole('link', { name: 'GPU 서버 A' }))
      .toHaveAttribute('href', '/nodes/node-a')
  })

  it('isolates per-node API errors and clearly labels bounded inventory', () => {
    show([
      { nodeId: 'node-b', nodeName: '장애 서버', resources: null, error: '503' },
      measured,
    ], true)
    const region = screen.getByTestId('dashboard-gpu-summary')
    expect(within(region).getByText(/앞의 5대만 표시/)).toBeInTheDocument()
    expect(within(region).getByText(/이 서버의 GPU 자원 확인 불가/)).toBeInTheDocument()
    expect(within(region).getByText('GPU 0 · RTX A4000')).toBeInTheDocument()
  })

  it('distinguishes failed inventory from genuinely empty inventory', () => {
    const { unmount } = show(null)
    expect(screen.getByText(/GPU 스냅샷 확인 불가/)).toBeInTheDocument()
    unmount()
    show([])
    expect(screen.getByText('등록된 서버가 없습니다.')).toBeInTheDocument()
    expect(screen.queryByText(/확인 불가/)).not.toBeInTheDocument()
  })
})
