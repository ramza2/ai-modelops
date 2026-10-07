import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MemoryRouter,
  Route,
  Routes,
  useNavigate,
  useSearchParams,
} from 'react-router-dom'
import { ApiError } from '../api/client'
import type {
  CapacityProfile,
  RuntimeHistoryResponse,
  RuntimeLatestResponse,
} from '../api/types'
import { DeploymentObservabilityPage } from '../pages/DeploymentObservabilityPage'
import * as observabilityApi from '../api/observability'

function LocationProbe() {
  const [params] = useSearchParams()
  return <div data-testid="location-search">{params.toString()}</div>
}

function NavTo({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(to)}>
      {label}
    </button>
  )
}

function makeLatest(
  overrides: Partial<RuntimeLatestResponse['items'][number]> = {},
): RuntimeLatestResponse {
  return {
    items: [
      {
        deployment_id: 'dep-a',
        deployment_name: 'chat-a',
        sampled_at: '2026-10-07T00:00:00Z',
        availability: 'AVAILABLE',
        kv_cache_usage_ratio: 0.2,
        num_requests_running: 1,
        num_requests_waiting: 0,
        prompt_tokens_total: 10,
        generation_tokens_total: 5,
        missing_metrics: [],
        error_code: null,
        error_message: null,
        runtime_instance: {
          container_id: 'ctr-a',
          started_at: '2026-10-06T00:00:00Z',
          restart_count: 0,
        },
        ...overrides,
      },
    ],
  }
}

function makeHistory(
  deploymentId = 'dep-a',
): RuntimeHistoryResponse {
  return {
    deployment_id: deploymentId,
    hours: 24,
    limit: 500,
    ordering: 'oldest_to_newest',
    items: [
      {
        deployment_id: deploymentId,
        sampled_at: '2026-10-07T00:00:00Z',
        availability: 'AVAILABLE',
        kv_cache_usage_ratio: 0.1,
        num_requests_running: 0,
        num_requests_waiting: 0,
        prompt_tokens_total: 1,
        generation_tokens_total: 1,
        error_code: null,
        error_message: null,
        runtime_instance: null,
      },
      {
        deployment_id: deploymentId,
        sampled_at: '2026-10-07T01:00:00Z',
        availability: 'PARTIAL',
        kv_cache_usage_ratio: 0.3,
        num_requests_running: 2,
        num_requests_waiting: 1,
        prompt_tokens_total: 3,
        generation_tokens_total: 2,
        error_code: null,
        error_message: null,
        runtime_instance: {
          container_id: 'ctr-later',
          started_at: '2026-10-06T00:00:00Z',
          restart_count: 1,
        },
      },
    ],
  }
}

function makeProfile(deploymentId = 'dep-a'): CapacityProfile {
  return {
    hours: 24,
    deployment: {
      id: deploymentId,
      name: `chat-${deploymentId}`,
      deployment_type: 'MANAGED',
      runtime_status: 'RUNNING',
      health_status: 'HEALTHY',
    },
    model: {
      model_id: 'm1',
      model_name: 'demo',
      model_version_id: 'v1',
      version_label: '1.0.0',
      runtime_type: 'VLLM',
      runtime_image: 'example/runtime:tag',
      served_model_name: 'served',
      expected_idle_vram_mb: 8000,
      expected_peak_vram_mb: 16000,
    },
    gpu_count: 2,
    gpu_assignments: [
      {
        device_order: 0,
        device_index: 0,
        model_name: 'A4000',
        vram_total_mb: 16384,
        safety_margin_mb: 1024,
      },
      {
        device_order: 1,
        device_index: 1,
        model_name: 'A4000',
        vram_total_mb: 16384,
        safety_margin_mb: 1024,
      },
    ],
    configuration: {
      runtime_observation_sampled_at: '2026-10-07T00:00:00Z',
      runtime_instance: {
        container_id: 'ctr-a',
        started_at: '2026-10-06T00:00:00Z',
        restart_count: 0,
      },
      settings: {
        max_model_len: {
          requested: 8192,
          requested_source: 'DEPLOYMENT_CONFIG',
          observed_explicit: 8192,
          comparison_status: 'MATCH',
        },
        max_num_seqs: {
          requested: 8,
          requested_source: 'DEPLOYMENT_CONFIG',
          observed_explicit: null,
          comparison_status: 'REQUESTED_NOT_OBSERVED',
        },
        tensor_parallel_size: {
          requested: 1,
          requested_source: 'MODEL_VERSION_RUNTIME_CONFIG',
          observed_explicit: 1,
          comparison_status: 'MATCH',
        },
        gpu_memory_utilization: {
          requested: 0.9,
          requested_source: 'UNSET',
          observed_explicit: null,
          comparison_status: 'UNSET',
        },
        dtype: {
          requested: 'auto',
          requested_source: 'MODEL_VERSION_RUNTIME_CONFIG',
          observed_explicit: 'auto',
          comparison_status: 'MATCH',
        },
        quantization: {
          requested: null,
          requested_source: 'UNSET',
          observed_explicit: null,
          comparison_status: 'UNSET',
        },
        scheduling_policy: {
          requested: 'priority',
          requested_source: 'DEPLOYMENT_CONFIG',
          observed_explicit: 'priority',
          comparison_status: 'MATCH',
        },
      },
    },
    invocations: {
      hours: 24,
      request_count: 20,
      success_count: 18,
      error_count: 2,
      tokenized_request_count: 10,
      input_tokens_avg: 50,
      input_tokens_p50: 40,
      input_tokens_p95: 90,
      input_tokens_max: 120,
      output_tokens_avg: 15,
      total_tokens_avg: 65,
      latency_ms_avg: 100,
      latency_ms_p50: 80,
      latency_ms_p95: 200,
      latency_ms_max: 400,
    },
    runtime_analytics: {
      deployment_id: deploymentId,
      snapshot_count: 2,
      interval_count: 1,
      boundaries: {
        reset_boundary_count: 1,
        identity_unknown_interval_count: 0,
      },
      gauges: {
        kv_cache_usage_ratio: { sample_count: 2, avg: 0.2, max: 0.3 },
        num_requests_running: { sample_count: 2, avg: 1, max: 2 },
      },
      tokens: {
        prompt_tokens: {
          delta: 100,
          interval_count: 1,
          covered_seconds: 60,
          observed_tokens_per_second: 1.6,
          counter_regression_interval_count: 0,
        },
      },
      histograms: {
        e2e_latency_seconds: {
          observation_count: 10,
          mean_seconds: 0.2,
          p50_seconds: 0.15,
          p95_seconds: 0.4,
          interval_count: 1,
          covered_seconds: 60,
          histogram_regression_interval_count: 0,
          bucket_schema_change_interval_count: 0,
        },
      },
    },
  }
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route
          path="/observability/deployments/:deploymentId"
          element={<DeploymentObservabilityPage />}
        />
      </Routes>
    </MemoryRouter>,
  )
}

function renderNavigable(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <NavTo to="/observability/deployments/dep-b" label="go-dep-b" />
      <Routes>
        <Route
          path="/observability/deployments/:deploymentId"
          element={<DeploymentObservabilityPage />}
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('DeploymentObservabilityPage', () => {
  beforeEach(() => {
    vi.spyOn(observabilityApi, 'getRuntimeLatest')
    vi.spyOn(observabilityApi, 'getRuntimeHistory')
    vi.spyOn(observabilityApi, 'getCapacityProfile')
    vi.spyOn(observabilityApi, 'getInvocationSummary')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('loads latest/history/capacity-profile and never calls /analytics', async () => {
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue(makeLatest())
    vi.mocked(observabilityApi.getRuntimeHistory).mockResolvedValue(
      makeHistory(),
    )
    vi.mocked(observabilityApi.getCapacityProfile).mockResolvedValue(
      makeProfile(),
    )
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/observability/deployments/dep-a')
    expect(await screen.findByText(/ctr-a/)).toBeInTheDocument()
    expect(screen.getByText(/classic histogram bucket estimate/i)).toBeInTheDocument()
    expect(screen.getByText(/scheduler\/runner sequence capacity/i)).toBeInTheDocument()
    expect(screen.getByText(/REQUESTED_NOT_OBSERVED/)).toBeInTheDocument()
    expect(screen.getAllByText('16384')).toHaveLength(2)
    expect(screen.queryByText('32768')).not.toBeInTheDocument()
    expect(observabilityApi.getRuntimeLatest).toHaveBeenCalledWith(
      'dep-a',
      expect.anything(),
    )
    expect(observabilityApi.getRuntimeHistory).toHaveBeenCalledWith(
      'dep-a',
      expect.objectContaining({ hours: 24, limit: 500 }),
    )
    expect(observabilityApi.getCapacityProfile).toHaveBeenCalledWith(
      'dep-a',
      24,
      expect.anything(),
    )
    expect(observabilityApi.getInvocationSummary).not.toHaveBeenCalled()
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/analytics'))).toBe(false)
    expect(screen.queryByText(/metric_sources/)).not.toBeInTheDocument()
    expect(document.querySelector('pre')).toBeNull()
  })

  it.each([
    [
      '/observability/deployments/dep-a?hours=12&limit=100',
      'hours=12&limit=100',
    ],
    [
      '/observability/deployments/dep-a?hours=37&limit=777',
      'hours=37&limit=777',
    ],
    ['/observability/deployments/dep-a?hours=999', ''],
    ['/observability/deployments/dep-a?limit=0', ''],
    [
      '/observability/deployments/dep-a?hours=048&limit=050',
      'hours=48&limit=50',
    ],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue({
      items: [],
    })
    vi.mocked(observabilityApi.getRuntimeHistory).mockResolvedValue({
      deployment_id: 'dep-a',
      hours: 24,
      limit: 500,
      ordering: 'oldest_to_newest',
      items: [],
    })
    vi.mocked(observabilityApi.getCapacityProfile).mockResolvedValue(
      makeProfile(),
    )
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('applies arbitrary valid hours/limit through number controls', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue(makeLatest())
    vi.mocked(observabilityApi.getRuntimeHistory).mockResolvedValue(
      makeHistory(),
    )
    vi.mocked(observabilityApi.getCapacityProfile).mockResolvedValue(
      makeProfile(),
    )
    renderAt('/observability/deployments/dep-a')
    await screen.findByText(/ctr-a/)
    await user.clear(screen.getByLabelText(/Hours/))
    await user.type(screen.getByLabelText(/Hours/), '37')
    await user.clear(screen.getByLabelText(/History limit/))
    await user.type(screen.getByLabelText(/History limit/), '777')
    await user.click(screen.getByRole('button', { name: '적용' }))
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        'hours=37&limit=777',
      )
    })
    await waitFor(() => {
      expect(observabilityApi.getRuntimeHistory).toHaveBeenCalledWith(
        'dep-a',
        expect.objectContaining({ hours: 37, limit: 777 }),
      )
      expect(observabilityApi.getCapacityProfile).toHaveBeenCalledWith(
        'dep-a',
        37,
        expect.anything(),
      )
    })
    expect(observabilityApi.getInvocationSummary).not.toHaveBeenCalled()
  })

  it('keeps same-id non-404 refresh snapshot and clears on identity change 500', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getRuntimeLatest)
      .mockResolvedValueOnce(makeLatest())
      .mockRejectedValueOnce(new ApiError('latest boom', { status: 500 }))
      .mockRejectedValueOnce(new ApiError('dep-b boom', { status: 500 }))
    vi.mocked(observabilityApi.getRuntimeHistory)
      .mockResolvedValueOnce(makeHistory())
      .mockRejectedValueOnce(new ApiError('history boom', { status: 500 }))
      .mockRejectedValueOnce(new ApiError('dep-b boom', { status: 500 }))
    vi.mocked(observabilityApi.getCapacityProfile)
      .mockResolvedValueOnce(makeProfile())
      .mockRejectedValueOnce(new ApiError('profile boom', { status: 500 }))
      .mockRejectedValueOnce(new ApiError('dep-b boom', { status: 500 }))
    renderNavigable('/observability/deployments/dep-a')
    expect(await screen.findByText(/ctr-a/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText(/latest boom/)).toBeInTheDocument()
    expect(screen.getByText(/ctr-a/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'go-dep-b' }))
    await waitFor(() => {
      expect(screen.getAllByText(/dep-b boom/).length).toBeGreaterThan(0)
    })
    expect(screen.queryByText(/ctr-a/)).not.toBeInTheDocument()
    expect(screen.queryByText('chat-dep-a')).not.toBeInTheDocument()
  })

  it('clears prior identity on navigated 404', async () => {
    const user = userEvent.setup()
    vi.mocked(observabilityApi.getRuntimeLatest)
      .mockResolvedValueOnce(makeLatest())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(observabilityApi.getRuntimeHistory)
      .mockResolvedValueOnce(makeHistory())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(observabilityApi.getCapacityProfile)
      .mockResolvedValueOnce(makeProfile())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    renderNavigable('/observability/deployments/dep-a')
    expect(await screen.findByText(/ctr-a/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'go-dep-b' }))
    expect(
      await screen.findByText('Deployment를 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/ctr-a/)).not.toBeInTheDocument()
  })

  it('treats any section 404 as authoritative and ignores delayed siblings', async () => {
    const user = userEvent.setup()
    let resolveHistory!: (value: RuntimeHistoryResponse) => void
    let resolveProfile!: (value: CapacityProfile) => void
    vi.mocked(observabilityApi.getRuntimeLatest)
      .mockResolvedValueOnce(makeLatest())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(observabilityApi.getRuntimeHistory)
      .mockResolvedValueOnce(makeHistory())
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveHistory = resolve
          }),
      )
    vi.mocked(observabilityApi.getCapacityProfile)
      .mockResolvedValueOnce(makeProfile())
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveProfile = resolve
          }),
      )
    renderAt('/observability/deployments/dep-a')
    expect(await screen.findByText(/ctr-a/)).toBeInTheDocument()
    expect(screen.getByText('chat-dep-a')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('Deployment를 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/ctr-a/)).not.toBeInTheDocument()
    expect(screen.queryByText('chat-dep-a')).not.toBeInTheDocument()
    resolveHistory(makeHistory())
    resolveProfile(makeProfile())
    await waitFor(() => {
      expect(
        screen.getByText('Deployment를 찾을 수 없습니다.'),
      ).toBeInTheDocument()
    })
    expect(screen.queryByText(/ctr-a/)).not.toBeInTheDocument()
    expect(screen.queryByText('chat-dep-a')).not.toBeInTheDocument()
    expect(screen.queryByText(/PARTIAL/)).not.toBeInTheDocument()
  })

  it('renders history oldest→newest and capacity comparison statuses', async () => {
    vi.mocked(observabilityApi.getRuntimeLatest).mockResolvedValue(makeLatest())
    vi.mocked(observabilityApi.getRuntimeHistory).mockResolvedValue(
      makeHistory(),
    )
    vi.mocked(observabilityApi.getCapacityProfile).mockResolvedValue(
      makeProfile(),
    )
    renderAt('/observability/deployments/dep-a')
    await screen.findByText(/ctr-later/)
    const historyHeading = screen.getByRole('heading', {
      name: /History \(oldest → newest\)/,
    })
    const historySection = historyHeading.closest('section')
    expect(historySection).not.toBeNull()
    const historyTable = within(historySection as HTMLElement).getByRole(
      'table',
    )
    const rows = within(historyTable).getAllByRole('row')
    expect(rows[1]?.textContent).toMatch(/AVAILABLE/)
    expect(rows[2]?.textContent).toMatch(/PARTIAL/)
    expect(screen.getAllByText(/MATCH/).length).toBeGreaterThan(0)
  })
})
