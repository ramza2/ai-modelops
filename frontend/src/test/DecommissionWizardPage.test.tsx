import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Deployment, ModelSummary, ModelVersion, OperationDetail } from '../api/types'
import { DecommissionWizardPage } from '../pages/DecommissionWizardPage'
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

function makeVersion(): ModelVersion {
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
  }
}

function makeModel(): ModelSummary {
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
  }
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

  it('renders inspect from persisted status and shows active-route blocker', async () => {
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
      makeStatus(),
    )
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())

    renderWizard()
    expect(
      await screen.findByRole('heading', {
        name: /Decommission: chat-prod/,
      }),
    ).toBeInTheDocument()
    expect(screen.getByText(/ACTIVE_ROUTE/)).toBeInTheDocument()
    expect(
      screen.getAllByText(/Unpublished ≠ Stopped ≠ Removed/).length,
    ).toBeGreaterThan(0)
  })

  it('forces Unpublish first when active route exists', async () => {
    const user = userEvent.setup()
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
      makeStatus(),
    )
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())

    renderWizard()
    await screen.findByText(/1\. Inspect/)
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText(/2\. Unpublish/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Unpublish + verify Gateway' }),
    ).toBeInTheDocument()
    expect(screen.getByText('chat-prod', { selector: 'strong' })).toBeInTheDocument()
  })

  it('disables Remove until stopped and shows purge confirmation', async () => {
    const user = userEvent.setup()
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
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
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ runtime_status: 'RUNNING' }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())

    renderWizard('/deployments/d1/decommission?step=remove')
    expect(await screen.findByText(/4\. Remove Container/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Enqueue REMOVE' })).toBeDisabled()

    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
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
    cleanup()
    renderWizard('/deployments/d1/decommission?step=purge')
    expect(
      await screen.findByText(/6\. Purge Cache \(optional, destructive\)/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Retire ≠ Purge/)).toBeInTheDocument()
    expect(screen.getByPlaceholderText('PURGE')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('PURGE'), 'NOPE')
    expect(
      screen.getByRole('button', { name: 'Purge local cache' }),
    ).not.toBeDisabled()
  })

  it('resumes Stop operation from URL operation_id', async () => {
    const op: OperationDetail = {
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
    }
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
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
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(makeDeployment())
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())
    vi.mocked(operationsApi.getOperation).mockResolvedValue(op)

    renderWizard(
      '/deployments/d1/decommission?step=stop&operation_id=op-stop-1',
    )
    expect(await screen.findByText(/3\. Stop/)).toBeInTheDocument()
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
  })

  it('disables Retire until route/container safe', async () => {
    vi.mocked(decommissionApi.getDecommissionStatus).mockResolvedValue(
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
    vi.mocked(deploymentsApi.getDeployment).mockResolvedValue(
      makeDeployment({ runtime_status: 'STOPPED' }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(makeVersion())
    vi.mocked(modelsApi.getModel).mockResolvedValue(makeModel())

    renderWizard('/deployments/d1/decommission?step=retire')
    expect(await screen.findByText(/5\. Retire Deployment/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retire' })).toBeDisabled()
  })
})
