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
import { getOperation } from '../api/operations'
import type {
  Deployment,
  LifecycleOperation,
  ModelVersion,
  NodeDetail,
  OperationDetail,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime, operationElapsed, shortId } from '../utils/date'
import {
  abbreviateMiddle,
  pickRuntimeConfigEntries,
  safeRuntimeConfigValue,
} from '../utils/format'
import { formatInt, formatMemoryMb } from '../utils/number'
import {
  createDetailFetchGate,
  createStopSyncFallbackClock,
  formatOperationError,
  isActiveOperationStatus,
  isDeploymentStoppedForSync,
  OPERATION_POLL_MS,
  operationPollConfig,
  resolveDetailLifecyclePhase,
  shouldPollDetailLifecycle,
  stopSyncWaitElapsedMs,
} from '../utils/operationLifecycle'
import { DEPLOYMENT_FIELD_HELP } from '../utils/uiLabels'

type LifecycleAction = 'start' | 'stop' | 'restart'

function toLifecycleOperation(op: OperationDetail): LifecycleOperation {
  return {
    id: op.id,
    operation_type: op.operation_type,
    status: op.status,
    target_deployment_id: op.target_deployment_id,
    current_step: op.current_step,
    created_at: op.created_at,
    started_at: op.started_at,
    finished_at: op.finished_at,
    error: op.error,
  }
}

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
  /** Re-render signal when STOP sync wait crosses the max (finished_at or fallback). */
  const [stopWaitTick, setStopWaitTick] = useState(0)

  const fetchGateRef = useRef(createDetailFetchGate())
  const refAbortRef = useRef<AbortController | null>(null)
  const stopSyncFallbackRef = useRef(createStopSyncFallbackClock())
  const activeDeploymentIdRef = useRef(deploymentId)
  activeDeploymentIdRef.current = deploymentId
  const trackedOpIdRef = useRef<string | null>(null)
  trackedOpIdRef.current = lastOperation?.id ?? null
  const finishedAtRef = useRef<string | null>(null)
  finishedAtRef.current = lastOperation?.finished_at ?? null
  const refreshingRef = useRef(false)
  refreshingRef.current = refreshing
  const mutatingRef = useRef<LifecycleAction | null>(null)
  mutatingRef.current = mutating

  // stopWaitTick is only a re-render signal. Prefer trustworthy finished_at;
  // otherwise elapsed is anchored to the first STOP SUCCEEDED observation
  // for this Operation ID so null/invalid/future stamps cannot poll forever.
  void stopWaitTick
  const stopWaitElapsedMs =
    lastOperation?.id &&
    (lastOperation.operation_type || '').toUpperCase() === 'STOP' &&
    lastOperation.status === 'SUCCEEDED'
      ? stopSyncWaitElapsedMs({
          operationId: lastOperation.id,
          finishedAt: lastOperation.finished_at,
          fallback: stopSyncFallbackRef.current,
        })
      : null
  const lifecyclePhase = resolveDetailLifecyclePhase({
    operationType: lastOperation?.operation_type,
    operationStatus: lastOperation?.status,
    hasOperationId: Boolean(lastOperation?.id),
    deploymentStopped: isDeploymentStoppedForSync(deployment),
    stopWaitElapsedMs,
  })

  const loadRefs = useCallback(
    async (dep: Deployment, gen: number, gate: ReturnType<typeof createDetailFetchGate>) => {
      refAbortRef.current?.abort()
      const controller = new AbortController()
      refAbortRef.current = controller
      setVersionWarning(null)
      setNodeWarning(null)

      const stillCurrent = () =>
        !controller.signal.aborted &&
        gate.isCurrent(gen) &&
        activeDeploymentIdRef.current === String(dep.id)

      const versionPromise = getModelVersion(
        dep.model_version_id,
        controller.signal,
      )
        .then((data) => {
          if (!stillCurrent()) return
          setVersion(data)
        })
        .catch((err: unknown) => {
          if (!stillCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : '모델 버전을 불러오지 못했습니다.'
          setVersionWarning(message)
        })

      const nodePromise = dep.node_id
        ? getNode(dep.node_id, controller.signal)
            .then((data) => {
              if (!stillCurrent()) return
              setNode(data)
            })
            .catch((err: unknown) => {
              if (!stillCurrent()) return
              if (err instanceof DOMException && err.name === 'AbortError') {
                return
              }
              const message =
                err instanceof ApiError
                  ? err.message
                  : err instanceof Error
                    ? err.message
                    : '대상 서버를 불러오지 못했습니다.'
              setNodeWarning(message)
            })
        : Promise.resolve().then(() => {
            if (stillCurrent()) {
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
        setError('모델 배포를 찾을 수 없습니다.')
        return
      }

      const gate = fetchGateRef.current
      const { gen, signal } = gate.begin('load')

      if (mode === 'initial') {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)
      setNotFound(false)

      const isCurrent = () =>
        gate.isCurrent(gen) && activeDeploymentIdRef.current === deploymentId

      try {
        const data = await getDeployment(deploymentId, signal)
        if (!isCurrent()) return
        setDeployment(data)
        setNotFound(false)
        setLastUpdated(new Date())
        await loadRefs(data, gen, gate)
      } catch (err) {
        if (!isCurrent()) {
          if (
            activeDeploymentIdRef.current === deploymentId &&
            gate.currentKind() === 'poll'
          ) {
            setLoading(false)
            setRefreshing(false)
          }
          return
        }
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (err instanceof ApiError && err.status === 404) {
          setDeployment(null)
          setVersion(null)
          setNode(null)
          setNotFound(true)
          setError('모델 배포를 찾을 수 없습니다.')
          return
        }
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : '모델 배포를 불러오지 못했습니다.'
        setError(message)
      } finally {
        if (isCurrent()) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [deploymentId, loadRefs],
  )

  useEffect(() => {
    // New Deployment identity: drop prior Operation tracking immediately.
    setLastOperation(null)
    setActionError(null)
    setMutating(null)
    stopSyncFallbackRef.current.clear()
    void load('initial')
    return () => {
      fetchGateRef.current.abort()
      refAbortRef.current?.abort()
      stopSyncFallbackRef.current.clear()
    }
  }, [load])

  // While an enqueued lifecycle Operation is in-flight — or STOP SUCCEEDED
  // while runtime_status has not yet reached STOPPED — poll Operation +
  // Deployment every 4s. Silent — does not set refreshing.
  // Shares fetch epoch with manual refresh (latest response wins).
  useEffect(() => {
    const depId = deploymentId
    const opId = lastOperation?.id
    if (!depId || !opId) return
    if (!shouldPollDetailLifecycle(lifecyclePhase)) return

    let cancelled = false
    const timer = window.setInterval(() => {
      if (refreshingRef.current || mutatingRef.current) return

      if (lifecyclePhase === 'stop_wait') {
        const elapsed = stopSyncWaitElapsedMs({
          operationId: opId,
          finishedAt: finishedAtRef.current,
          fallback: stopSyncFallbackRef.current,
        })
        if (elapsed >= operationPollConfig.healthWaitMaxMs) {
          setStopWaitTick((n) => n + 1)
          return
        }
      }

      const gate = fetchGateRef.current
      const { gen, signal } = gate.begin('poll')
      void Promise.all([
        getOperation(opId, signal),
        getDeployment(depId, signal),
      ])
        .then(([op, dep]) => {
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeDeploymentIdRef.current !== depId ||
            trackedOpIdRef.current !== opId ||
            op.id !== opId ||
            dep.id !== depId
          ) {
            return
          }
          setLastOperation(toLifecycleOperation(op))
          setDeployment(dep)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeDeploymentIdRef.current !== depId ||
            trackedOpIdRef.current !== opId
          ) {
            return
          }
          if (err instanceof DOMException && err.name === 'AbortError') return
          // Keep last known Deployment / Operation snapshot.
        })
    }, operationPollConfig.ms)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      if (fetchGateRef.current.currentKind() === 'poll') {
        fetchGateRef.current.abort()
      }
    }
  }, [deploymentId, lastOperation?.id, lifecyclePhase])

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
            : '모델 실행 관리 작업 접수에 실패했습니다.'
      setActionError(message)
    } finally {
      setMutating(null)
    }
  }

  if (notFound) {
    return (
      <AppShell
        title="모델 배포 관리"
        description="모델 배포 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/deployments">
          ← 모델 배포 관리
        </Link>
        <SectionError
          title="모델 배포를 찾을 수 없습니다."
          message="요청한 모델 배포가 없거나 삭제되었습니다."
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
      title={deployment?.name ?? '모델 배포 상세'}
      description="모델 배포의 목표·실행·정상 동작 상태입니다. 시작·중지·재시작은 Management API 작업 접수이며 Worker가 비동기로 실행합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={busy}
    >
      <Link className="back-link" to="/deployments">
        ← 모델 배포 관리
      </Link>

      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="목표 / 실행 / 정상 동작">
            {DEPLOYMENT_FIELD_HELP.desiredVsRuntime}{' '}
            {DEPLOYMENT_FIELD_HELP.runtimeVsHealth}
          </HelpHint>
          <HelpHint showTopic label="MANAGED vs IMPORTED">
            {DEPLOYMENT_FIELD_HELP.managedVsImported}
          </HelpHint>
          <HelpHint showTopic label="퇴역 (Retired)">
            {DEPLOYMENT_FIELD_HELP.retired}
          </HelpHint>
          <HelpHint showTopic label="모델 실행 관리 (Lifecycle)">
            {DEPLOYMENT_FIELD_HELP.lifecycle}
          </HelpHint>
          <HelpHint showTopic label="STOP 상태 동기화">
            {DEPLOYMENT_FIELD_HELP.stopSync}
          </HelpHint>
          <HelpHint showTopic label="모델 종료·정리 절차">
            {DEPLOYMENT_FIELD_HELP.decommission}
          </HelpHint>
        </div>
      </details>

      {loading && !deployment ? (
        <LoadingBlock label="모델 배포를 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={
            deployment ? '모델 배포 새로고침 실패' : '모델 배포 오류'
          }
          message={
            deployment
              ? `원인: ${error} — 이전에 불러온 모델 배포 정보를 그대로 표시합니다. 네트워크·권한을 확인한 뒤 새로고침을 다시 시도하세요.`
              : error
          }
        />
      ) : null}

      {versionWarning ? (
        <SectionError title="모델 버전 경고" message={versionWarning} />
      ) : null}
      {nodeWarning ? (
        <SectionError title="대상 서버 경고" message={nodeWarning} />
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
                title={DEPLOYMENT_FIELD_HELP.retired}
              >
                <span className="status-badge__dot" aria-hidden="true" />
                <span className="status-badge__text">
                  {deployment.retired_at ? '퇴역 (Retired)' : '활성 (Active)'}
                </span>
              </span>
            </div>
            <p className="panel-hint">
              <Link
                className="table-link"
                to={`/observability/deployments/${deployment.id}`}
              >
                모델 실행·자원 모니터링 보기
              </Link>
              <span className="secondary-text">
                {' '}
                — {DEPLOYMENT_FIELD_HELP.monitoringLink}
              </span>
            </p>
            <dl className="meta-grid">
              <div>
                <dt>
                  <TermLabel
                    text="배포 유형"
                    apiTerm="Type"
                    help={DEPLOYMENT_FIELD_HELP.managedVsImported}
                  />
                </dt>
                <dd>{deployment.deployment_type || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="목표 상태"
                    apiTerm="Desired State"
                    help={DEPLOYMENT_FIELD_HELP.desiredVsRuntime}
                  />
                </dt>
                <dd>
                  <StatusBadge status={deployment.desired_state} />
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="실행 상태"
                    apiTerm="Runtime Status"
                    help={DEPLOYMENT_FIELD_HELP.desiredVsRuntime}
                  />
                </dt>
                <dd>
                  <StatusBadge status={deployment.runtime_status} />
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="정상 동작 상태"
                    apiTerm="Health Status"
                    help={DEPLOYMENT_FIELD_HELP.runtimeVsHealth}
                  />
                </dt>
                <dd>
                  <StatusBadge status={deployment.health_status} />
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="모델 버전" apiTerm="Model Version" />
                </dt>
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
                <dt>
                  <TermLabel text="대상 서버" apiTerm="Node" />
                </dt>
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
                <dt>
                  <TermLabel text="컨테이너 이름" apiTerm="Container Name" />
                </dt>
                <dd className="mono table-cell-clip" title={deployment.container_name || undefined}>
                  {deployment.container_name || '—'}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="컨테이너 ID" apiTerm="Container ID" />
                </dt>
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
                <dt>
                  <TermLabel
                    text="모델 실행 주소"
                    apiTerm="Upstream Base URL"
                  />
                </dt>
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
                <dt>
                  <TermLabel text="실행 포트" apiTerm="Runtime Port" />
                </dt>
                <dd>{formatInt(deployment.runtime_port)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="마지막 실행 시각" apiTerm="Last Started" />
                </dt>
                <dd>{formatApiDateTime(deployment.last_started_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="마지막 중지 시각" apiTerm="Last Stopped" />
                </dt>
                <dd>{formatApiDateTime(deployment.last_stopped_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="마지막 상태 확인"
                    apiTerm="Last Health"
                  />
                </dt>
                <dd>{formatApiDateTime(deployment.last_health_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="상태 설명" apiTerm="Status Reason" />
                </dt>
                <dd>{deployment.status_reason || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="퇴역 처리 시각"
                    apiTerm="Retired At"
                    help={DEPLOYMENT_FIELD_HELP.retired}
                  />
                </dt>
                <dd>
                  {deployment.retired_at
                    ? formatApiDateTime(deployment.retired_at)
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="생성 시각" apiTerm="Created" />
                </dt>
                <dd>{formatApiDateTime(deployment.created_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="수정 시각" apiTerm="Updated" />
                </dt>
                <dd>{formatApiDateTime(deployment.updated_at)}</dd>
              </div>
            </dl>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="lifecycle-heading"
          >
            <div className="detail-panel__header">
              <h2 id="lifecycle-heading">모델 실행 관리 (Lifecycle)</h2>
              <HelpHint label="모델 실행 관리">
                {DEPLOYMENT_FIELD_HELP.lifecycle}
              </HelpHint>
            </div>
            <p className="panel-hint">{DEPLOYMENT_FIELD_HELP.lifecycle}</p>
            <ul className="panel-hint-list">
              <li>{DEPLOYMENT_FIELD_HELP.lifecycleStart}</li>
              <li>{DEPLOYMENT_FIELD_HELP.lifecycleStop}</li>
              <li>{DEPLOYMENT_FIELD_HELP.lifecycleRestart}</li>
            </ul>

            {canLifecycle ? (
              <div
                className="action-row"
                role="group"
                aria-label="모델 실행 관리"
              >
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy}
                  aria-busy={mutating === 'start'}
                  title={DEPLOYMENT_FIELD_HELP.lifecycleStart}
                  onClick={() => void runLifecycle('start')}
                >
                  {mutating === 'start' ? '작업 접수 중…' : '모델 시작'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  aria-busy={mutating === 'stop'}
                  title={DEPLOYMENT_FIELD_HELP.lifecycleStop}
                  onClick={() => void runLifecycle('stop')}
                >
                  {mutating === 'stop' ? '작업 접수 중…' : '모델 중지'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  aria-busy={mutating === 'restart'}
                  title={DEPLOYMENT_FIELD_HELP.lifecycleRestart}
                  onClick={() => void runLifecycle('restart')}
                >
                  {mutating === 'restart' ? '작업 접수 중…' : '모델 재시작'}
                </button>
                <Link
                  className="btn"
                  to={`/deployments/${deployment.id}/decommission`}
                  title={DEPLOYMENT_FIELD_HELP.decommission}
                >
                  모델 종료·정리 절차
                </Link>
              </div>
            ) : (
              <p className="metric-line" role="status">
                {deployment.retired_at
                  ? '퇴역(Retired)된 모델 배포에는 시작·중지·재시작을 사용할 수 없습니다.'
                  : 'IMPORTED 모델 배포에는 시작·중지·재시작을 노출하지 않습니다.'}
              </p>
            )}
            <p className="panel-hint secondary-text">
              {DEPLOYMENT_FIELD_HELP.decommission}
            </p>

            {actionError ? (
              <SectionError
                title="모델 실행 관리 작업 접수 실패"
                message={`원인: ${actionError} — 버튼을 다시 시도하거나 네트워크·권한을 확인하세요.`}
              />
            ) : null}

            {lastOperation ? (
              <div className="operation-result" role="status">
                <h3 className="section-subheading">최근 작업 (Operation)</h3>
                <p className="panel-hint">
                  {DEPLOYMENT_FIELD_HELP.operationTracking}
                </p>
                {isActiveOperationStatus(lastOperation.status) ? (
                  <p className="panel-hint">
                    진행 중 — 작업·모델 배포를 {OPERATION_POLL_MS / 1000}
                    초마다 자동 갱신합니다.
                  </p>
                ) : null}
                {lifecyclePhase === 'stop_wait' ? (
                  <p className="panel-hint" data-testid="stop-sync-wait">
                    STOP 작업은 완료(SUCCEEDED)되었지만 모델 배포 실행 상태(
                    runtime_status)가 아직 STOPPED가 아닙니다. 상태 동기화를
                    위해 모델 배포를 {OPERATION_POLL_MS / 1000}초마다 자동
                    조회합니다. Health는 Backend 값을 그대로 표시하며 임의로
                    변경하지 않습니다. Operation 성공 ≠ Runtime STOPPED입니다.
                  </p>
                ) : null}
                {lifecyclePhase === 'stop_timeout' ? (
                  <SectionError
                    title="STOP 상태 동기화 대기 시간 초과"
                    message="작업(Operation)은 SUCCEEDED이지만 runtime_status가 STOPPED로 반영되지 않았습니다. 수동 새로고침으로 다시 확인하세요. Operation 성공이 Runtime HEALTHY/STOPPED를 의미하지는 않습니다."
                    onRetry={() => void load('refresh')}
                  />
                ) : null}
                <dl className="meta-grid">
                  <div>
                    <dt>
                      <TermLabel text="작업 ID" apiTerm="Operation ID" />
                    </dt>
                    <dd>
                      <Link
                        className="table-link mono table-cell-clip"
                        to={`/operations/${lastOperation.id}`}
                      >
                        {lastOperation.id}
                      </Link>
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="작업 유형" apiTerm="Type" />
                    </dt>
                    <dd>{lastOperation.operation_type}</dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="진행 상태" apiTerm="Status" />
                    </dt>
                    <dd>
                      <StatusBadge status={lastOperation.status} />
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="현재 단계" apiTerm="Current Step" />
                    </dt>
                    <dd className="mono">
                      {lastOperation.current_step || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="경과 시간" apiTerm="Elapsed" />
                    </dt>
                    <dd>{operationElapsed(lastOperation)}</dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="오류 정보" apiTerm="Error" />
                    </dt>
                    <dd>{formatOperationError(lastOperation.error)}</dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="생성 시각" apiTerm="Created" />
                    </dt>
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
            <div className="detail-panel__header">
              <h2 id="gpu-assignments-heading">할당된 GPU</h2>
              <HelpHint label="할당된 GPU">
                {DEPLOYMENT_FIELD_HELP.gpuAssignments}
              </HelpHint>
            </div>
            <p className="panel-hint">{DEPLOYMENT_FIELD_HELP.gpuAssignments}</p>
            {deployment.gpu_assignments.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">
                        <TermLabel text="할당 순서" apiTerm="Order" />
                      </th>
                      <th scope="col">
                        <TermLabel text="GPU 장치" apiTerm="GPU Device" />
                      </th>
                      <th scope="col">
                        <TermLabel
                          text="예상 GPU 메모리"
                          apiTerm="Expected VRAM"
                          help={DEPLOYMENT_FIELD_HELP.gpuAssignments}
                        />
                      </th>
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
            <p className="panel-hint">
              최신 GPU 상태는{' '}
              <Link className="table-link" to="/nodes">
                서버 및 GPU
              </Link>{' '}
              화면에서 확인하세요.
            </p>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="deployment-config-heading"
          >
            <div className="detail-panel__header">
              <h2 id="deployment-config-heading">모델 배포 실행 설정</h2>
              <HelpHint label="모델 배포 실행 설정">
                {DEPLOYMENT_FIELD_HELP.deploymentConfig}
              </HelpHint>
            </div>
            <p className="panel-hint">{DEPLOYMENT_FIELD_HELP.deploymentConfig}</p>
            <dl className="config-kv-grid">
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
            <p className="panel-hint">
              <Link
                className="table-link"
                to={`/observability/deployments/${deployment.id}`}
              >
                모델 실행·자원 모니터링 보기
              </Link>
              <span className="secondary-text">
                {' '}
                — {DEPLOYMENT_FIELD_HELP.monitoringLink}
              </span>
            </p>
          </section>
        </>
      ) : null}
    </AppShell>
  )
}
