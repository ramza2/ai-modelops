import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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
import {
  clearWizardDraft,
  readWizardDraft,
  writeWizardDraft,
  wizardDraftKey,
} from '../utils/cacheDeployDraft'

function makeCache(overrides: Partial<ModelCacheEntry> = {}): ModelCacheEntry {
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
    ...overrides,
  }
}

function makeVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
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

function renderWizard(path = '/models/cache/cache-1/deploy?step=runtime') {
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

describe('CacheDeployWizardPage Runtime layout (P1-5A / P1-8C)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.spyOn(cacheDeployApi, 'getCacheEntry').mockResolvedValue(makeCache())
    vi.spyOn(modelsApi, 'getModelVersion').mockResolvedValue(makeVersion())
    vi.spyOn(modelsApi, 'getModel').mockResolvedValue(makeModel())
    vi.spyOn(nodesApi, 'getNode').mockResolvedValue(makeNode())
    vi.spyOn(nodesApi, 'refreshNodeResources').mockResolvedValue({
      node_id: 'node-1',
      sampled_at: '2026-10-01T00:00:00Z',
      age_ms: 1000,
      host: null,
      gpus: [
        {
          gpu: {
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
          snapshot: {
            sampled_at: '2026-10-01T00:00:00Z',
            vram_total_mb: 16384,
            vram_used_mb: 4000,
            vram_free_mb: 12000,
            utilization_percent: null,
            temperature_c: null,
            power_watts: null,
          },
        },
      ],
    } as never)
  })

  function makeFit() {
    return {
      result: 'FIT' as const,
      tensor_parallel: 1,
      evaluated_at: '2026-10-01T00:00:00Z',
      resource_sampled_at: '2026-10-01T00:00:00Z',
      reasons: ['ok'],
      assumptions: [],
      warnings: [],
      suggested_gpu_device_ids: ['gpu-0'],
      estimated_required_vram_mb: 2000,
      estimated_required_vram_mb_per_gpu: 2000,
      disk_gate: 'OK',
      gpu_results: [
        {
          gpu_device_id: 'gpu-0',
          gpu_index: 0,
          gpu_uuid: 'GPU-0',
          name: 'Mock GPU',
          vram_total_mb: 16384,
          vram_free_mb: 12000,
          vram_used_mb: 4000,
          safety_margin_mb: 1024,
          estimated_required_vram_mb: 2000,
          result: 'FIT' as const,
          reasons: [],
        },
      ],
    }
  }

  afterEach(() => {
    cleanup()
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('groups Runtime fields and keeps control labels / types', async () => {
    renderWizard()

    expect(
      await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('runtime-section-basic')).toBeInTheDocument()
    expect(screen.getByTestId('runtime-section-resource')).toBeInTheDocument()
    expect(screen.getByTestId('runtime-section-advanced')).toBeInTheDocument()
    expect(screen.getByText('기본 설정')).toBeInTheDocument()
    expect(screen.getByText('리소스 설정')).toBeInTheDocument()
    expect(screen.getByText(/고급 옵션/)).toBeInTheDocument()

    const grids = document.querySelectorAll('.form-grid')
    expect(grids.length).toBe(3)
    const labelCount = Array.from(grids).reduce(
      (n, g) => n + g.querySelectorAll('label').length,
      0,
    )
    expect(labelCount).toBeGreaterThanOrEqual(10)
    expect(document.querySelectorAll('.form-grid__wide').length).toBe(3)

    expect(screen.getAllByText(/max_model_len/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/max_num_seqs/).length).toBeGreaterThan(0)
    expect(
      screen.getAllByText(/gpu_memory_utilization/).length,
    ).toBeGreaterThan(0)
    expect(screen.getAllByText(/Served model name/).length).toBeGreaterThan(0)
    expect(
      screen.getByText(/vLLM Model Runner\(Auto\/V1\)와는 다른 설정/),
    ).toBeInTheDocument()

    const parser = screen.getByLabelText('Reasoning Parser')
    expect(parser).toHaveValue('')
    expect(
      screen.queryByText(/Reasoning Parser는 Runtime 시작 옵션/),
    ).not.toBeInTheDocument()
    const modelRunner = screen.getByLabelText('vLLM Model Runner')
    expect(modelRunner).toHaveValue('')
    expect(
      screen.queryByText(/V1 Model Runner는 컨테이너 환경변수/),
    ).not.toBeInTheDocument()

    const numberInputs = document.querySelectorAll(
      '.form-grid input[type="number"]',
    )
    expect(numberInputs.length).toBeGreaterThanOrEqual(4)
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
    expect(screen.getByTestId('qwen3-compat-warning')).toBeInTheDocument()

    await userEvent.selectOptions(modelRunner, 'v1')
    expect(modelRunner).toHaveValue('v1')
    expect(
      screen.getByText(/V1 Model Runner는 컨테이너 환경변수/),
    ).toBeInTheDocument()
  })

  it('blocks Next: GPU on invalid numeric Runtime inputs', async () => {
    renderWizard()
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })

    const next = screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' })
    expect(next).toBeEnabled()

    const port = screen.getByLabelText(/Runtime port/)
    await userEvent.clear(port)
    await userEvent.type(port, '0')
    expect(next).toBeDisabled()
    expect(
      document.querySelectorAll('.form-field-error').length,
    ).toBeGreaterThan(0)
    expect(
      screen.getByText('1–65535 사이의 정수여야 합니다.'),
    ).toBeInTheDocument()

    await userEvent.clear(port)
    await userEvent.type(port, '8000')
    expect(next).toBeEnabled()

    const len = screen.getByLabelText(/max_model_len/)
    await userEvent.clear(len)
    await userEvent.type(len, '-5')
    expect(next).toBeDisabled()
    expect(
      screen.getByText(/max_model_len은\(는\) 미설정이거나 양의 정수/),
    ).toBeInTheDocument()

    await userEvent.clear(len)
    expect(next).toBeEnabled()

    const util = screen.getByLabelText(/gpu_memory_utilization/)
    await userEvent.clear(util)
    await userEvent.type(util, '1.5')
    expect(next).toBeDisabled()
    expect(
      screen.getByText('미설정이거나 0 초과 1 이하 값이어야 합니다.'),
    ).toBeInTheDocument()
  })

  it('does not overwrite last-good Draft while Runtime numerics are invalid; resumes after fix', async () => {
    const cache = makeCache()
    renderWizard()
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })

    const nameInput = screen.getByDisplayValue(/-managed$/)
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'draft-keep-valid')
    await userEvent.clear(screen.getByLabelText(/Runtime port/))
    await userEvent.type(screen.getByLabelText(/Runtime port/), '8123')

    await waitFor(() => {
      const saved = readWizardDraft(sessionStorage, cache)
      expect(saved?.runtimePort).toBe(8123)
      expect(saved?.name).toBe('draft-keep-valid')
    })
    const lastGoodRaw = sessionStorage.getItem(wizardDraftKey('cache-1'))
    expect(lastGoodRaw).toBeTruthy()

    await userEvent.clear(screen.getByLabelText(/Runtime port/))
    expect(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' })).toBeDisabled()
    // Empty/NaN port must not JSON-null the draft (would fail read after F5).
    expect(sessionStorage.getItem(wizardDraftKey('cache-1'))).toBe(lastGoodRaw)
    expect(readWizardDraft(sessionStorage, cache)?.runtimePort).toBe(8123)

    await userEvent.type(screen.getByLabelText(/Runtime port/), '0')
    expect(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' })).toBeDisabled()
    expect(sessionStorage.getItem(wizardDraftKey('cache-1'))).toBe(lastGoodRaw)
    expect(readWizardDraft(sessionStorage, cache)?.runtimePort).toBe(8123)

    const len = screen.getByLabelText(/max_model_len/)
    await userEvent.clear(len)
    await userEvent.type(len, '-1')
    expect(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' })).toBeDisabled()
    expect(sessionStorage.getItem(wizardDraftKey('cache-1'))).toBe(lastGoodRaw)

    await userEvent.clear(len)
    await userEvent.clear(screen.getByLabelText(/Runtime port/))
    await userEvent.type(screen.getByLabelText(/Runtime port/), '9002')
    await userEvent.type(screen.getByLabelText(/max_model_len/), '4096')
    expect(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' })).toBeEnabled()

    await waitFor(() => {
      const saved = readWizardDraft(sessionStorage, cache)
      expect(saved?.runtimePort).toBe(9002)
      expect(saved?.runtime.max_model_len).toBe(4096)
      expect(saved?.name).toBe('draft-keep-valid')
    })

    cleanup()
    renderWizard('/models/cache/cache-1/deploy?step=runtime')
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })
    expect(screen.getByDisplayValue('draft-keep-valid')).toBeInTheDocument()
    expect(screen.getByDisplayValue('9002')).toBeInTheDocument()
    expect(screen.getByDisplayValue('4096')).toBeInTheDocument()
  })

  it('keeps unset optional fields empty (no auto dtype/quant)', async () => {
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({ dtype: 'float16', quantization: 'awq' }),
    )
    renderWizard()
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })

    const dtype = screen.getByPlaceholderText('float16')
    const quant = screen.getByPlaceholderText('awq')
    expect(dtype).toHaveValue('')
    expect(quant).toHaveValue('')
    expect(screen.getByText(/ModelVersion 참고: float16/)).toBeInTheDocument()
    expect(screen.getByText(/ModelVersion 참고: awq/)).toBeInTheDocument()
  })

  it('does not warn qwen3 compatibility when repository suggests Qwen3', async () => {
    vi.mocked(cacheDeployApi.getCacheEntry).mockResolvedValue(
      makeCache({ repository_id: 'Qwen/Qwen3-8B' }),
    )
    vi.mocked(modelsApi.getModelVersion).mockResolvedValue(
      makeVersion({
        source_repository: 'Qwen/Qwen3-8B',
        served_model_name: 'Qwen/Qwen3-8B',
      }),
    )
    renderWizard()
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })
    await userEvent.selectOptions(
      screen.getByLabelText('Reasoning Parser'),
      'qwen3',
    )
    expect(
      screen.getByText(/Reasoning Parser는 Runtime 시작 옵션/),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId('qwen3-compat-warning'),
    ).not.toBeInTheDocument()
  })

  it('invalidates Fresh Fit when runner / parser / model runner change', async () => {
    vi.spyOn(cacheDeployApi, 'previewCacheDeployFit').mockResolvedValue(
      makeFit(),
    )

    renderWizard('/models/cache/cache-1/deploy?step=gpu')
    await screen.findByRole('heading', { level: 2, name: /3\. GPU 선택/ })
    // First GPU is pre-selected on load.
    await userEvent.click(
      screen.getByRole('button', { name: '현재 설정으로 GPU 자원 검사' }),
    )
    expect(
      await screen.findAllByTitle(/자원 적합\(FIT\)/),
    ).not.toHaveLength(0)
    expect(cacheDeployApi.previewCacheDeployFit).toHaveBeenCalledTimes(1)

    await userEvent.click(screen.getByRole('button', { name: '이전' }))
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })
    await userEvent.selectOptions(
      screen.getByLabelText('Reasoning Parser'),
      'qwen3',
    )
    await userEvent.click(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' }))
    await screen.findByRole('heading', { level: 2, name: /3\. GPU 선택/ })
    expect(
      screen.getByText(/설정으로 GPU 자원 검사」를 실행해야/),
    ).toBeInTheDocument()
    expect(screen.queryByTitle('FIT')).not.toBeInTheDocument()
    expect(screen.queryByTestId('wizard-fit-result')).not.toBeInTheDocument()
  })

  it('restores draft Runtime values and shows Review applied/unset partition', async () => {
    writeWizardDraft(sessionStorage, {
      schema: 1,
      cacheId: 'cache-1',
      nodeId: 'node-1',
      modelVersionId: 'ver-1',
      revision: 'abc123',
      savedAt: Date.now(),
      step: 'runtime',
      name: 'restored-dep',
      containerName: 'restored-ctr',
      servedModelName: 'restored-served',
      runtimePort: 9001,
      runtime: {
        runner: 'pooling',
        max_model_len: 2048,
        probe_type: 'CHAT',
        health_path: '/health',
        tensor_parallel_size: 1,
        reasoning_parser: 'qwen3',
        vllm_model_runner: 'v1',
        enable_auto_tool_choice: true,
        tool_call_parser: 'hermes',
      },
      selectedGpuIds: ['gpu-0'],
      expectedVramMb: '',
    })
    expect(sessionStorage.getItem(wizardDraftKey('cache-1'))).toBeTruthy()

    vi.spyOn(cacheDeployApi, 'previewCacheDeployFit').mockResolvedValue(
      makeFit(),
    )

    renderWizard('/models/cache/cache-1/deploy?step=runtime')
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })
    expect(screen.getByDisplayValue('restored-dep')).toBeInTheDocument()
    expect(screen.getByDisplayValue('9001')).toBeInTheDocument()
    expect(screen.getByDisplayValue('2048')).toBeInTheDocument()
    expect(screen.getByLabelText('Reasoning Parser')).toHaveValue('qwen3')
    expect(screen.getByLabelText('vLLM Model Runner')).toHaveValue('v1')
    expect(screen.getByLabelText('Auto Tool Choice')).toHaveValue('true')
    expect(screen.getByLabelText('Tool Call Parser')).toHaveValue('hermes')

    await userEvent.click(screen.getByRole('button', { name: '다음: GPU 선택 및 자원 검사' }))
    await screen.findByRole('heading', { level: 2, name: /3\. GPU 선택/ })
    await userEvent.click(
      screen.getByRole('button', { name: '현재 설정으로 GPU 자원 검사' }),
    )
    expect(
      await screen.findAllByTitle(/자원 적합\(FIT\)/),
    ).not.toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: '다음: 설정 검토' }))

    const summary = await screen.findByTestId('runtime-review-summary')
    expect(within(summary).getByText('배포 설정 요약')).toBeInTheDocument()
    expect(within(summary).getByText('Fresh Fit 결과')).toBeInTheDocument()
    expect(within(summary).getByText('선택한 GPU')).toBeInTheDocument()
    const applied = within(summary).getByTestId('runtime-review-applied')
    expect(applied).toHaveTextContent('max_model_len')
    expect(applied).toHaveTextContent('2048')
    expect(applied).toHaveTextContent('reasoning_parser')
    expect(applied).toHaveTextContent('qwen3')
    expect(applied).toHaveTextContent('vllm_model_runner')
    expect(applied).toHaveTextContent('v1')
    expect(applied).toHaveTextContent('enable_auto_tool_choice')
    expect(applied).toHaveTextContent('tool_call_parser')
    expect(applied).toHaveTextContent('hermes')
    const unset = within(summary).getByTestId('runtime-review-unset')
    expect(unset).toHaveTextContent('dtype')
    expect(unset).toHaveTextContent('quantization')
    expect(screen.getByTestId('review-qwen3-compat-warning')).toBeInTheDocument()
    expect(summary).toHaveTextContent(/thinking_token_budget/)

    clearWizardDraft(sessionStorage, 'cache-1')
  })

  it('shows Korean GPU selection summary and Fit cards with 정보 없음 for missing metrics', async () => {
    vi.spyOn(cacheDeployApi, 'previewCacheDeployFit').mockResolvedValue({
      ...makeFit(),
      result: 'TIGHT',
      gpu_results: [
        {
          gpu_device_id: 'gpu-0',
          gpu_index: 0,
          gpu_uuid: 'GPU-0',
          name: 'Mock GPU',
          vram_total_mb: 16384,
          vram_free_mb: null,
          vram_used_mb: null,
          safety_margin_mb: 1024,
          estimated_required_vram_mb: null,
          result: 'TIGHT',
          reasons: ['thin headroom'],
        },
      ],
    })

    renderWizard('/models/cache/cache-1/deploy?step=gpu')
    await screen.findByRole('heading', { level: 2, name: /3\. GPU 선택/ })
    expect(screen.getByTestId('wizard-gpu-selection-summary')).toHaveTextContent(
      /현재 선택 GPU/,
    )
    expect(
      screen.getAllByText(/선택한 GPU 수에 따라 Tensor Parallel/).length,
    ).toBeGreaterThan(0)
    expect(
      screen.getByText(/안정적인 동시 실행을 보장하지 않습니다/),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '현재 설정으로 GPU 자원 검사' }),
    ).toBeInTheDocument()

    await userEvent.click(
      screen.getByRole('button', { name: '현재 설정으로 GPU 자원 검사' }),
    )
    const fit = await screen.findByTestId('wizard-fit-result')
    expect(within(fit).getByText(/종합 판정/)).toBeInTheDocument()
    expect(within(fit).getAllByText(/여유 부족 가능/).length).toBeGreaterThan(0)
    expect(within(fit).getByText('현재 여유 메모리')).toBeInTheDocument()
    expect(within(fit).getAllByText('정보 없음').length).toBeGreaterThan(1)
    expect(within(fit).queryByText('0 MiB')).not.toBeInTheDocument()
    expect(within(fit).getByText('thin headroom')).toBeInTheDocument()
  })

  it('hides internal step key from the main chrome and keeps URL step sync', async () => {
    renderWizard('/models/cache/cache-1/deploy?step=runtime')
    await screen.findByRole('heading', { level: 2, name: '2. 모델 실행 설정' })
    expect(screen.queryByText(/내부 단계 키:/)).not.toBeInTheDocument()
    expect(screen.getByRole('list', { name: '배포 설정 단계' })).toHaveTextContent(
      /현재/,
    )
    expect(screen.getByText(/이 단계에서 하는 일/)).toBeInTheDocument()
    await userEvent.click(screen.getByText('용어 및 기술 안내'))
    expect(screen.getByText(/내부 키입니다\(현재: runtime\)/)).toBeInTheDocument()
  })
})
