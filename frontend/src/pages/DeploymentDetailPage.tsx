import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getDeployment,
  restartDeployment,
  startDeployment,
  stopDeployment,
} from '../api/deployments'
import { getModelVersion } from '../api/models'
import { getNode } from '../api/nodes'
import type {
  Deployment,
  LifecycleOperation,
  ModelVersion,
  NodeDetail,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime, shortId } from '../utils/date'
import {
  abbreviateMiddle,
  pickRuntimeConfigEntries,
  safeRuntimeConfigValue,
} from '../utils/format'
import { formatInt, formatMemoryMb } from '../utils/number'

type LifecycleAction = 'start' | 'stop' | 'restart'

export function DeploymentDetailPage() {
  const { deploymentId = '' } = useParams<{ deploymentId: string }>()

  const [deployment, setDeployment] = useState<Deployment | null>(null)
  const [version, setVersion] = useState<ModelVersion | null>(null)
  const [node, setNode] = useState<NodeDetail | null>(null)

  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [versionWarning, setVersionWarning] = useState<string | null>(null)
  const [nodeWarning, setNodeWarning] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const [mutating, setMutating] = useState<LifecycleAction | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [lastOperation, setLastOperation] = useState<LifecycleOperation | null>(
    null,
  )

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const refAbortRef = useRef<AbortController | null>(null)

  const loadRefs = useCallback(
    async (dep: Deployment, gen: number) => {
      refAbortRef.current?.abort()
      const controller = new AbortController()
      refAbortRef.current = controller
      setVersionWarning(null)
      setNodeWarning(null)

      const versionPromise = getModelVersion(
        dep.model_version_id,
        controller.signal,
      )
        .then((data) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          setVersion(data)
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Model Version을 불러오지 못했습니다.'
          setVersionWarning(message)
        })

      const nodePromise = dep.node_id
        ? getNode(dep.node_id, controller.signal)
            .then((data) => {
              if (controller.signal.aborted || gen !== readGenRef.current) {
                return
              }
              setNode(data)
            })
            .catch((err: unknown) => {
              if (controller.signal.aborted || gen !== readGenRef.current) {
                return
              }
              if (err instanceof DOMException && err.name === 'AbortError') {
                return
              }
              const message =
                err instanceof ApiError
                  ? err.message
                  : err instanceof Error
                    ? err.message
                    : 'Node를 불러오지 못했습니다.'
              setNodeWarning(message)
            })
        : Promise.resolve().then(() => {
            if (!controller.signal.aborted && gen === readGenRef.current) {
              setNode(null)
            }
          })

      await Promise.all([versionPromise, nodePromise])
    },
    [],
  )

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!deploymentId) {
        setNotFound(true)
        setLoading(false)
        setError('Deployment를 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial') {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)
      setNotFound(false)

      try {
        const data = await getDeployment(deploymentId, controller.signal)
        if (controller.signal.aborted || gen !== readGenRef.current) return
        setDeployment(data)
        setNotFound(false)
        setLastUpdated(new Date())
        await loadRefs(data, gen)
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (err instanceof ApiError && err.status === 404) {
          setDeployment(null)
          setVersion(null)
          setNode(null)
          setNotFound(true)
          setError('Deployment를 찾을 수 없습니다.')
          return
        }
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Deployment를 불러오지 못했습니다.'
        setError(message)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [deploymentId, loadRefs],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
      refAbortRef.current?.abort()
    }
  }, [load])

  const runLifecycle = async (action: LifecycleAction) => {
    if (!deploymentId || mutating) return
    setMutating(action)
    setActionError(null)
    try {
      const runner =
        action === 'start'
          ? startDeployment
          : action === 'stop'
            ? stopDeployment
            : restartDeployment
      const op = await runner(deploymentId)
      setLastOperation(op)
      await load('refresh')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Lifecycle Operation enqueue에 실패했습니다.'
      setActionError(message)
    } finally {
      setMutating(null)
    }
  }

  if (notFound) {
    return (
      <AppShell
        title="Deployments"
        description="Deployment 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/deployments">
          ← Deployments
        </Link>
        <SectionError
          title="Deployment를 찾을 수 없습니다."
          message="요청한 Deployment가 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const canLifecycle =
    deployment?.deployment_type === 'MANAGED' && !deployment.retired_at
  const configEntries = pickRuntimeConfigEntries(deployment?.deployment_config)
  const busy = mutating !== null

  return (
    <AppShell
      title={deployment?.name ?? 'Deployment 상세'}
      description="Deployment 메타데이터와 desired/runtime/health 상태입니다. lifecycle은 Management API Operation enqueue이며 Worker가 비동기로 실행합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={busy}
    >
      <Link className="back-link" to="/deployments">
        ← Deployments
      </Link>

      {loading && !deployment ? (
        <LoadingBlock label="Deployment를 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={
            deployment ? 'Deployment 새로고침 실패' : 'Deployment 오류'
          }
          message={
            deployment
              ? `기존 Deployment 정보를 표시하고 있습니다. 새로고침 실패: ${error}`
              : error
          }
        />
      ) : null}

      {versionWarning ? (
        <SectionError title="Model Version 경고" message={versionWarning} />
      ) : null}
      {nodeWarning ? (
        <SectionError title="Node 경고" message={nodeWarning} />
      ) : null}

      {deployment ? (
        <>
          <section
            className="detail-panel"
            aria-labelledby="deployment-identity-heading"
          >
            <div className="detail-panel__header">
              <h2 id="deployment-identity-heading">{deployment.name}</h2>
              <span
                className={`status-badge status-badge--${deployment.retired_at ? 'muted' : 'ok'}`}
              >
                <span className="status-badge__dot" aria-hidden="true" />
                <span className="status-badge__text">
                  {deployment.retired_at ? 'Retired' : 'Active'}
                </span>
              </span>
            </div>
            <dl className="meta-grid">
              <div>
                <dt>Type</dt>
                <dd>{deployment.deployment_type || '—'}</dd>
              </div>
              <div>
                <dt>Desired State</dt>
                <dd>
                  <StatusBadge status={deployment.desired_state} />
                </dd>
              </div>
              <div>
                <dt>Runtime Status</dt>
                <dd>
                  <StatusBadge status={deployment.runtime_status} />
                </dd>
              </div>
              <div>
                <dt>Health Status</dt>
                <dd>
                  <StatusBadge status={deployment.health_status} />
                </dd>
              </div>
              <div>
                <dt>Model Version</dt>
                <dd>
                  <Link
                    className="table-link"
                    to={`/model-versions/${deployment.model_version_id}`}
                  >
                    {version?.version_label ??
                      shortId(deployment.model_version_id)}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Node</dt>
                <dd>
                  {deployment.node_id ? (
                    <Link
                      className="table-link"
                      to={`/nodes/${deployment.node_id}`}
                    >
                      {node?.name ?? shortId(deployment.node_id)}
                    </Link>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
              <div>
                <dt>Container Name</dt>
                <dd className="mono">{deployment.container_name || '—'}</dd>
              </div>
              <div>
                <dt>Container ID</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={deployment.container_id}
                    abbreviated={abbreviateMiddle(
                      deployment.container_id,
                      12,
                      8,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Upstream Base URL</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={deployment.upstream_base_url}
                    abbreviated={abbreviateMiddle(
                      deployment.upstream_base_url,
                      28,
                      12,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Runtime Port</dt>
                <dd>{formatInt(deployment.runtime_port)}</dd>
              </div>
              <div>
                <dt>Last Started</dt>
                <dd>{formatApiDateTime(deployment.last_started_at)}</dd>
              </div>
              <div>
                <dt>Last Stopped</dt>
                <dd>{formatApiDateTime(deployment.last_stopped_at)}</dd>
              </div>
              <div>
                <dt>Last Health</dt>
                <dd>{formatApiDateTime(deployment.last_health_at)}</dd>
              </div>
              <div>
                <dt>Status Reason</dt>
                <dd>{deployment.status_reason || '—'}</dd>
              </div>
              <div>
                <dt>Retired At</dt>
                <dd>
                  {deployment.retired_at
                    ? formatApiDateTime(deployment.retired_at)
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{formatApiDateTime(deployment.created_at)}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{formatApiDateTime(deployment.updated_at)}</dd>
              </div>
            </dl>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="lifecycle-heading"
          >
            <h2 id="lifecycle-heading">Lifecycle</h2>
            <p className="panel-hint">
              Start / Stop / Restart는 202 Accepted Operation을 enqueue합니다.
              Frontend는 Node Agent나 Gateway를 직접 호출하지 않습니다. Worker가
              완료할 때까지 runtime/health는 즉시 바뀌지 않을 수 있습니다.
            </p>

            {canLifecycle ? (
              <div className="action-row" role="group" aria-label="Lifecycle">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy}
                  aria-busy={mutating === 'start'}
                  onClick={() => void runLifecycle('start')}
                >
                  {mutating === 'start' ? 'Enqueue 중…' : 'Start'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  aria-busy={mutating === 'stop'}
                  onClick={() => void runLifecycle('stop')}
                >
                  {mutating === 'stop' ? 'Enqueue 중…' : 'Stop'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  aria-busy={mutating === 'restart'}
                  onClick={() => void runLifecycle('restart')}
                >
                  {mutating === 'restart' ? 'Enqueue 중…' : 'Restart'}
                </button>
                <Link
                  className="btn"
                  to={`/deployments/${deployment.id}/decommission`}
                >
                  Decommission
                </Link>
              </div>
            ) : (
              <p className="metric-line" role="status">
                {deployment.retired_at
                  ? 'Retired Deployment에는 lifecycle action을 사용할 수 없습니다.'
                  : 'IMPORTED Deployment에는 lifecycle action을 노출하지 않습니다.'}
              </p>
            )}

            {actionError ? (
              <SectionError title="Lifecycle enqueue 실패" message={actionError} />
            ) : null}

            {lastOperation ? (
              <div className="operation-result" role="status">
                <h3 className="section-subheading">최근 enqueue Operation</h3>
                <dl className="meta-grid">
                  <div>
                    <dt>Operation ID</dt>
                    <dd className="mono">{lastOperation.id}</dd>
                  </div>
                  <div>
                    <dt>Type</dt>
                    <dd>{lastOperation.operation_type}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd>
                      <StatusBadge status={lastOperation.status} />
                    </dd>
                  </div>
                  <div>
                    <dt>Current Step</dt>
                    <dd>{lastOperation.current_step || '—'}</dd>
                  </div>
                  <div>
                    <dt>Created</dt>
                    <dd>{formatApiDateTime(lastOperation.created_at)}</dd>
                  </div>
                </dl>
              </div>
            ) : null}
          </section>

          <section
            className="detail-panel"
            aria-labelledby="gpu-assignments-heading"
          >
            <h2 id="gpu-assignments-heading">GPU Assignments</h2>
            <p className="panel-hint">
              Deployment에 저장된 GPU 할당입니다. 현재 free VRAM이나 Preflight
              판정이 아닙니다.
            </p>
            {deployment.gpu_assignments.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Order</th>
                      <th scope="col">GPU Device</th>
                      <th scope="col">Expected VRAM</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deployment.gpu_assignments.map((a) => (
                      <tr key={`${a.gpu_device_id}-${a.device_order}`}>
                        <td>{a.device_order}</td>
                        <td className="mono">{shortId(a.gpu_device_id, 12)}</td>
                        <td>{formatMemoryMb(a.expected_vram_mb)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty-state" role="status">
                할당된 GPU가 없습니다.
              </p>
            )}
          </section>

          <section
            className="detail-panel"
            aria-labelledby="deployment-config-heading"
          >
            <h2 id="deployment-config-heading">Deployment Config</h2>
            <p className="panel-hint">
              Deployment에 저장된 override 정의입니다. Model Version
              runtime_config 또는 실제 컨테이너 observed 값과 다를 수 있습니다.
              Capacity Profile / runtime observability는 C4에서 호출하지 않습니다.
            </p>
            <dl className="meta-grid">
              {configEntries.known.map(({ key, value }) => (
                <div key={key}>
                  <dt>deployment_config.{key}</dt>
                  <dd className="mono">{safeRuntimeConfigValue(value)}</dd>
                </div>
              ))}
            </dl>
            {configEntries.known.length === 0 ? (
              <p className="empty-state" role="status">
                표시할 알려진 deployment_config 항목이 없습니다.
              </p>
            ) : null}
            {configEntries.otherCount > 0 ? (
              <p className="metric-line">
                기타 설정 {configEntries.otherCount}개
              </p>
            ) : null}
          </section>
        </>
      ) : null}
    </AppShell>
  )
}
