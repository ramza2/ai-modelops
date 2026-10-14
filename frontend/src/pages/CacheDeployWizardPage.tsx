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
import { getNode, refreshNodeResources } from '../api/nodes'
import { displayedGpuFreeMb, fitInputFingerprint, isFreshResourceSnapshot } from '../utils/cacheDeployFit'
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
import { formatApiDateTime, operationElapsed } from '../utils/date'
import {
  createDetailFetchGate,
  formatOperationError,
  healthWaitElapsedMsFromFinishedAt,
  isDeploymentReadyForPublish,
  OPERATION_POLL_MS,
  operationPollConfig,
  resolveWizardLifecyclePhase,
  shouldPollWizardLifecycle,
} from '../utils/operationLifecycle'

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
  const [gpuResourceLoading, setGpuResourceLoading] = useState(false)
  const [gpuResourceError, setGpuResourceError] = useState<string | null>(null)
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
  const [fitKey, setFitKey] = useState('')
  const [fitLoading, setFitLoading] = useState(false)

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
  const lifecycleGateRef = useRef(createDetailFetchGate())
  const activeOpIdRef = useRef(operationIdParam)
  activeOpIdRef.current = operationIdParam
  const activeDepIdRef = useRef(deploymentIdParam)
  activeDepIdRef.current = deploymentIdParam
  const finishedAtRef = useRef<string | null>(null)
  const busyRef = useRef(false)
  busyRef.current = busy
  const restoredCacheRef = useRef<string | null>(null)
  const restoredPublishRef = useRef<string | null>(null)
  const [restoredDraft, setRestoredDraft] = useState(false)
  const [reviewNeedsFit, setReviewNeedsFit] = useState(false)
  /** Forces re-render when finished_at-based health wait crosses the max. */
  const [healthWaitTick, setHealthWaitTick] = useState(0)
  const [pollWarning, setPollWarning] = useState<string | null>(null)
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
          setFitKey('')
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
          const n = await getNode(entry.node_id, controller.signal)
          if (controller.signal.aborted) return
          setNode(n)
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

  // GPU list shows a newly refreshed Node Agent snapshot, not an old persisted
  // /resources/latest value. Fit and Create independently fetch live resources.
  useEffect(() => {
    if (step !== 'gpu' || !node?.id) return
    let active = true
    setResources(null)
    setGpuResourceLoading(true)
    setGpuResourceError(null)
    void refreshNodeResources(node.id)
      .then((fresh) => { if (active) setResources(fresh) })
      .catch((err: unknown) => {
        if (active) setGpuResourceError(
          err instanceof ApiError ? err.message : 'GPU resource refresh failed.',
        )
      })
      .finally(() => { if (active) setGpuResourceLoading(false) })
    return () => { active = false }
  }, [step, node?.id])

  // Save only operator inputs. Backend remains source of truth for Fit,
  // deployment runtime, operation status, and ACTIVE route.
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
  const currentFitKey = fitInputFingerprint(selectedGpuIds, expectedVramMb, runtime)
  const currentFitKeyRef = useRef(currentFitKey)
  currentFitKeyRef.current = currentFitKey
  const fitRevisionRef = useRef(0)
  // Invalidate even if inputs later change back to their previous values.
  useEffect(() => {
    fitRevisionRef.current += 1
    setFit(null)
    setFitKey('')
    setAckUnknown(false)
  }, [currentFitKey])
  const activeFit = fitKey === currentFitKey ? fit : null

  const refreshFit = async () => {
    if (!cacheId || uniqueGpuIds.length === 0) return
    const requestedKey = currentFitKey
    const requestedRevision = fitRevisionRef.current
    setFitLoading(true)
    setFit(null)
    setFitKey('')
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
      if (currentFitKeyRef.current !== requestedKey ||
          fitRevisionRef.current !== requestedRevision) return
      setFit(result)
      setFitKey(requestedKey)
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
    if (!cache || busy || fitLoading || !activeFit ||
        activeFit.result === 'INSUFFICIENT' ||
        (activeFit.result === 'UNKNOWN' && !ackUnknown)) return
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

  // Clear poll-only warning when Operation identity changes (not Operation failure).
  useEffect(() => {
    setPollWarning(null)
  }, [operationIdParam])

  finishedAtRef.current = operation?.finished_at ?? null

  const healthy = isDeploymentReadyForPublish(deployment)
  // healthWaitTick is only a re-render signal; elapsed always uses wall clock
  // vs Operation.finished_at so F5 cannot reset the 5m window.
  void healthWaitTick
  const healthWaitElapsedMs = healthWaitElapsedMsFromFinishedAt(
    operation?.finished_at,
  )
  const lifecyclePhase = resolveWizardLifecyclePhase({
    step,
    hasOperationId: Boolean(operationIdParam),
    operationStatus: operation?.status,
    deploymentReady: healthy,
    healthWaitElapsedMs,
  })

  // P1-4B: 4s silent poll on Start — Operation while active, then limited
  // Deployment health wait after SUCCEEDED (elapsed from finished_at so F5
  // does not reset the 5m window). Shares DetailFetchGate epoch.
  useEffect(() => {
    if (!shouldPollWizardLifecycle(lifecyclePhase)) return
    const opId = operationIdParam
    const depId = deploymentIdParam
    if (!opId) return

    let cancelled = false
    const timer = window.setInterval(() => {
      if (busyRef.current) return

      const elapsed = healthWaitElapsedMsFromFinishedAt(finishedAtRef.current)
      if (
        lifecyclePhase === 'health_wait' &&
        elapsed != null &&
        elapsed >= operationPollConfig.healthWaitMaxMs
      ) {
        setHealthWaitTick((n) => n + 1)
        return
      }

      const gate = lifecycleGateRef.current
      const { gen, signal } = gate.begin('poll')
      void (async () => {
        try {
          const op = await getOperation(opId, signal)
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeOpIdRef.current !== opId ||
            op.id !== opId
          ) {
            return
          }
          setOperation(op)

          if (depId) {
            const dep = await getDeployment(depId, signal)
            if (
              cancelled ||
              !gate.isCurrent(gen) ||
              activeDepIdRef.current !== depId ||
              activeOpIdRef.current !== opId ||
              dep.id !== depId
            ) {
              return
            }
            setDeployment(dep)
          }
          setLastUpdated(new Date())
          setPollWarning(null)
        } catch (err: unknown) {
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeOpIdRef.current !== opId
          ) {
            return
          }
          if (err instanceof DOMException && err.name === 'AbortError') return
          // Keep last known Operation/Deployment; do not wipe wizard inputs.
          setPollWarning(
            '자동 상태 갱신에 실패했습니다. 마지막 성공 조회 결과를 유지합니다.',
          )
        }
      })()
    }, operationPollConfig.ms)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      if (lifecycleGateRef.current.currentKind() === 'poll') {
        lifecycleGateRef.current.abort()
      }
    }
  }, [
    lifecyclePhase,
    operationIdParam,
    deploymentIdParam,
  ])

  useEffect(() => {
    return () => {
      lifecycleGateRef.current.abort()
    }
  }, [cacheId, operationIdParam, deploymentIdParam, step])

  const loadEndpoints = async () => {
    const page = await listEndpoints({ page: 1, pageSize: 100 })
    const apiType =
      (model?.model_type || '').toUpperCase() === 'EMBEDDING' ? 'EMBEDDING' : 'CHAT'
    setEndpoints(page.items.filter((e) => e.api_type === apiType))
  }

  // After F5, the existing-alias dropdown must be repopulated.
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
        <p role="alert">Review 이전 설정을 복원했습니다. GPU/Runtime을 확인한 후 Fresh Fit을 다시 실행해 주세요.</p>
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
            <label className="form-grid__wide">
              Deployment name
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="form-grid__wide">
              Container name
              <input
                value={containerName}
                onChange={(e) => setContainerName(e.target.value)}
              />
            </label>
            <label className="form-grid__wide">
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
            <label>
              Reasoning Parser (optional)
              <select
                aria-label="Reasoning Parser"
                value={runtime.reasoning_parser ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    reasoning_parser: e.target.value || null,
                  }))
                }
              >
                <option value="">(미설정)</option>
                <option value="qwen3">qwen3</option>
              </select>
            </label>
            <label>
              vLLM Model Runner (optional)
              <select
                aria-label="vLLM Model Runner"
                value={runtime.vllm_model_runner ?? ''}
                onChange={(e) =>
                  setRuntime((r) => ({
                    ...r,
                    vllm_model_runner: e.target.value || null,
                  }))
                }
              >
                <option value="">Auto (default)</option>
                <option value="v1">V1</option>
              </select>
            </label>
          </div>
          <p className="secondary-text">
            Catalog dtype/quant hints are suggestions only. Official vLLM image
            ENTRYPOINT contract remains <code>vllm serve …</code>.
          </p>
          {runtime.reasoning_parser === 'qwen3' ? (
            <p className="panel-hint" role="note">
              Reasoning Parser는 Runtime 시작 옵션입니다. Chat 응답에{' '}
              <code>reasoning</code>/<code>content</code> 분리가 생길 수 있으므로
              업무 클라이언트 호환성을 운영에서 검증하세요. 요청별{' '}
              <code>enable_thinking</code> / <code>thinking_token_budget</code> /
              <code>max_tokens</code>는 Parser와 별개입니다.
            </p>
          ) : null}
          {runtime.vllm_model_runner === 'v1' ? (
            <p className="panel-hint" role="note">
              V1 Model Runner는 컨테이너 환경변수{' '}
              <code>VLLM_USE_V2_MODEL_RUNNER=0</code>만 설정합니다. vLLM 0.24.0에서
              Qwen3 <code>thinking_token_budget</code> 등이 V2에서 거부될 때
              사용하세요. 호환성·성능·메모리 특성을 운영에서 검증하세요. 기존
              컨테이너는 STOP 후 START(ENSURE_CONTAINER)로 재생성되어야 적용됩니다.
            </p>
          ) : null}
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
            GPU 단계 진입 시 Node Agent에서 최신 스냅샷을 수집합니다.
            저장된 측정값은 실시간 스트리밍이 아닙니다.
            {typeof resources?.age_ms === 'number'
              ? ` · age ${Math.round(resources.age_ms / 1000)}s`
              : ''}
            . Fresh Fit은 선택한 GPU를 UUID 기준으로 다시 측정합니다.
            VRAM은 GPU 간 합산되지 않습니다.
          </p>
          {gpuResourceLoading ? <p className="secondary-text">GPU 메모리 측정 중…</p> : null}
          {gpuResourceError ? (
            <p role="alert">GPU 리소스 갱신 실패: {gpuResourceError}. Fresh Fit으로 별도 확인해 주세요.</p>
          ) : null}
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
                  const live = activeFit?.gpu_results.find((x) => x.gpu_device_id === g.id)
                  const freshSnapshot = isFreshResourceSnapshot(snap?.sampled_at)
                  // An UNKNOWN live free value must not fall back to an old snapshot.
                  const freeMb = displayedGpuFreeMb(live, snap)
                  const measuredAt = live
                    ? activeFit?.resource_sampled_at || activeFit?.evaluated_at
                    : freshSnapshot ? snap?.sampled_at : null
                  const checked = selectedGpuIds.includes(g.id)
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
                          {measuredAt
                            ? `${live ? 'Fresh Fit' : 'Node Agent refresh'} · ${measuredAt}`
                            : gpuResourceLoading ? '측정 중…' : '최신 측정값 없음'}
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
          {activeFit ? (
            <div style={{ marginTop: '0.75rem' }}>
              <StatusBadge status={activeFit.result} />{' '}
              <span className="secondary-text">
                Node Agent sample:{' '}
                {activeFit.resource_sampled_at || activeFit.evaluated_at || 'time unknown'}
              </span>
              <ul>
                {activeFit.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              {activeFit.gpu_results.map((g) => (
                <p key={g.gpu_device_id} className="secondary-text">
                  GPU #{g.gpu_index ?? '?'} {g.gpu_uuid ? `(${g.gpu_uuid})` : ''}:{' '}
                  {g.vram_free_mb ?? '—'} free / {g.vram_used_mb ?? '—'} used /{' '}
                  {g.vram_total_mb} total MiB; estimated {g.estimated_required_vram_mb ?? '—'} MiB,
                  safety margin {g.safety_margin_mb} MiB — {g.result}
                  {g.reasons.length ? ` (${g.reasons.join('; ')})` : ''}
                </p>
              ))}
              {activeFit.assumptions.length > 0 ? (
                <p className="secondary-text">
                  Assumptions: {activeFit.assumptions.join(' / ')}
                </p>
              ) : null}
              {activeFit.result === 'UNKNOWN' ? (
                <label>
                  <input
                    type="checkbox"
                    checked={ackUnknown}
                    onChange={(e) => setAckUnknown(e.target.checked)}
                  />{' '}
                  Acknowledge UNKNOWN fit and proceed
                </label>
              ) : null}
              {activeFit.result === 'INSUFFICIENT' ? (
                <p className="secondary-text">
                  INSUFFICIENT blocks Deployment creation.
                </p>
              ) : null}
            </div>
          ) : (
            <p className="secondary-text">
              현재 GPU/Runtime 설정으로 Fresh Fit을 다시 실행해야 Review를 진행할 수 있습니다.
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
                fitLoading || !activeFit ||
                activeFit.result === 'INSUFFICIENT' ||
                (activeFit.result === 'UNKNOWN' && !ackUnknown)
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
                fit: activeFit?.result,
              },
              null,
              2,
            )}
          </pre>
          <p className="secondary-text">
            Create is metadata only — Start is a separate step.
            {!activeFit ? ' Run fresh Fit again after a reload or input change.' : ''}
          </p>
          <div className="toolbar__actions">
            <button type="button" className="btn btn--ghost" onClick={() => syncStep('gpu')}>
              Back
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || fitLoading || !activeFit ||
                activeFit.result === 'INSUFFICIENT' ||
                (activeFit.result === 'UNKNOWN' && !ackUnknown)}
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
            <StatusBadge status={deployment.desired_state} />{' '}
            <StatusBadge status={deployment.runtime_status} />{' '}
            <StatusBadge status={deployment.health_status} />
          </p>
          {operation ? (
            <div className="operation-result" role="status">
              {lifecyclePhase === 'operation_active' ? (
                <p className="panel-hint">
                  Operation 진행 중 — Operation/Deployment를{' '}
                  {OPERATION_POLL_MS / 1000}초마다 자동 갱신합니다.
                </p>
              ) : null}
              {lifecyclePhase === 'health_wait' ? (
                <p className="panel-hint">
                  Operation은 SUCCEEDED입니다. Deployment가 RUNNING + HEALTHY가
                  될 때까지 제한적으로 상태를 확인합니다 (최대{' '}
                  {Math.round(operationPollConfig.healthWaitMaxMs / 60000)}분,
                  Operation 완료 시각 기준).
                </p>
              ) : null}
              {lifecyclePhase === 'health_timeout' ? (
                <SectionError
                  title="Health 대기 시간 초과"
                  message="Operation은 성공했지만 Deployment가 RUNNING + HEALTHY로 전환되지 않았습니다. Deployment 상세에서 상태를 확인하세요."
                />
              ) : null}
              {lifecyclePhase === 'failed' ? (
                <SectionError
                  title="Operation 실패"
                  message={
                    formatOperationError(operation.error) !== '—'
                      ? formatOperationError(operation.error)
                      : `Operation status: ${operation.status}`
                  }
                />
              ) : null}
              {lifecyclePhase === 'ready' ? (
                <p className="panel-hint" role="status">
                  Deployment가 RUNNING + HEALTHY입니다. Publish로 진행할 수
                  있습니다.
                </p>
              ) : null}
              {pollWarning ? (
                <p className="panel-hint" role="status" data-testid="poll-warning">
                  {pollWarning}
                </p>
              ) : null}
              <dl className="meta-grid">
                <div>
                  <dt>Operation ID</dt>
                  <dd>
                    <Link
                      className="table-link mono"
                      to={`/operations/${operation.id}`}
                    >
                      {operation.id}
                    </Link>
                  </dd>
                </div>
                <div>
                  <dt>Status</dt>
                  <dd>
                    <StatusBadge status={operation.status} />
                  </dd>
                </div>
                <div>
                  <dt>Current Step</dt>
                  <dd className="mono">{operation.current_step || '—'}</dd>
                </div>
                <div>
                  <dt>Elapsed</dt>
                  <dd>{operationElapsed(operation)}</dd>
                </div>
                <div>
                  <dt>Error</dt>
                  <dd>{formatOperationError(operation.error)}</dd>
                </div>
                <div>
                  <dt>Last updated</dt>
                  <dd>
                    {lastUpdated
                      ? formatApiDateTime(lastUpdated.toISOString())
                      : '—'}
                  </dd>
                </div>
              </dl>
              {(operation.steps || []).length > 0 ? (
                <ul>
                  {(operation.steps || []).map((s) => (
                    <li key={s.id}>
                      {s.step_code}: <StatusBadge status={s.status} />{' '}
                      {s.error?.message || ''}
                    </li>
                  ))}
                </ul>
              ) : null}
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
