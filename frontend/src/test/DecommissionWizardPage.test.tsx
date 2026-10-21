import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Deployment, ModelSummary, ModelVersion, OperationDetail } from '../api/types'
import {
  DecommissionWizardPage,
  isGatewayVerificationPassed,
} from '../pages/DecommissionWizardPage'
import * as decommissionApi from '../api/decommission'
import * as deploymentsApi from '../api/deployments'
import * as modelsApi from '../api/models'
import * as operationsApi from '../api/operations'
import type { DecommissionStatus } from '../api/decommission'

function makeStatus(
  overrides: Partial<DecommissionStatus> = {},
): DecommissionStatus {
  return {
    deployment_id: 'd1',
    deployment_type: 'MANAGED',
    desired_state: 'RUNNING',
    runtime_status: 'RUNNING',
    health_status: 'HEALTHY',
    retired_at: null,
    active_routes: [
      {
        endpoint_id: 'ep-1',
        alias: 'chat-prod',
        route_id: 'r-1',
        rewrite_model_name: 'org/demo',
        routing_version: 2,
      },
    ],
    container_present: true,
    gpu_assignments: [],
    source_cache: {
      cache_id: 'cache-1',
      status: 'READY',
      local_path: '/var/modelops/cache/org/demo/abc',
      revision: 'a'.repeat(40),
      size_bytes: 1024,
    },
    active_operation: null,
    can_unpublish: true,
    can_stop: false,
    can_remove_container: false,
    can_retire: false,
    can_purge_cache: false,
    blockers: [
      {
        code: 'ACTIVE_ROUTE',
        message: 'Unpublish ACTIVE Endpoint route before Stop.',
      },
    ],
    note: 'Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠ Purged ≠ Archived.',
    ...overrides,
  }
}

function makeDeployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: 'd1',
    name: 'chat-prod',
    model_version_id: 'v1',
    node_id: 'n1',
    deployment_type: 'MANAGED',
    desired_state: 'RUNNING',
    runtime_status: 'RUNNING',
    health_status: 'HEALTHY',
    container_id: 'ctr-1',
    container_name: 'ctr-chat',
    upstream_base_url: 'http://internal:8000',
    runtime_port: 8000,
    deployment_config: {},
    gpu_assignments: [],
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
    id: 'v1',
    model_id: 'm1',
    version_label: '1.0.0',
    source_repository: 'org/demo',
    source_revision: 'a'.repeat(40),
    quantization: null,
    dtype: null,
    runtime_type: 'VLLM',
    runtime_image: 'vllm/vllm-openai:latest',
    runtime_image_digest: null,
    served_model_name: 'org/demo',
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

function makeModel(overrides: Partial<ModelSummary> = {}): ModelSummary {
  return {
    id: 'm1',
    slug: 'demo',
    name: 'org/demo',
    model_type: 'LLM',
    provider: null,
    source_type: 'HUGGINGFACE',
    license_name: null,
    description: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makeOp(
  overrides: Partial<OperationDetail> = {},
): OperationDetail {
  return {
    id: 'op-stop-1',
    operation_type: 'STOP',
    status: 'RUNNING',
    switch_strategy: null,
    endpoint_alias_id: null,
    source_deployment_id: null,
    target_deployment_id: 'd1',
    current_step: 'STOP_CONTAINER',
    retry_of_operation_id: null,
    requested_by: null,
    request_reason: null,
    cancel_requested_at: null,
    created_at: '2026-10-08T00:00:00Z',
    started_at: '2026-10-08T00:00:01Z',
    finished_at: null,
    error: null,
    steps: [],
    ...overrides,
  }
}

function mockHappyLoad(status: DecommissionStatus = makeStatus()) {
  vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(status)
  vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
    makeDeployment({
      runtime_status: status.runtime_status,
      desired_state: status.desired_state,
      retired_at: status.retired_at,
    }),
  )
  vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
  vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
}

function renderWizard(path = '/deployments/d1/decommission?step=inspect') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/deployments/:deploymentId/decommission"
          element={<DecommissionWizardPage />}
        />
        <Route path="/operations/:operationId" element={<div>Op</div>} />
        <Route path="/deployments" element={<div>Deployments</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function confirmAndRun(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByTestId('danger-confirm-dialog')
  await user.click(within(dialog).getByRole('checkbox'))
  await user.click(within(dialog).getByRole('button', { name: /실행/ }))
}

describe('DecommissionWizardPage', () => {
  beforeEach(() => {
    vi.spyOn(decommissionApi, 'getDecommissionStatus')
    vi.spyOn(decommissionApi, 'unpublishEndpoint')
    vi.spyOn(decommissionApi, 'removeDeployment')
    vi.spyOn(decommissionApi, 'retireDeployment')
    vi.spyOn(decommissionApi, 'purgeModelCache')
    vi.spyOn(decommissionApi, 'archiveModelVersion')
    vi.spyOn(decommissionApi, 'archiveModel')
    vi.spyOn(deploymentsApi, 'getDeployment')
    vi.spyOn(deploymentsApi, 'stopDeployment')
    vi.spyOn(modelsApi, 'getModelVersion')
    vi.spyOn(modelsApi, 'getModel')
    vi.spyOn(operationsApi, 'getOperation')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders Korean inspect and active-route blocker', async () => {
    mockHappyLoad()
    renderWizard()
    expect(
      await screen.findByRole('heading', {
        name: /모델 종료·정리: chat-prod/,
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /1\. 현재 상태 확인/ })).toBeInTheDocument()
    expect(screen.getByText(/ACTIVE_ROUTE/)).toBeInTheDocument()
    expect(
      screen.getAllByText(/API 연결 해제 ≠ 모델 중지/).length,
    ).toBeGreaterThan(0)
  })

  it('forces Unpublish first when active route exists', async () => {
    const user = userEvent.setup()
    mockHappyLoad()
    renderWizard()
    await screen.findByRole('heading', { name: /1\. 현재 상태 확인/ })
    await user.click(screen.getByRole('button', { name: '다음' }))
    expect(await screen.findByRole('heading', { name: /2\. API 연결 해제/ })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'API 연결 해제 확인…' }),
    ).toBeInTheDocument()
    expect(screen.getByText('chat-prod', { selector: 'strong' })).toBeInTheDocument()
  })

  it('blocks Stop execute while ACTIVE Route remains even via URL step', async () => {
    mockHappyLoad(
      makeStatus({
        can_stop: false,
        can_unpublish: true,
      }),
    )
    renderWizard('/deployments/d1/decommission?step=stop')
    expect(await screen.findByRole('heading', { name: /3\. 모델 실행 중지/ })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '모델 중지 확인…' }),
    ).toBeDisabled()
    expect(screen.getByText('ACTIVE Route 잔존')).toBeInTheDocument()
    expect(
      screen.getByText(/API 연결 해제 전에는 모델 중지를 실행할 수 없습니다/),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '다음: 모델 컨테이너 제거' }),
    ).toBeDisabled()
  })

  it('does not advance from Stop on Operation SUCCEEDED alone', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        runtime_status: 'RUNNING',
        blockers: [],
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(
      makeOp({ status: 'SUCCEEDED', finished_at: '2026-10-08T00:01:00Z' }),
    )
    renderWizard(
      '/deployments/d1/decommission?step=stop&operation_id=op-stop-1',
    )
    expect(await screen.findByRole('heading', { name: /3\. 모델 실행 중지/ })).toBeInTheDocument()
    expect(
      await screen.findByText(/STOP 완료 ≠ Runtime STOPPED/),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '다음: 모델 컨테이너 제거' }),
    ).toBeDisabled()
  })

  it('treats container_present null as UNKNOWN and blocks Retire advance', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        can_remove_container: false,
        runtime_status: 'STOPPED',
        container_present: null,
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=remove')
    expect(
      await screen.findByRole('heading', { name: /4\. 모델 컨테이너 제거/ }),
    ).toBeInTheDocument()
    expect(screen.getByText(/확인 불가 \(UNKNOWN/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '다음: 모델 배포 퇴역 처리' }),
    ).toBeDisabled()
  })

  it('disables Remove until Backend allows and shows purge PURGE gate', async () => {
    const user = userEvent.setup()
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        can_remove_container: false,
        runtime_status: 'RUNNING',
        blockers: [
          {
            code: 'RUNTIME_STILL_RUNNING',
            message: 'Stop the managed runtime before Remove.',
          },
        ],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=remove')
    expect(
      await screen.findByRole('heading', { name: /4\. 모델 컨테이너 제거/ }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '컨테이너 제거 확인…' }),
    ).toBeDisabled()

    cleanup()
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        can_remove_container: false,
        can_retire: true,
        can_purge_cache: true,
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=purge')
    expect(
      await screen.findByRole('heading', { name: /6\. 로컬 모델 캐시 삭제/ }),
    ).toBeInTheDocument()
    expect(screen.getByText(/정확히 PURGE/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '로컬 캐시 삭제 확인…' }))
    const dialog = await screen.findByTestId('danger-confirm-dialog')
    expect(within(dialog).getByPlaceholderText('PURGE')).toBeInTheDocument()
    await user.type(within(dialog).getByPlaceholderText('PURGE'), 'NOPE')
    await user.click(within(dialog).getByRole('checkbox'))
    expect(
      within(dialog).getByRole('button', { name: '캐시 삭제 실행' }),
    ).toBeDisabled()
  })

  it('resumes Stop operation from URL operation_id', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        runtime_status: 'RUNNING',
        active_operation: {
          operation_id: 'op-stop-1',
          operation_type: 'STOP',
          status: 'RUNNING',
        },
        blockers: [
          {
            code: 'ACTIVE_LIFECYCLE_OPERATION',
            message: 'An active lifecycle Operation is in progress.',
          },
        ],
      }),
    )
    vi.mocked(operationsApi.getOperation).mockResolvedValue(makeOp())

    renderWizard(
      '/deployments/d1/decommission?step=stop&operation_id=op-stop-1',
    )
    expect(await screen.findByRole('heading', { name: /3\. 모델 실행 중지/ })).toBeInTheDocument()
    await waitFor(() => {
      expect(operationsApi.getOperation).toHaveBeenCalledWith(
        'op-stop-1',
        expect.anything(),
      )
    })
    expect(screen.getByRole('link', { name: 'op-stop-1' })).toHaveAttribute(
      'href',
      '/operations/op-stop-1',
    )
    expect(
      screen.getByRole('button', { name: '모델 중지 확인…' }),
    ).toBeDisabled()
  })

  it('disables Retire until route/container safe', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_retire: false,
        container_present: true,
        runtime_status: 'STOPPED',
        blockers: [
          {
            code: 'CONTAINER_STILL_PRESENT',
            message: 'Remove managed container before Retire.',
          },
        ],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=retire')
    expect(
      await screen.findByRole('heading', { name: /5\. 모델 배포 퇴역 처리/ }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '배포 퇴역 확인…' }),
    ).toBeDisabled()
  })

  it('revalidates before Unpublish and aborts when status goes stale', async () => {
    const user = userEvent.setup()
    mockHappyLoad()
    renderWizard('/deployments/d1/decommission?step=unpublish')
    await screen.findByRole('button', { name: 'API 연결 해제 확인…' })
    await user.click(screen.getByRole('button', { name: 'API 연결 해제 확인…' }))

    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: true,
        blockers: [],
      }),
    )
    await confirmAndRun(user)
    expect(await screen.findByText(/상태가 변경되었습니다/)).toBeInTheDocument()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('does not execute when preflight refresh fails', async () => {
    const user = userEvent.setup()
    mockHappyLoad()
    renderWizard('/deployments/d1/decommission?step=unpublish')
    await user.click(
      await screen.findByRole('button', { name: 'API 연결 해제 확인…' }),
    )
    vi.mocked(decommissionApi.getDecommissionStatus).mockRejectedValueOnce(
      new Error('network down'),
    )
    await confirmAndRun(user)
    expect(
      await screen.findByText(/최신 상태 조회에 실패|network down/),
    ).toBeInTheDocument()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
  })

  it('keeps operation_id when enqueue succeeds but detail fetch fails', async () => {
    const user = userEvent.setup()
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: true,
        blockers: [],
      }),
    )
    vi.mocked(deploymentsApi.stopDeployment).mockResolvedValue({
      id: 'op-new-stop',
      operation_type: 'STOP',
      status: 'QUEUED',
      target_deployment_id: 'd1',
      current_step: null,
      created_at: '2026-10-08T00:00:00Z',
      started_at: null,
      finished_at: null,
      error: null,
    })
    vi.mocked(operationsApi.getOperation).mockRejectedValue(
      new Error('detail unavailable'),
    )

    renderWizard('/deployments/d1/decommission?step=stop')
    await user.click(
      await screen.findByRole('button', { name: '모델 중지 확인…' }),
    )
    await confirmAndRun(user)
    expect(await screen.findByText(/접수되었을 수 있습니다/)).toBeInTheDocument()
    expect(deploymentsApi.stopDeployment).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(operationsApi.getOperation).toHaveBeenCalledWith(
        'op-new-stop',
      )
    })
    // Do not urge an immediate second stop after uncertain detail fetch.
    expect(screen.queryByText(/버튼을 다시 시도/)).not.toBeInTheDocument()
  })

  it('allows skipping purge and shows partial done summary', async () => {
    const user = userEvent.setup()
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        can_remove_container: false,
        can_retire: false,
        can_purge_cache: true,
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=purge')
    await user.click(
      await screen.findByRole('button', {
        name: '건너뛰기 / 다음: 모델·버전 보관',
      }),
    )
    expect(await screen.findByRole('heading', { name: /7\. 모델·버전 보관/ })).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: '정리 결과 확인으로' }),
    )
    expect(await screen.findByRole('heading', { name: /8\. 정리 결과 확인/ })).toBeInTheDocument()
    expect(screen.getByText(/· 선택 단계 건너뜀/)).toBeInTheDocument()
    expect(
      screen.getByText(/필수 정리 항목은 확인되었습니다/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/모든 정리 완료/)).not.toBeInTheDocument()
  })

  it('blocks archive model until version archived', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        can_retire: false,
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=archive')
    expect(await screen.findByRole('heading', { name: /7\. 모델·버전 보관/ })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '모델 보관 확인…' }),
    ).toBeDisabled()
    expect(
      screen.getByRole('button', { name: '버전 보관 확인…' }),
    ).not.toBeDisabled()
  })

  it('disables cancel while revalidate/execute is in flight', async () => {
    const user = userEvent.setup()
    let resolveStatus!: (value: DecommissionStatus) => void
    mockHappyLoad()
    renderWizard('/deployments/d1/decommission?step=unpublish')
    await user.click(
      await screen.findByRole('button', { name: 'API 연결 해제 확인…' }),
    )
    const dialog = await screen.findByTestId('danger-confirm-dialog')
    await user.click(within(dialog).getByRole('checkbox'))

    vi.mocked(decommissionApi.getDecommissionStatus).mockImplementation(
      () =>
        new Promise<DecommissionStatus>((resolve) => {
          resolveStatus = resolve
        }),
    )
    await user.click(
      within(dialog).getByRole('button', { name: '연결 해제 실행' }),
    )
    expect(
      within(dialog).getByRole('button', { name: '취소' }),
    ).toBeDisabled()
    expect(
      within(dialog).getByRole('button', { name: '실행 중…' }),
    ).toBeDisabled()
    // Fail closed on revalidate: abort without POST.
    resolveStatus(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: true,
        blockers: [],
      }),
    )
    expect(await screen.findByText(/상태가 변경되었습니다/)).toBeInTheDocument()
    expect(decommissionApi.unpublishEndpoint).not.toHaveBeenCalled()
    expect(
      within(screen.getByTestId('danger-confirm-dialog')).getByRole('button', {
        name: '취소',
      }),
    ).not.toBeDisabled()
  })

  it('dedupes rapid double confirm so Unpublish POST runs at most once', async () => {
    const user = userEvent.setup()
    mockHappyLoad()
    let resolveUnpublish!: (value: decommissionApi.UnpublishResult) => void
    vi.mocked(decommissionApi.unpublishEndpoint).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUnpublish = resolve
        }),
    )
    renderWizard('/deployments/d1/decommission?step=unpublish')
    await user.click(
      await screen.findByRole('button', { name: 'API 연결 해제 확인…' }),
    )
    const dialog = await screen.findByTestId('danger-confirm-dialog')
    await user.click(within(dialog).getByRole('checkbox'))
    const confirmBtn = within(dialog).getByRole('button', {
      name: '연결 해제 실행',
    })
    await user.click(confirmBtn)
    await user.click(confirmBtn)
    await waitFor(() => {
      expect(decommissionApi.unpublishEndpoint).toHaveBeenCalledTimes(1)
    })
    resolveUnpublish({
      endpoint: {
        id: 'ep-1',
        alias: 'chat-prod',
        api_type: 'CHAT',
      },
      previous_route: null,
      routing_version: 3,
      changed: true,
      gateway_verification: { status: 'PASSED' },
    })
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { name: /3\. 모델 실행 중지/ }),
      ).toBeInTheDocument()
    })
  })

  it('advances to Stop only when Gateway verification is PASSED', async () => {
    const user = userEvent.setup()
    let statusCalls = 0
    vi.mocked(decommissionApi.getDecommissionStatus).mockImplementation(
      async () => {
        statusCalls += 1
        // 1 load + 2 revalidate before POST keep fingerprint; later refresh clears routes.
        if (statusCalls <= 2) return makeStatus()
        return makeStatus({
          active_routes: [],
          can_unpublish: false,
          can_stop: true,
          blockers: [],
        })
      },
    )
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(decommissionApi.unpublishEndpoint).mockResolvedValue({
      endpoint: { id: 'ep-1', alias: 'chat-prod', api_type: 'CHAT' },
      previous_route: null,
      routing_version: 3,
      changed: true,
      gateway_verification: { status: 'PASSED' },
    })
    renderWizard('/deployments/d1/decommission?step=unpublish')
    await user.click(
      await screen.findByRole('button', { name: 'API 연결 해제 확인…' }),
    )
    await confirmAndRun(user)
    expect(
      await screen.findByRole('heading', { name: /3\. 모델 실행 중지/ }),
    ).toBeInTheDocument()
    expect(isGatewayVerificationPassed('PASSED')).toBe(true)
  })

  it.each([
    'ROUTING_PENDING',
    'ROUTE_STILL_ACTIVE',
    'GATEWAY_UNAVAILABLE',
    'SKIPPED',
  ])(
    'keeps Unpublish step and blocks Stop when Gateway verification is %s',
    async (gwStatus) => {
      const user = userEvent.setup()
      let statusCalls = 0
      vi.mocked(decommissionApi.getDecommissionStatus).mockImplementation(
        async () => {
          statusCalls += 1
          if (statusCalls <= 2) return makeStatus()
          return makeStatus({
            active_routes: [],
            can_unpublish: false,
            can_stop: true,
            blockers: [],
          })
        },
      )
      vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
      vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
      vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
      vi.mocked(decommissionApi.unpublishEndpoint).mockResolvedValue({
        endpoint: { id: 'ep-1', alias: 'chat-prod', api_type: 'CHAT' },
        previous_route: null,
        routing_version: 3,
        changed: true,
        gateway_verification: { status: gwStatus },
      })
      renderWizard('/deployments/d1/decommission?step=unpublish')
      await user.click(
        await screen.findByRole('button', { name: 'API 연결 해제 확인…' }),
      )
      await confirmAndRun(user)
      expect(
        await screen.findByText(/Gateway 검증이 확인되지 않았습니다/),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('heading', { name: /2\. API 연결 해제/ }),
      ).toBeInTheDocument()
      expect(isGatewayVerificationPassed(gwStatus)).toBe(false)
      await user.click(
        screen.getByRole('button', { name: /^3\. 모델 실행 중지$/ }),
      )
      expect(
        await screen.findByRole('heading', { name: /3\. 모델 실행 중지/ }),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: '모델 중지 확인…' }),
      ).toBeDisabled()
      expect(
        screen.getByText(/Gateway 검증 미완료 — Stop 차단/),
      ).toBeInTheDocument()
    },
  )

  it('aborts Archive Version when Deployment model_version_id changes', async () => {
    const user = userEvent.setup()
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        can_retire: false,
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=archive')
    await user.click(
      await screen.findByRole('button', { name: '버전 보관 확인…' }),
    )
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        model_version_id: 'v-changed',
        runtime_status: 'STOPPED',
        retired_at: '2026-10-08T00:00:00Z',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({ id: 'v-changed', model_id: 'm1' }),
    )
    await confirmAndRun(user)
    expect(
      await screen.findByText(/Model Version이 확인 대상과 다릅니다/),
    ).toBeInTheDocument()
    expect(decommissionApi.archiveModelVersion).not.toHaveBeenCalled()
  })

  it('aborts Archive Model when Version is no longer archived at revalidate', async () => {
    const user = userEvent.setup()
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
      makeStatus({
        active_routes: [],
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        can_retire: false,
        blockers: [],
      }),
    )
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({
        runtime_status: 'STOPPED',
        retired_at: '2026-10-08T00:00:00Z',
      }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({ archived_at: '2026-10-08T00:00:00Z' }),
    )
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())

    renderWizard('/deployments/d1/decommission?step=archive')
    await user.click(
      await screen.findByRole('button', { name: '모델 보관 확인…' }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({ archived_at: null }),
    )
    await confirmAndRun(user)
    expect(
      await screen.findByText(/아직 Archived가 아닙니다/),
    ).toBeInTheDocument()
    expect(decommissionApi.archiveModel).not.toHaveBeenCalled()
  })

  it('blocks Purge while a lifecycle Operation is active', async () => {
    mockHappyLoad(
      makeStatus({
        active_routes: [],
        can_unpublish: false,
        can_stop: false,
        can_remove_container: false,
        can_retire: false,
        can_purge_cache: true,
        runtime_status: 'STOPPED',
        container_present: false,
        retired_at: '2026-10-08T00:00:00Z',
        active_operation: {
          operation_id: 'op-rm-1',
          operation_type: 'REMOVE',
          status: 'RUNNING',
        },
        blockers: [],
      }),
    )
    renderWizard('/deployments/d1/decommission?step=purge')
    expect(
      await screen.findByRole('heading', { name: /6\. 로컬 모델 캐시 삭제/ }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '로컬 캐시 삭제 확인…' }),
    ).toBeDisabled()
    expect(
      screen.getByText(/진행 중인 Deployment Operation이 있습니다/),
    ).toBeInTheDocument()
  })
})
