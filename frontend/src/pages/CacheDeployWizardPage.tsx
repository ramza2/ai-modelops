import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  createDeploymentFromCache,
  getCacheDeployPublishStatus,
  getCacheEntry,
  previewCacheDeployFit,
  publishCacheDeployment,
  type CacheDeployRuntimeConfig,
  type FreshFitResult,
  type PublishResult,
} from '../api/cacheDeploy'
import { getDeployment, startDeployment } from '../api/deployments'
import { listEndpoints } from '../api/endpoints'
import { getModel, getModelVersion } from '../api/models'
import { getNode, getNodeResources } from '../api/nodes'
import { getOperation } from '../api/operations'
import {
  clearPublishDraft,
  clearWizardDraft,
  readPublishDraft,
  readWizardDraft,
  resumableStep,
  writePublishDraft,
  writeWizardDraft,
  type DraftStorage,
} from '../utils/cacheDeployDraft'
import type {
  Deployment,
  Endpoint,
  ModelCacheEntry,
  ModelSummary,
  ModelVersion,
  NodeDetail,
  NodeResourcesLatest,
  OperationDetail,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'

type Step =
  | 'cache'
  | 'runtime'
  | 'gpu'
  | 'review'
  | 'start'
  | 'publish'
  | 'done'

const STEPS: Step[] = [
  'cache',
  'runtime',
  'gpu',
  'review',
  'start',
  'publish',
  'done',
]

/** Known-profile values for an existing BGE-M3 cache — not generic EMBEDDING defaults. */
const BGE_M3_KNOWN_PROFILE: CacheDeployRuntimeConfig = {
  runner: 'pooling',
  max_model_len: 8192,
  max_num_seqs: 4,
  gpu_memory_utilization: 0.15,
}

/** Exported for unit tests — generic EMBEDDING must not ship BGE-only knobs. */
export function defaultsForModelType(
  modelType: string | null | undefined,
  repositoryId?: string | null,
): {
  runtime: CacheDeployRuntimeConfig
  servedHint: string
  knownProfileLabel: string | null
} {
  const t = (modelType || '').toUpperCase()
  const repo = (repositoryId || '').trim()
  if (t === 'EMBEDDING') {
    const isBgeM3 = repo === 'BAAI/bge-m3'
    return {
      runtime: {
        runner: 'pooling',
        probe_type: 'EMBEDDING',
        tensor_parallel_size: 1,
        health_path: '/health',
        ...(isBgeM3 ? BGE_M3_KNOWN_PROFILE : {}),
      },
      servedHint: '',
      knownProfileLabel: isBgeM3
        ? 'BAAI/bge-m3 known profile (not a generic EMBEDDING default)'
        : null,
    }
  }
  return {
    runtime: {
      probe_type: 'CHAT',
      tensor_parallel_size: 1,
      health_path: '/health',
    },
    servedHint: '',
    knownProfileLabel: null,
  }
}

function formatBytes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let v = value / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`
}

export function CacheDeployWizardPage() {
  const { cacheId = '' } = useParams<{ cacheId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()

  const urlStep = searchParams.get('step')
  const stepParam = (urlStep as Step | null) || 'cache'
  const deploymentIdParam = searchParams.get('deployment_id') || ''
  const operationIdParam = searchParams.get('operation_id') || ''

  const [step, setStep] = useState<Step>(
    STEPS.includes(stepParam) ? stepParam : 'cache',
  )
  const [cache, setCache] = useState<ModelCacheEntry | null>(null)
  const [model, setModel] = useState<ModelSummary | null>(null)
  const [version, setVersion] = useState<ModelVersion | null>(null)
  const [node, setNode] = useState<NodeDetail | null>(null)
  const [resources, setResources] = useState<NodeResourcesLatest | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [name, setName] = useState('')
  const [containerName, setContainerName] = useState('')
  const [servedModelName, setServedModelName] = useState('')
  const [runtimePort, setRuntimePort] = useState(8000)
  const [runtime, setRuntime] = useState<CacheDeployRuntimeConfig>({})
  const [knownProfileLabel, setKnownProfileLabel] = useState<string | null>(null)
  const [selectedGpuIds, setSelectedGpuIds] = useState<string[]>([])
  const [expectedVramMb, setExpectedVramMb] = useState<string>('')
  const [ackUnknown, setAckUnknown] = useState(false)
  const [fit, setFit] = useState<FreshFitResult | null>(null)
  const [fitLoading, setFitLoading] = useState(false)
  // A Fit response is a transient resource decision; never persist/restore it.
  const [fitFingerprint, setFitFingerprint] = useState('')

  const [deployment, setDeployment] = useState<Deployment | null>(null)
  const [operation, setOperation] = useState<OperationDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [publishMode, setPublishMode] = useState<'new' | 'existing'>('new')
  const [alias, setAlias] = useState('')
  const [endpointId, setEndpointId] = useState('')
  const [rewriteModelName, setRewriteModelName] = useState('')
  const [publishResult, setPublishResult] = useState<PublishResult | null>(null)
  const [publishStatusLoading, setPublishStatusLoading] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const restoredCacheRef = useRef<string | null>(null)
  const restoredPublishRef = useRef<string | null>(null)
  const [restoredDraft, setRestoredDraft] = useState(false)
  const [reviewNeedsFit, setReviewNeedsFit] = useState(false)
  const storage = (() : DraftStorage | null => {
    try { return window.sessionStorage } catch { return null }
  })()

  const syncStep = useCallback(
    (next: Step, extra?: { deploymentId?: string; operationId?: string }) => {
      setStep(next)
      const params = new URLSearchParams()
      params.set('step', next)
      const depId = extra?.deploymentId ?? deploymentIdParam
      const opId = extra?.operationId ?? operationIdParam
      if (depId) params.set('deployment_id', depId)
      if (opId) params.set('operation_id', opId)
      setSearchParams(params, { replace: true })
    },
    [deploymentIdParam, operationIdParam, setSearchParams],
  )

  useEffect(() => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const entry = await getCacheEntry(cacheId, controller.signal)
        if (controller.signal.aborted) return
        if (!entry) {
          setError('Cache entry not found.')
          setLoading(false)
          return
        }
        if (entry.status !== 'READY') {
          setError(`Cache status is ${entry.status}; Deploy requires READY.`)
        }
        setCache(entry)
        setLastUpdated(new Date())
        const repoLeaf = (entry.repository_id || 'model').split('/').pop() || 'model'
        const firstLoad = restoredCacheRef.current !== cacheId
        const saved = firstLoad && !deploymentIdParam
          ? readWizardDraft(storage, entry)
          : null
        if (firstLoad) {
          setName(saved?.name ?? `${repoLeaf}-managed`)
          setContainerName(saved?.containerName ??
            `modelops-${repoLeaf}`.replace(/[^a-zA-Z0-9._-]/g, '-'))
          setRuntimePort(saved?.runtimePort ?? 8000)
          setExpectedVramMb(saved?.expectedVramMb ?? '')
          setAlias(repoLeaf)
          setAckUnknown(false)
          setFit(null)
          setFitFingerprint('')
          setRestoredDraft(Boolean(saved))
          setReviewNeedsFit(urlStep === 'review' || saved?.step === 'review')
        }

        if (entry.model_version_id) {
          const ver = await getModelVersion(entry.model_version_id, controller.signal)
          if (controller.signal.aborted) return
          setVersion(ver)
          if (firstLoad) {
            setServedModelName(saved?.servedModelName ?? ver.served_model_name)
            setRewriteModelName(ver.served_model_name)
          }
          const mod = await getModel(ver.model_id, controller.signal)
          if (controller.signal.aborted) return
          setModel(mod)
          const d = defaultsForModelType(mod.model_type, entry.repository_id)
          setKnownProfileLabel(d.knownProfileLabel)
          if (firstLoad) setRuntime({ ...d.runtime, ...(saved?.runtime ?? {}) })
        }
        if (entry.node_id) {
          const [n, res] = await Promise.all([
            getNode(entry.node_id, controller.signal),
            getNodeResources(entry.node_id, controller.signal).catch(() => null),
          ])
          if (controller.signal.aborted) return
          setNode(n)
          setResources(res)
          if (firstLoad) {
            const available = new Set(n.gpus.map((g) => g.id))
            setSelectedGpuIds(
              saved
                ? saved.selectedGpuIds.filter((id) => available.has(id))
                : n.gpus[0] ? [n.gpus[0].id] : [],
            )
          }
        }

        if (deploymentIdParam) {
          if (restoredPublishRef.current !== deploymentIdParam) {
            const publishDraft = readPublishDraft(storage, deploymentIdParam)
            if (publishDraft) {
              setPublishMode(publishDraft.mode)
              setAlias(publishDraft.alias)
              setEndpointId(publishDraft.endpointId)
              setRewriteModelName(publishDraft.rewriteModelName)
              setRestoredDraft(true)
            }
            restoredPublishRef.current = deploymentIdParam
          }
          const dep = await getDeployment(deploymentIdParam, controller.signal)
          if (controller.signal.aborted) return
          setDeployment(dep)

          // Persist Done/Publish across browser refresh from DB ACTIVE route.
          if (stepParam === 'done' || stepParam === 'publish') {
            setPublishStatusLoading(true)
            try {
              const status = await getCacheDeployPublishStatus(
                deploymentIdParam,
                { verifyGateway: stepParam === 'done' },
                controller.signal,
              )
              if (controller.signal.aborted) return
              if (status.published) {
                clearPublishDraft(storage, deploymentIdParam)
                setPublishResult({
                  endpoint: status.endpoint,
                  route: status.route,
                  routing_version: status.routing_version,
                  gateway_verification: status.gateway_verification,
                  note: status.note,
                  reused: true,
                })
              } else {
                setPublishResult(null)
              }
            } catch (pubErr) {
              if (controller.signal.aborted) return
              if (stepParam === 'done') {
                setActionError(
                  pubErr instanceof ApiError
                    ? pubErr.message
                    : 'Failed to load publish status.',
                )
              }
            } finally {
              if (!controller.signal.aborted) setPublishStatusLoading(false)
            }
          }
        }
        if (operationIdParam) {
          const op = await getOperation(operationIdParam, controller.signal)
          if (controller.signal.aborted) return
          setOperation(op)
        }
        if (firstLoad) {
          restoredCacheRef.current = cacheId
          if (!deploymentIdParam) {
            const resume = saved
              ? resumableStep(saved.step, urlStep)
              : urlStep === 'review' ? 'gpu' : null
            if (resume && resume !== stepParam) syncStep(resume)
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return
        setError(err instanceof ApiError ? err.message : 'Failed to load cache.')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    })()
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheId, deploymentIdParam, operationIdParam, stepParam])

  // Only operator input is saved. Fit/Operation/Endpoint state is server-owned.
  // Rehydration finishes before the first save so defaults never erase a draft.
  useEffect(() => {
    if (!cache || cache.id !== cacheId || cache.status !== 'READY' ||
        restoredCacheRef.current !== cacheId || loading || deploymentIdParam ||
        !['cache', 'runtime', 'gpu', 'review'].includes(step)) return
    writeWizardDraft(storage, {
      schema: 1, cacheId, nodeId: cache.node_id,
      modelVersionId: cache.model_version_id, revision: cache.resolved_revision,
      savedAt: Date.now(), step: step as 'cache' | 'runtime' | 'gpu' | 'review',
      name, containerName, servedModelName, runtimePort, runtime,
      selectedGpuIds, expectedVramMb,
    })
  }, [cache, cacheId, loading, deploymentIdParam, step, name, containerName,
    servedModelName, runtimePort, runtime, selectedGpuIds, expectedVramMb])

  useEffect(() => {
    if (!deploymentIdParam || step !== 'publish' || loading ||
        publishStatusLoading || publishResult ||
        restoredPublishRef.current !== deploymentIdParam) return
    writePublishDraft(storage, {
      schema: 1, deploymentId: deploymentIdParam, savedAt: Date.now(),
      mode: publishMode, alias, endpointId, rewriteModelName,
    })
  }, [deploymentIdParam, step, loading, publishStatusLoading, publishResult,
    publishMode, alias, endpointId, rewriteModelName])

  const uniqueGpuIds = Array.from(new Set(selectedGpuIds))
  const fitInput = JSON.stringify([uniqueGpuIds, expectedVramMb, runtime.max_model_len,
    runtime.max_num_seqs, runtime.gpu_memory_utilization, runtime.dtype,
    runtime.quantization])
  const fitInputRef = useRef(fitInput)
  fitInputRef.current = fitInput
  const validFit = fitFingerprint === fitInput ? fit : null

  const refreshFit = async () => {
    if (!cacheId || uniqueGpuIds.length === 0) return
    const inputAtRequest = fitInput
    setFitLoading(true)
    setFit(null)
    setFitFingerprint('')
    setAckUnknown(false)
    setActionError(null)
    try {
      const result = await previewCacheDeployFit(cacheId, {
        gpuDeviceIds: uniqueGpuIds,
        tensorParallel: uniqueGpuIds.length,
        expectedVramMb: expectedVramMb ? Number(expectedVramMb) : null,
        dtype: runtime.dtype,
        quantization: runtime.quantization,
      })
      if (fitInputRef.current !== inputAtRequest) return
      setFit(result)
      setFitFingerprint(inputAtRequest)
      setReviewNeedsFit(false)
      setRuntime((r) => ({ ...r, tensor_parallel_size: uniqueGpuIds.length }))
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Fresh fit preview failed.',
      )
    } finally {
      setFitLoading(false)
    }
  }

  const onCreate = async () => {
    if (!cache || busy || !validFit || fitLoading) return
    setBusy(true)
    setActionError(null)
    try {
      const created = await createDeploymentFromCache(cache.id, {
        name,
        containerName,
        gpuDeviceIds: uniqueGpuIds,
        runtimePort,
        servedModelName,
        expectedVramMb: expectedVramMb ? Number(expectedVramMb) : null,
        acknowledgeUnknownFit: ackUnknown,
        runtimeConfig: {
          ...runtime,
          tensor_parallel_size: uniqueGpuIds.length,
        },
      })
      setDeployment(created)
      clearWizardDraft(storage, cacheId)
      syncStep('start', { deploymentId: created.id })
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Deployment create failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const onStart = async () => {
    if (!deployment || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const op = await startDeployment(deployment.id)
      syncStep('start', {
        deploymentId: deployment.id,
        operationId: op.id,
      })
      const detail = await getOperation(op.id)
      setOperation(detail)
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Start enqueue failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!operationIdParam) return
    if (operation?.status === 'SUCCEEDED' || operation?.status === 'FAILED') return
    const timer = window.setInterval(() => {
      void getOperation(operationIdParam)
        .then(async (op) => {
          setOperation(op)
          if (deploymentIdParam) {
            const dep = await getDeployment(deploymentIdParam)
            setDeployment(dep)
          }
          if (
            op.status === 'SUCCEEDED' &&
            deployment?.runtime_status === 'RUNNING' &&
            deployment?.health_status === 'HEALTHY'
          ) {
            // keep on start until user advances; refresh dep below
          }
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [
    operationIdParam,
    operation?.status,
    deploymentIdParam,
    deployment?.runtime_status,
    deployment?.health_status,
  ])

  const healthy =
    deployment?.desired_state === 'RUNNING' &&
    deployment?.runtime_status === 'RUNNING' &&
    deployment?.health_status === 'HEALTHY'

  const loadEndpoints = async () => {
    const page = await listEndpoints({ page: 1, pageSize: 100 })
    const apiType =
      (model?.model_type || '').toUpperCase() === 'EMBEDDING' ? 'EMBEDDING' : 'CHAT'
    setEndpoints(page.items.filter((e) => e.api_type === apiType))
  }

  // After F5, an existing-alias selection must have its options reloaded.
  useEffect(() => {
    if (step !== 'publish' || publishMode !== 'existing' || !model) return
    void loadEndpoints().catch(() => setActionError('Endpoint 목록을 불러오지 못했습니다.'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, publishMode, model?.model_type])

  const onPublish = async () => {
    if (!deployment || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const result = await publishCacheDeployment(deployment.id, {
        alias: publishMode === 'new' ? alias : null,
        endpointId: publishMode === 'existing' ? endpointId : null,
        rewriteModelName: rewriteModelName || servedModelName,
        verifyGateway: true,
      })
      setPublishResult(result)
      clearPublishDraft(storage, deployment.id)
      syncStep('done', { deploymentId: deployment.id })
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ACTIVE_ROUTE_EXISTS') {
        setActionError(
          '이 Alias는 이미 ACTIVE route가 있습니다. HOT/COLD Switch 워크플로를 사용하세요.',
        )
      } else {
        setActionError(
          err instanceof ApiError ? err.message : 'Publish failed.',
        )
      }
    } finally {
      setBusy(false)
    }
  }

  const onRefresh = () => {
    setRefreshing(true)
    navigate(0)
  }

  if (loading) {
    return (
      <AppShell
        title="Deploy from Cache"
        description="M7-C wizard"
        onRefresh={onRefresh}
        refreshing={refreshing}
        lastUpdated={lastUpdated}
      >
        <LoadingBlock label="캐시/메타데이터를 불러오는 중…" />
      </AppShell>
    )
  }

  return (
    <AppShell
      title="Deploy from Cache"
      description="Downloaded ≠ Deployed ≠ Published. Wizard composes existing Deployment / Operation / Endpoint APIs."
      onRefresh={onRefresh}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models/cache">
          ← Model Cache
        </Link>
        <span className="secondary-text">
          Step: <strong>{step}</strong>
        </span>
      </div>

      {error ? <SectionError title="로드 오류" message={error} /> : null}
      {actionError ? (
        <SectionError title="작업 오류" message={actionError} />
      ) : null}
      {restoredDraft ? (
        <p className="secondary-text" role="status">
          이전 입력 설정을 이 탭에서 복원했습니다. Fit 판정과 배포 상태는 저장하지 않습니다.
        </p>
      ) : null}
      {reviewNeedsFit && step === 'gpu' ? (
        <p role="alert">Review 이전 설정을 복원했습니다. 최신 GPU 상태로 Fresh Fit을 다시 실행해 주세요.</p>
      ) : null}

      {step === 'cache' && cache ? (
        <section className="panel">
          <h2>1. Cache</h2>
          <dl className="detail-list">
            <div>
              <dt>Repository</dt>
              <dd className="mono">{cache.repository_id || '—'}</dd>
            </div>
            <div>
              <dt>Immutable SHA</dt>
              <dd className="mono">{cache.resolved_revision || '—'}</dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{formatBytes(cache.size_bytes)}</dd>
            </div>
            <div>
              <dt>Node</dt>
              <dd>
                {cache.node_name || cache.node_id}{' '}
                <StatusBadge status={cache.status} />
              </dd>
            </div>
            <div>
              <dt>Path</dt>
              <dd className="mono">{cache.local_path || '—'}</dd>
            </div>
            <div>
              <dt>Model type</dt>
              <dd>{model?.model_type || '—'}</dd>
            </div>
          </dl>
          <button
            type="button"
            className="btn"
            disabled={cache.status !== 'READY'}
            onClick={() => syncStep('runtime')}
          >
            Next: Runtime
          </button>
        </section>
      ) : null}

      {step === 'runtime' ? (
        <section className="panel">
          <h2>2. Runtime</h2>
          {knownProfileLabel ? (
            <p className="muted" data-testid="known-profile-label">
              Prefill: {knownProfileLabel}
            </p>
          ) : null}
          <div className="form-grid">
            <label>
              Deployment name
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label>
              Container name
              <input
                value={containerName}
                onChange={(e) => setContainerName(e.target.value)}
              />
            </label>
            <label>
              Served model name
              <input
                value={servedModelName}
                onChange={(e) => {
                  setServedModelName(e.target.value)
                  setRewriteModelName(e.target.value)
                }}
              />
            </label>
            <label>
              Runtime port
              <input
                type="number"
                value={runtimePort}
                onChange={(e) => setRuntimePort(Number(e.target.value))}
              />
            </label>
            <label>
              max_model_len
              <input
                type="number"
                value={runtime.max_model_len ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    max_model_len: e.target.value
                      ? Number(e.target.value)
                      : null,
                  }))
                }
              />
            </label>
            <label>
              max_num_seqs
              <input
                type="number"
                value={runtime.max_num_seqs ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    max_num_seqs: e.target.value
                      ? Number(e.target.value)
                      : null,
                  }))
                }
              />
            </label>
            <label>
              gpu_memory_utilization
              <input
                type="number"
                step="0.01"
                value={runtime.gpu_memory_utilization ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    gpu_memory_utilization: e.target.value
                      ? Number(e.target.value)
                      : null,
                  }))
                }
              />
            </label>
            <label>
              runner
              <select
                value={runtime.runner ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    runner: e.target.value || null,
                  }))
                }
              >
                <option value="">(omit / generate)</option>
                <option value="generate">generate</option>
                <option value="pooling">pooling</option>
              </select>
            </label>
            <label>
              dtype (optional)
              <input
                value={runtime.dtype ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({ ...r, dtype: e.target.value || null }))
                }
                placeholder={version?.dtype || 'do not guess'}
              />
            </label>
            <label>
              quantization (optional)
              <input
                value={runtime.quantization ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    quantization: e.target.value || null,
                  }))
                }
                placeholder={version?.quantization || 'do not guess'}
              />
            </label>
          </div>
          <p className="secondary-text">
            Catalog dtype/quant hints are suggestions only. Official vLLM image
            ENTRYPOINT contract remains <code>vllm serve …</code>.
          </p>
          <div className="toolbar__actions">
            <button type="button" className="btn btn--ghost" onClick={() => syncStep('cache')}>
              Back
            </button>
            <button type="button" className="btn" onClick={() => syncStep('gpu')}>
              Next: GPU
            </button>
          </div>
        </section>
      ) : null}

      {step === 'gpu' ? (
        <section className="panel">
          <h2>3. GPU (fresh fit)</h2>
          <p className="secondary-text">
            Table values are persisted Node snapshots
            {resources?.is_live === false ? ' (not live)' : ''}
            {typeof resources?.age_ms === 'number'
              ? ` · age ${Math.round(resources.age_ms / 1000)}s`
              : ''}
            . Fresh Fit re-reads Node Agent live free VRAM by GPU UUID. Disk is
            not the main gate after cache READY. VRAM is never pooled across GPUs.
          </p>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Select</th>
                  <th>GPU</th>
                  <th>Free / Total</th>
                </tr>
              </thead>
              <tbody>
                {(node?.gpus || []).map((g) => {
                  const snap = resources?.gpus.find((x) => x.gpu.id === g.id)
                    ?.snapshot
                  const live = validFit?.gpu_results.find((x) => x.gpu_device_id === g.id)
                  const checked = selectedGpuIds.includes(g.id)
                  const freeMb = live?.vram_free_mb ?? snap?.vram_free_mb
                  return (
                    <tr key={g.id}>
                      <td>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            setSelectedGpuIds((prev) =>
                              e.target.checked
                                ? [...prev, g.id]
                                : prev.filter((id) => id !== g.id),
                            )
                          }}
                        />
                      </td>
                      <td>
                        #{g.device_index} {g.model_name}
                        <div className="secondary-text mono">{g.gpu_uuid}</div>
                      </td>
                      <td>
                        {freeMb ?? '—'} / {live?.vram_total_mb ?? g.vram_total_mb} MiB
                        <div className="secondary-text">
                          {live
                            ? `Fresh Fit · ${validFit?.resource_sampled_at || validFit?.evaluated_at || 'sampled'}`
                            : snap?.sampled_at
                              ? `Snapshot · ${snap.sampled_at}`
                              : 'No sample'}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <label>
            expected_vram_mb (optional override)
            <input
              value={expectedVramMb}
              onChange={(e) => setExpectedVramMb(e.target.value)}
              placeholder="leave empty to estimate from cache size"
            />
          </label>
          <div className="toolbar__actions" style={{ marginTop: '0.75rem' }}>
            <button
              type="button"
              className="btn"
              disabled={fitLoading || selectedGpuIds.length === 0}
              onClick={() => void refreshFit()}
            >
              {fitLoading ? 'Fitting…' : 'Run fresh fit'}
            </button>
          </div>
          {validFit ? (
            <div style={{ marginTop: '0.75rem' }}>
              <StatusBadge status={validFit.result} />{' '}
              <span className="secondary-text">
                Node Agent sample:{' '}
                {validFit.resource_sampled_at || validFit.evaluated_at || 'time unknown'}
              </span>
              <ul>
                {validFit.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              {validFit.gpu_results.map((g) => (
                <p key={g.gpu_device_id} className="secondary-text">
                  GPU #{g.gpu_index ?? '?'} {g.gpu_uuid ? `(${g.gpu_uuid})` : ''}:{' '}
                  {g.vram_free_mb ?? '—'} free / {g.vram_used_mb ?? '—'} used /{' '}
                  {g.vram_total_mb} total MiB — {g.result}
                  {g.reasons.length ? ` (${g.reasons.join('; ')})` : ''}
                </p>
              ))}
              {validFit.assumptions.length > 0 ? (
                <p className="secondary-text">
                  Assumptions: {validFit.assumptions.join(' / ')}
                </p>
              ) : null}
              {validFit.result === 'UNKNOWN' ? (
                <label>
                  <input
                    type="checkbox"
                    checked={ackUnknown}
                    onChange={(e) => setAckUnknown(e.target.checked)}
                  />{' '}
                  Acknowledge UNKNOWN fit and proceed
                </label>
              ) : null}
              {validFit.result === 'INSUFFICIENT' ? (
                <p className="secondary-text">
                  INSUFFICIENT blocks Deployment creation.
                </p>
              ) : null}
            </div>
          ) : (
            <p className="secondary-text">
              진행하려면 선택한 GPU 및 Runtime 설정으로 Fresh Fit을 실행해야 합니다.
            </p>
          )}
          <div className="toolbar__actions">
            <button type="button" className="btn btn--ghost" onClick={() => syncStep('runtime')}>
              Back
            </button>
            <button
              type="button"
              className="btn"
              disabled={
                selectedGpuIds.length === 0 ||
                fitLoading || !validFit ||
                validFit?.result === 'INSUFFICIENT' ||
                (validFit?.result === 'UNKNOWN' && !ackUnknown)
              }
              onClick={() => syncStep('review')}
            >
              Next: Review
            </button>
          </div>
        </section>
      ) : null}

      {step === 'review' ? (
        <section className="panel">
          <h2>4–5. Review & Create Deployment</h2>
          <pre className="mono" style={{ whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(
              {
                name,
                containerName,
                servedModelName,
                runtimePort,
                selectedGpuIds,
                runtime: {
                  ...runtime,
                  tensor_parallel_size: selectedGpuIds.length,
                },
                model_path: cache?.local_path,
                network_names: ['modelops-model'],
                fit: validFit?.result,
              },
              null,
              2,
            )}
          </pre>
          <p className="secondary-text">
            Create is metadata only — Start is a separate step.
          </p>
          <div className="toolbar__actions">
            <button type="button" className="btn btn--ghost" onClick={() => syncStep('gpu')}>
              Back
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || fitLoading || !validFit ||
                validFit.result === 'INSUFFICIENT' ||
                (validFit.result === 'UNKNOWN' && !ackUnknown)}
              onClick={() => void onCreate()}
            >
              {busy ? 'Creating…' : 'Create Deployment'}
            </button>
          </div>
        </section>
      ) : null}

      {step === 'start' && deployment ? (
        <section className="panel">
          <h2>6. Start / Operation</h2>
          <p>
            Deployment{' '}
            <Link to={`/deployments/${deployment.id}`}>{deployment.name}</Link>{' '}
            <StatusBadge status={deployment.runtime_status} />{' '}
            <StatusBadge status={deployment.health_status} />
          </p>
          {operation ? (
            <div>
              <p>
                Operation{' '}
                <Link to={`/operations/${operation.id}`}>{operation.id}</Link>{' '}
                <StatusBadge status={operation.status} />
              </p>
              <ul>
                {(operation.steps || []).map((s) => (
                  <li key={s.id}>
                    {s.step_code}: <StatusBadge status={s.status} />{' '}
                    {s.error?.message || ''}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => void onStart()}
            >
              {busy ? 'Starting…' : 'Start Deployment'}
            </button>
          )}
          <div className="toolbar__actions" style={{ marginTop: '0.75rem' }}>
            <button
              type="button"
              className="btn"
              disabled={!healthy}
              title={
                healthy
                  ? undefined
                  : 'Publish disabled until RUNNING + HEALTHY'
              }
              onClick={() => {
                void loadEndpoints()
                syncStep('publish', { deploymentId: deployment.id })
              }}
            >
              Next: Publish
            </button>
          </div>
        </section>
      ) : null}

      {step === 'publish' && deployment ? (
        <section className="panel">
          <h2>7. Publish (initial route)</h2>
          {publishStatusLoading ? (
            <LoadingBlock label="Checking published state…" />
          ) : null}
          {publishResult ? (
            <>
              <p>
                Already published to alias{' '}
                <strong>{publishResult.endpoint.alias}</strong>. Refresh-safe
                state restored from ACTIVE route.
              </p>
              <button
                type="button"
                className="btn"
                onClick={() =>
                  syncStep('done', { deploymentId: deployment.id })
                }
              >
                Continue to Done
              </button>
            </>
          ) : (
            <>
              {!healthy ? (
                <SectionError
                  title="Not healthy"
                  message="Publish requires desired RUNNING + runtime RUNNING + HEALTHY."
                />
              ) : null}
              <label>
                <input
                  type="radio"
                  checked={publishMode === 'new'}
                  onChange={() => setPublishMode('new')}
                />{' '}
                Create new Endpoint Alias
              </label>
              <label>
                <input
                  type="radio"
                  checked={publishMode === 'existing'}
                  onChange={() => {
                    setPublishMode('existing')
                    void loadEndpoints()
                  }}
                />{' '}
                Select existing Alias (no active route)
              </label>
              {publishMode === 'new' ? (
                <label>
                  Alias
                  <input
                    value={alias}
                    onChange={(e) => setAlias(e.target.value)}
                  />
                </label>
              ) : (
                <label>
                  Endpoint
                  <select
                    value={endpointId}
                    onChange={(e) => setEndpointId(e.target.value)}
                  >
                    <option value="">Select…</option>
                    {endpoints.map((ep) => (
                      <option
                        key={ep.id}
                        value={ep.id}
                        disabled={!!ep.active_route}
                      >
                        {ep.alias}
                        {ep.active_route
                          ? ' (has ACTIVE route → use Switch)'
                          : ''}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label>
                rewrite_model_name
                <input
                  value={rewriteModelName}
                  onChange={(e) => setRewriteModelName(e.target.value)}
                />
              </label>
              <p className="secondary-text">
                Gateway clients use the Alias; upstream receives the served model
                name.
              </p>
              <button
                type="button"
                className="btn"
                disabled={busy || !healthy || publishStatusLoading}
                onClick={() => void onPublish()}
              >
                {busy ? 'Publishing…' : 'Publish + verify Gateway'}
              </button>
            </>
          )}
        </section>
      ) : null}

      {step === 'done' ? (
        <section className="panel">
          <h2>8. Gateway verification</h2>
          {publishStatusLoading ? (
            <LoadingBlock label="Restoring published state…" />
          ) : publishResult ? (
            <>
              <p>{publishResult.note}</p>
              <p>
                Endpoint{' '}
                <Link to={`/endpoints/${publishResult.endpoint.id}`}>
                  {publishResult.endpoint.alias}
                </Link>{' '}
                ({publishResult.endpoint.api_type})
              </p>
              <p>
                Verification:{' '}
                <StatusBadge
                  status={
                    publishResult.gateway_verification?.status || 'SKIPPED'
                  }
                />
              </p>
              {deployment ? (
                <p>
                  Deployment{' '}
                  <Link to={`/deployments/${deployment.id}`}>
                    {deployment.name}
                  </Link>
                </p>
              ) : null}
              <button
                type="button"
                className="btn"
                onClick={() => navigate('/models/cache')}
              >
                Back to Model Cache
              </button>
            </>
          ) : (
            <>
              <SectionError
                title="Not published"
                message="No ACTIVE route targets this Deployment yet. Return to Publish."
              />
              <button
                type="button"
                className="btn"
                onClick={() =>
                  syncStep('publish', {
                    deploymentId: deploymentIdParam || deployment?.id,
                  })
                }
              >
                Back to Publish
              </button>
            </>
          )}
        </section>
      ) : null}
    </AppShell>
  )
}
