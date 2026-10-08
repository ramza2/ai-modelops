import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  createDeploymentFromCache,
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

  const stepParam = (searchParams.get('step') as Step | null) || 'cache'
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
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const abortRef = useRef<AbortController | null>(null)

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
        setName((n) => n || `${repoLeaf}-managed`)
        setContainerName((c) => c || `modelops-${repoLeaf}`.replace(/[^a-zA-Z0-9._-]/g, '-'))
        setAlias((a) => a || repoLeaf)

        if (entry.model_version_id) {
          const ver = await getModelVersion(entry.model_version_id, controller.signal)
          if (controller.signal.aborted) return
          setVersion(ver)
          setServedModelName((s) => s || ver.served_model_name)
          setRewriteModelName((s) => s || ver.served_model_name)
          const mod = await getModel(ver.model_id, controller.signal)
          if (controller.signal.aborted) return
          setModel(mod)
          const d = defaultsForModelType(mod.model_type, entry.repository_id)
          setKnownProfileLabel(d.knownProfileLabel)
          setRuntime((prev) => ({ ...d.runtime, ...prev }))
        }
        if (entry.node_id) {
          const [n, res] = await Promise.all([
            getNode(entry.node_id, controller.signal),
            getNodeResources(entry.node_id, controller.signal).catch(() => null),
          ])
          if (controller.signal.aborted) return
          setNode(n)
          setResources(res)
          if (n.gpus[0] && selectedGpuIds.length === 0) {
            setSelectedGpuIds([n.gpus[0].id])
          }
        }

        if (deploymentIdParam) {
          const dep = await getDeployment(deploymentIdParam, controller.signal)
          if (controller.signal.aborted) return
          setDeployment(dep)
        }
        if (operationIdParam) {
          const op = await getOperation(operationIdParam, controller.signal)
          if (controller.signal.aborted) return
          setOperation(op)
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
  }, [cacheId, deploymentIdParam, operationIdParam])

  const refreshFit = async () => {
    if (!cacheId || selectedGpuIds.length === 0) return
    setFitLoading(true)
    setActionError(null)
    try {
      const result = await previewCacheDeployFit(cacheId, {
        gpuDeviceIds: selectedGpuIds,
        tensorParallel: selectedGpuIds.length,
        expectedVramMb: expectedVramMb ? Number(expectedVramMb) : null,
        dtype: runtime.dtype,
        quantization: runtime.quantization,
      })
      setFit(result)
      setRuntime((r) => ({ ...r, tensor_parallel_size: selectedGpuIds.length }))
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Fresh fit preview failed.',
      )
    } finally {
      setFitLoading(false)
    }
  }

  const onCreate = async () => {
    if (!cache || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const created = await createDeploymentFromCache(cache.id, {
        name,
        containerName,
        gpuDeviceIds: selectedGpuIds,
        runtimePort,
        servedModelName,
        expectedVramMb: expectedVramMb ? Number(expectedVramMb) : null,
        acknowledgeUnknownFit: ackUnknown,
        runtimeConfig: {
          ...runtime,
          tensor_parallel_size: selectedGpuIds.length,
        },
      })
      setDeployment(created)
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
            Live free/total from Node resources. Disk is not the main gate after
            cache READY. VRAM is never pooled across GPUs.
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
                      </td>
                      <td>
                        {snap?.vram_free_mb ?? '—'} / {g.vram_total_mb} MiB
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
          {fit ? (
            <div style={{ marginTop: '0.75rem' }}>
              <StatusBadge status={fit.result} />
              <ul>
                {fit.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              {fit.result === 'UNKNOWN' ? (
                <label>
                  <input
                    type="checkbox"
                    checked={ackUnknown}
                    onChange={(e) => setAckUnknown(e.target.checked)}
                  />{' '}
                  Acknowledge UNKNOWN fit and proceed
                </label>
              ) : null}
              {fit.result === 'INSUFFICIENT' ? (
                <p className="secondary-text">
                  INSUFFICIENT blocks Deployment creation.
                </p>
              ) : null}
            </div>
          ) : null}
          <div className="toolbar__actions">
            <button type="button" className="btn btn--ghost" onClick={() => syncStep('runtime')}>
              Back
            </button>
            <button
              type="button"
              className="btn"
              disabled={
                selectedGpuIds.length === 0 ||
                fit?.result === 'INSUFFICIENT' ||
                (fit?.result === 'UNKNOWN' && !ackUnknown)
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
                fit: fit?.result,
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
              disabled={busy}
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
              <input value={alias} onChange={(e) => setAlias(e.target.value)} />
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
                    {ep.active_route ? ' (has ACTIVE route → use Switch)' : ''}
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
            disabled={busy || !healthy}
            onClick={() => void onPublish()}
          >
            {busy ? 'Publishing…' : 'Publish + verify Gateway'}
          </button>
        </section>
      ) : null}

      {step === 'done' && publishResult ? (
        <section className="panel">
          <h2>8. Gateway verification</h2>
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
              status={publishResult.gateway_verification?.status || 'SKIPPED'}
            />
          </p>
          {deployment ? (
            <p>
              Deployment{' '}
              <Link to={`/deployments/${deployment.id}`}>{deployment.name}</Link>
            </p>
          ) : null}
          <button
            type="button"
            className="btn"
            onClick={() => navigate('/models/cache')}
          >
            Back to Model Cache
          </button>
        </section>
      ) : null}
    </AppShell>
  )
}
