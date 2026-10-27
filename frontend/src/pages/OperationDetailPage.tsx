import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  cancelOperation,
  getOperation,
  retryOperation,
} from '../api/operations'
import type {
  OperationDetail,
  RetryOperationResponse,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, operationElapsed, shortId } from '../utils/date'
import {
  createDetailFetchGate,
  formatOperationError,
  isActiveOperationStatus,
  OPERATION_POLL_MS,
  operationPollConfig,
} from '../utils/operationLifecycle'
import {
  OPERATION_FIELD_HELP,
  formatOperationTypeDisplay,
} from '../utils/uiLabels'

const CANCEL_STATUSES = new Set(['QUEUED', 'RUNNING', 'ROLLING_BACK'])
const RETRY_STATUSES = new Set(['FAILED', 'ROLLED_BACK'])
const SWITCH_STRATEGIES = new Set(['HOT', 'COLD'])

type MutationKind = 'cancel' | 'retry'

function canSafeCancel(op: OperationDetail): boolean {
  return (
    op.operation_type === 'SWITCH' &&
    SWITCH_STRATEGIES.has(op.switch_strategy ?? '') &&
    CANCEL_STATUSES.has(op.status)
  )
}

function canRetryGate(op: OperationDetail): boolean {
  return (
    op.operation_type === 'SWITCH' &&
    SWITCH_STRATEGIES.has(op.switch_strategy ?? '') &&
    RETRY_STATUSES.has(op.status)
  )
}

export function OperationDetailPage() {
  const { operationId = '' } = useParams<{ operationId: string }>()

  const [operation, setOperation] = useState<OperationDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const [mutating, setMutating] = useState<MutationKind | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [cancelReason, setCancelReason] = useState('')
  const [retryChild, setRetryChild] = useState<RetryOperationResponse | null>(
    null,
  )

  const fetchGateRef = useRef(createDetailFetchGate())
  /** Latest Operation ID the page is authorized to display / mutate. */
  const activeIdRef = useRef(operationId)
  activeIdRef.current = operationId
  const refreshingRef = useRef(false)
  refreshingRef.current = refreshing
  const mutatingRef = useRef<MutationKind | null>(null)
  mutatingRef.current = mutating

  const resetIdentityState = useCallback(() => {
    setOperation(null)
    setRetryChild(null)
    setActionError(null)
    setCancelReason('')
    setMutating(null)
    setError(null)
    setNotFound(false)
    setLastUpdated(null)
    setRefreshing(false)
  }, [])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const requestId = operationId
      if (!requestId) {
        resetIdentityState()
        setNotFound(true)
        setLoading(false)
        setError('Operation을 찾을 수 없습니다.')
        return
      }

      const gate = fetchGateRef.current
      const { gen, signal } = gate.begin('load')

      if (mode === 'initial') {
        // New identity (or first mount): never keep prior Operation as stale.
        resetIdentityState()
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)
      setNotFound(false)

      const isCurrent = () =>
        gate.isCurrent(gen) && activeIdRef.current === requestId

      try {
        const data = await getOperation(requestId, signal)
        if (!isCurrent()) return
        // Reject mismatched payload for the requested identity.
        if (data.id !== requestId) return
        setOperation(data)
        setNotFound(false)
        setLastUpdated(new Date())
      } catch (err) {
        if (!isCurrent()) {
          // Superseded by a newer load/poll — drop spinner if poll won.
          if (
            activeIdRef.current === requestId &&
            gate.currentKind() === 'poll'
          ) {
            setLoading(false)
            setRefreshing(false)
          }
          return
        }
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (err instanceof ApiError && err.status === 404) {
          setOperation(null)
          setRetryChild(null)
          setNotFound(true)
          setError('Operation을 찾을 수 없습니다.')
          return
        }
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Operation을 불러오지 못했습니다.'
        setError(message)
      } finally {
        if (isCurrent()) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [operationId, resetIdentityState],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      fetchGateRef.current.abort()
    }
  }, [load])

  // Silent 4s polling while Operation is in-flight. Does not toggle
  // refreshing (keeps Cancel reason / action buttons usable). Shares a
  // fetch epoch with manual refresh so reversed responses cannot win.
  useEffect(() => {
    const requestId = operationId
    if (!requestId) return
    if (!operation || operation.id !== requestId) return
    if (!isActiveOperationStatus(operation.status)) return

    let cancelled = false
    const timer = window.setInterval(() => {
      // Do not preempt an in-flight manual refresh / mutation.
      if (refreshingRef.current || mutatingRef.current) return

      const gate = fetchGateRef.current
      const { gen, signal } = gate.begin('poll')
      void getOperation(requestId, signal)
        .then((data) => {
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeIdRef.current !== requestId ||
            data.id !== requestId
          ) {
            return
          }
          setOperation(data)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (
            cancelled ||
            !gate.isCurrent(gen) ||
            activeIdRef.current !== requestId
          ) {
            return
          }
          if (err instanceof DOMException && err.name === 'AbortError') return
          // Keep last known Operation + user input (cancel reason).
        })
    }, operationPollConfig.ms)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      // Abort only an in-flight poll owned by this effect; a newer load
      // begin() already invalidated this epoch via the shared gate.
      if (fetchGateRef.current.currentKind() === 'poll') {
        fetchGateRef.current.abort()
      }
    }
  }, [operationId, operation?.id, operation?.status])

  const runCancel = async () => {
    if (
      !operationId ||
      mutating ||
      refreshing ||
      !operation ||
      operation.id !== operationId ||
      !canSafeCancel(operation)
    ) {
      return
    }
    const requestId = operationId
    setMutating('cancel')
    setActionError(null)
    try {
      await cancelOperation(requestId, cancelReason)
      if (activeIdRef.current !== requestId) return
      await load('refresh')
    } catch (err) {
      if (activeIdRef.current !== requestId) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Cancel 요청에 실패했습니다.'
      setActionError(message)
    } finally {
      if (activeIdRef.current === requestId) {
        setMutating(null)
      }
    }
  }

  const runRetry = async () => {
    if (
      !operationId ||
      mutating ||
      refreshing ||
      !operation ||
      operation.id !== operationId ||
      !canRetryGate(operation)
    ) {
      return
    }
    const requestId = operationId
    setMutating('retry')
    setActionError(null)
    try {
      const child = await retryOperation(requestId)
      if (activeIdRef.current !== requestId) return
      setRetryChild(child)
      await load('refresh')
    } catch (err) {
      if (activeIdRef.current !== requestId) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Retry 요청에 실패했습니다.'
      setActionError(message)
    } finally {
      if (activeIdRef.current === requestId) {
        setMutating(null)
      }
    }
  }

  // Only render Operation data that matches the current URL identity.
  const displayOperation =
    operation && operation.id === operationId ? operation : null

  if (notFound) {
    return (
      <AppShell
        title="작업 이력"
        description="작업 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/operations">
          ← 작업 이력
        </Link>
        <SectionError
          title="작업을 찾을 수 없습니다."
          message="요청한 Operation이 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const busy = mutating !== null || refreshing
  const showCancel = displayOperation
    ? canSafeCancel(displayOperation)
    : false
  const showRetry = displayOperation ? canRetryGate(displayOperation) : false
  const steps = displayOperation?.steps ?? []
  const polling = Boolean(
    displayOperation && isActiveOperationStatus(displayOperation.status),
  )

  return (
    <AppShell
      title={
        displayOperation
          ? `작업 ${shortId(displayOperation.id, 12)}`
          : '작업 상세'
      }
      description="작업 진행 상태와 Step 이력입니다. Switch Safe Cancel / Explicit Retry만 제공하며, metadata·step detail JSON은 표시하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={mutating !== null}
    >
      <Link className="back-link" to="/operations">
        ← 작업 이력
      </Link>

      <p className="panel-hint" role="note">
        {OPERATION_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="진행 상태">
            {OPERATION_FIELD_HELP.status}
          </HelpHint>
          <HelpHint showTopic label="안전 취소 (Safe Cancel)">
            {OPERATION_FIELD_HELP.safeCancel}
          </HelpHint>
          <HelpHint showTopic label="명시적 재시도 (Explicit Retry)">
            {OPERATION_FIELD_HELP.explicitRetry}
          </HelpHint>
          <HelpHint showTopic label="기존/변경 대상">
            {OPERATION_FIELD_HELP.sourceTarget}
          </HelpHint>
        </div>
      </details>

      {loading && !displayOperation ? (
        <LoadingBlock label="작업을 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={displayOperation ? '작업 새로고침 실패' : '작업 오류'}
          message={
            displayOperation
              ? `기존 Operation 정보를 표시하고 있습니다. 새로고침 실패: ${error}`
              : error
          }
        />
      ) : null}

      {displayOperation ? (
        <>
          <section
            className="detail-panel"
            aria-labelledby="operation-identity-heading"
          >
            <div className="detail-panel__header">
              <h2 id="operation-identity-heading" className="mono">
                {displayOperation.id}
              </h2>
              <StatusBadge status={displayOperation.status} />
            </div>
            {polling ? (
              <p className="panel-hint" role="status">
                진행 중 — {OPERATION_POLL_MS / 1000}초마다 자동 갱신합니다.
                수동 새로고침과 Cancel reason 입력은 유지됩니다.
              </p>
            ) : null}
            <dl className="meta-grid">
              <div>
                <dt>
                  <TermLabel
                    text="작업 유형"
                    apiTerm="Type"
                    help={OPERATION_FIELD_HELP.type}
                  />
                </dt>
                <dd>
                  {formatOperationTypeDisplay(displayOperation.operation_type)}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="전환 방식"
                    apiTerm="Strategy"
                    help={OPERATION_FIELD_HELP.strategy}
                  />
                </dt>
                <dd>{displayOperation.switch_strategy || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="현재 작업 단계" apiTerm="Current Step" />
                </dt>
                <dd className="mono">
                  {displayOperation.current_step || '—'}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="경과 시간" apiTerm="Elapsed" />
                </dt>
                <dd>{operationElapsed(displayOperation)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="요청자" apiTerm="Requested By" />
                </dt>
                <dd>{displayOperation.requested_by || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="요청 사유" apiTerm="Request Reason" />
                </dt>
                <dd>{displayOperation.request_reason || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="취소 요청 시각"
                    apiTerm="Cancel Requested"
                  />
                </dt>
                <dd>
                  {formatApiDateTime(displayOperation.cancel_requested_at)}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="오류 정보" apiTerm="Error" />
                </dt>
                <dd>{formatOperationError(displayOperation.error)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="생성 시각" apiTerm="Created" />
                </dt>
                <dd>{formatApiDateTime(displayOperation.created_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="시작 시각" apiTerm="Started" />
                </dt>
                <dd>{formatApiDateTime(displayOperation.started_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="종료 시각" apiTerm="Finished" />
                </dt>
                <dd>{formatApiDateTime(displayOperation.finished_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="API 별칭" apiTerm="Endpoint" />
                </dt>
                <dd>
                  {displayOperation.endpoint_alias_id ? (
                    <Link
                      className="table-link mono"
                      to={`/endpoints/${displayOperation.endpoint_alias_id}`}
                    >
                      {shortId(displayOperation.endpoint_alias_id, 12)}
                    </Link>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="기존 대상"
                    apiTerm="Source"
                    help={OPERATION_FIELD_HELP.sourceTarget}
                  />
                </dt>
                <dd>
                  {displayOperation.source_deployment_id ? (
                    <Link
                      className="table-link mono"
                      to={`/deployments/${displayOperation.source_deployment_id}`}
                    >
                      {shortId(displayOperation.source_deployment_id, 12)}
                    </Link>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="변경 대상"
                    apiTerm="Target"
                    help={OPERATION_FIELD_HELP.sourceTarget}
                  />
                </dt>
                <dd>
                  {displayOperation.target_deployment_id ? (
                    <Link
                      className="table-link mono"
                      to={`/deployments/${displayOperation.target_deployment_id}`}
                    >
                      {shortId(displayOperation.target_deployment_id, 12)}
                    </Link>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="재시도 원본" apiTerm="Retry Of" />
                </dt>
                <dd>
                  {displayOperation.retry_of_operation_id ? (
                    <Link
                      className="table-link mono"
                      to={`/operations/${displayOperation.retry_of_operation_id}`}
                    >
                      {shortId(displayOperation.retry_of_operation_id, 12)}
                    </Link>
                  ) : (
                    '—'
                  )}
                </dd>
              </div>
            </dl>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="operation-actions-heading"
          >
            <h2 id="operation-actions-heading">작업 조작 (Actions)</h2>
            <p className="panel-hint" role="note">
              {OPERATION_FIELD_HELP.safeCancel}
            </p>
            <p className="panel-hint" role="note">
              {OPERATION_FIELD_HELP.explicitRetry}
            </p>
            <p className="panel-hint">
              Safe Cancel은 HOT/COLD SWITCH의 QUEUED/RUNNING/ROLLING_BACK에서만
              표시됩니다. Explicit Retry는 FAILED/ROLLED_BACK SWITCH의 거친 UI
              gate이며, 실제 가능 여부는 Backend가 판정합니다. Retry는 새
              Operation을 만들고 unique Idempotency-Key를 사용합니다.
            </p>

            {!showCancel && !showRetry ? (
              <p className="metric-line" role="status">
                현재 상태에서는 Cancel/Retry action을 사용할 수 없습니다.
              </p>
            ) : null}

            {showCancel ? (
              <div className="toolbar toolbar--wrap">
                <label className="toolbar__field" htmlFor="cancel-reason">
                  <span>취소 사유 Cancel Reason (optional)</span>
                  <input
                    id="cancel-reason"
                    type="text"
                    value={cancelReason}
                    disabled={busy}
                    maxLength={2000}
                    onChange={(e) => setCancelReason(e.target.value)}
                    placeholder="operator note"
                  />
                </label>
                <div className="toolbar__actions">
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    aria-busy={mutating === 'cancel'}
                    onClick={() => void runCancel()}
                  >
                    {mutating === 'cancel'
                      ? 'Cancel 중…'
                      : '안전 취소 (Safe Cancel)'}
                  </button>
                </div>
              </div>
            ) : null}

            {showRetry ? (
              <div className="action-row">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy}
                  aria-busy={mutating === 'retry'}
                  onClick={() => void runRetry()}
                >
                  {mutating === 'retry'
                    ? 'Retry 중…'
                    : '명시적 재시도 (Explicit Retry)'}
                </button>
              </div>
            ) : null}

            {actionError ? (
              <SectionError title="Action 실패" message={actionError} />
            ) : null}

            {retryChild ? (
              <div className="operation-result" role="status">
                <h3 className="section-subheading">
                  생성된 재시도 작업 (Retry Operation)
                </h3>
                <dl className="meta-grid">
                  <div>
                    <dt>
                      <TermLabel text="새 작업" apiTerm="New Operation" />
                    </dt>
                    <dd>
                      <Link
                        className="table-link mono"
                        to={`/operations/${retryChild.id}`}
                      >
                        {retryChild.id}
                      </Link>
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="진행 상태" apiTerm="Status" />
                    </dt>
                    <dd>
                      <StatusBadge status={retryChild.status} />
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="전환 방식" apiTerm="Strategy" />
                    </dt>
                    <dd>{retryChild.switch_strategy || '—'}</dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="재시도 원본" apiTerm="Retry Of" />
                    </dt>
                    <dd className="mono">
                      {retryChild.retry_of_operation_id || displayOperation.id}
                    </dd>
                  </div>
                </dl>
              </div>
            ) : null}
          </section>

          <section
            className="detail-panel"
            aria-labelledby="operation-steps-heading"
          >
            <h2 id="operation-steps-heading">작업 단계 (Steps)</h2>
            <p className="panel-hint">
              상세 응답에 포함된 순서대로의 Step입니다. Step detail JSON은
              렌더링하지 않습니다. step_code 원본은 그대로 표시합니다.
            </p>
            {steps.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">
                        <TermLabel text="순서" apiTerm="Seq" />
                      </th>
                      <th scope="col">
                        <TermLabel text="단계" apiTerm="Step" />
                      </th>
                      <th scope="col">
                        <TermLabel text="시도" apiTerm="Attempt" />
                      </th>
                      <th scope="col">
                        <TermLabel text="진행 상태" apiTerm="Status" />
                      </th>
                      <th scope="col">
                        <TermLabel text="시작 시각" apiTerm="Started" />
                      </th>
                      <th scope="col">
                        <TermLabel text="종료 시각" apiTerm="Finished" />
                      </th>
                      <th scope="col">
                        <TermLabel text="오류 정보" apiTerm="Error" />
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...steps]
                      .sort((a, b) => a.sequence_no - b.sequence_no)
                      .map((step) => (
                        <tr key={step.id}>
                          <td>{step.sequence_no}</td>
                          <td className="mono">{step.step_code}</td>
                          <td>{step.attempt_no}</td>
                          <td>
                            <StatusBadge status={step.status} />
                          </td>
                          <td>{formatApiDateTime(step.started_at)}</td>
                          <td>{formatApiDateTime(step.finished_at)}</td>
                          <td>{formatOperationError(step.error)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty-state" role="status">
                Step이 없습니다.
              </p>
            )}
          </section>
        </>
      ) : null}
    </AppShell>
  )
}
