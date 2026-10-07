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
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'

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

function formatError(
  error: { code: string | null; message: string | null } | null | undefined,
): string {
  if (!error) return '—'
  const parts = [error.code, error.message].filter(
    (p): p is string => Boolean(p && p.trim()),
  )
  return parts.length > 0 ? parts.join(': ') : '—'
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

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  /** Latest Operation ID the page is authorized to display / mutate. */
  const activeIdRef = useRef(operationId)
  activeIdRef.current = operationId

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

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

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
        !controller.signal.aborted &&
        gen === readGenRef.current &&
        activeIdRef.current === requestId

      try {
        const data = await getOperation(requestId, controller.signal)
        if (!isCurrent()) return
        // Reject mismatched payload for the requested identity.
        if (data.id !== requestId) return
        setOperation(data)
        setNotFound(false)
        setLastUpdated(new Date())
      } catch (err) {
        if (!isCurrent()) return
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
      abortRef.current?.abort()
    }
  }, [load])

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
        title="Operations"
        description="Operation 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/operations">
          ← Operations
        </Link>
        <SectionError
          title="Operation을 찾을 수 없습니다."
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

  return (
    <AppShell
      title={
        displayOperation
          ? `Operation ${shortId(displayOperation.id, 12)}`
          : 'Operation 상세'
      }
      description="Operation 진행 상태와 Step 이력입니다. Switch Safe Cancel / Explicit Retry만 제공하며, metadata·step detail JSON은 표시하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={mutating !== null}
    >
      <Link className="back-link" to="/operations">
        ← Operations
      </Link>

      {loading && !displayOperation ? (
        <LoadingBlock label="Operation을 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={
            displayOperation ? 'Operation 새로고침 실패' : 'Operation 오류'
          }
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
            <dl className="meta-grid">
              <div>
                <dt>Type</dt>
                <dd>{displayOperation.operation_type || '—'}</dd>
              </div>
              <div>
                <dt>Strategy</dt>
                <dd>{displayOperation.switch_strategy || '—'}</dd>
              </div>
              <div>
                <dt>Current Step</dt>
                <dd className="mono">
                  {displayOperation.current_step || '—'}
                </dd>
              </div>
              <div>
                <dt>Requested By</dt>
                <dd>{displayOperation.requested_by || '—'}</dd>
              </div>
              <div>
                <dt>Request Reason</dt>
                <dd>{displayOperation.request_reason || '—'}</dd>
              </div>
              <div>
                <dt>Cancel Requested</dt>
                <dd>
                  {formatApiDateTime(displayOperation.cancel_requested_at)}
                </dd>
              </div>
              <div>
                <dt>Error</dt>
                <dd>{formatError(displayOperation.error)}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{formatApiDateTime(displayOperation.created_at)}</dd>
              </div>
              <div>
                <dt>Started</dt>
                <dd>{formatApiDateTime(displayOperation.started_at)}</dd>
              </div>
              <div>
                <dt>Finished</dt>
                <dd>{formatApiDateTime(displayOperation.finished_at)}</dd>
              </div>
              <div>
                <dt>Endpoint</dt>
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
                <dt>Source Deployment</dt>
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
                <dt>Target Deployment</dt>
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
                <dt>Retry Of</dt>
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
            <h2 id="operation-actions-heading">Actions</h2>
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
                  <span>Cancel Reason (optional)</span>
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
                    {mutating === 'cancel' ? 'Cancel 중…' : 'Safe Cancel'}
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
                  {mutating === 'retry' ? 'Retry 중…' : 'Explicit Retry'}
                </button>
              </div>
            ) : null}

            {actionError ? (
              <SectionError title="Action 실패" message={actionError} />
            ) : null}

            {retryChild ? (
              <div className="operation-result" role="status">
                <h3 className="section-subheading">생성된 Retry Operation</h3>
                <dl className="meta-grid">
                  <div>
                    <dt>New Operation</dt>
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
                    <dt>Status</dt>
                    <dd>
                      <StatusBadge status={retryChild.status} />
                    </dd>
                  </div>
                  <div>
                    <dt>Strategy</dt>
                    <dd>{retryChild.switch_strategy || '—'}</dd>
                  </div>
                  <div>
                    <dt>Retry Of</dt>
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
            <h2 id="operation-steps-heading">Steps</h2>
            <p className="panel-hint">
              상세 응답에 포함된 순서대로의 Step입니다. Step detail JSON은
              렌더링하지 않습니다.
            </p>
            {steps.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Seq</th>
                      <th scope="col">Step</th>
                      <th scope="col">Attempt</th>
                      <th scope="col">Status</th>
                      <th scope="col">Started</th>
                      <th scope="col">Finished</th>
                      <th scope="col">Error</th>
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
                          <td>{formatError(step.error)}</td>
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
