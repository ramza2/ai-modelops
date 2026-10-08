import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  archiveModel,
  archiveModelVersion,
  getDecommissionStatus,
  purgeModelCache,
  removeDeployment,
  retireDeployment,
  unpublishEndpoint,
  type DecommissionStatus,
  type UnpublishResult,
} from '../api/decommission'
import { getDeployment, stopDeployment } from '../api/deployments'
import { getModel, getModelVersion } from '../api/models'
import { getOperation } from '../api/operations'
import type {
  Deployment,
  ModelSummary,
  ModelVersion,
  OperationDetail,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'

type Step =
  | 'inspect'
  | 'unpublish'
  | 'stop'
  | 'remove'
  | 'retire'
  | 'purge'
  | 'archive'
  | 'done'

const STEPS: Step[] = [
  'inspect',
  'unpublish',
  'stop',
  'remove',
  'retire',
  'purge',
  'archive',
  'done',
]

export function DecommissionWizardPage() {
  const { deploymentId = '' } = useParams<{ deploymentId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()

  const stepParam = (searchParams.get('step') as Step | null) || 'inspect'
  const operationIdParam = searchParams.get('operation_id') || ''

  const [step, setStep] = useState<Step>(
    STEPS.includes(stepParam) ? stepParam : 'inspect',
  )
  const [status, setStatus] = useState<DecommissionStatus | null>(null)
  const [deployment, setDeployment] = useState<Deployment | null>(null)
  const [version, setVersion] = useState<ModelVersion | null>(null)
  const [model, setModel] = useState<ModelSummary | null>(null)
  const [operation, setOperation] = useState<OperationDetail | null>(null)
  const [unpublishResult, setUnpublishResult] = useState<UnpublishResult | null>(
    null,
  )
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [purgeConfirm, setPurgeConfirm] = useState('')
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)

  const syncStep = useCallback(
    (next: Step, extra?: { operationId?: string | null }) => {
      setStep(next)
      const params = new URLSearchParams()
      params.set('step', next)
      const opId =
        extra && 'operationId' in extra
          ? extra.operationId
          : operationIdParam || null
      if (opId) params.set('operation_id', opId)
      setSearchParams(params, { replace: true })
    },
    [operationIdParam, setSearchParams],
  )

  const refreshStatus = useCallback(
    async (signal?: AbortSignal) => {
      const [st, dep] = await Promise.all([
        getDecommissionStatus(deploymentId, signal),
        getDeployment(deploymentId, signal),
      ])
      setStatus(st)
      setDeployment(dep)
      setLastUpdated(new Date())
      return st
    },
    [deploymentId],
  )

  useEffect(() => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const st = await refreshStatus(controller.signal)
        if (controller.signal.aborted) return
        const ver = await getModelVersion(
          (await getDeployment(deploymentId, controller.signal)).model_version_id,
          controller.signal,
        )
        if (controller.signal.aborted) return
        setVersion(ver)
        const mod = await getModel(ver.model_id, controller.signal)
        if (controller.signal.aborted) return
        setModel(mod)

        if (operationIdParam) {
          const op = await getOperation(operationIdParam, controller.signal)
          if (controller.signal.aborted) return
          setOperation(op)
        }

        // Auto-skip Unpublish when nothing to unpublish.
        if (
          stepParam === 'unpublish' &&
          st.active_routes.length === 0 &&
          !st.can_unpublish
        ) {
          // keep on unpublish showing "already unpublished"
        }
      } catch (err) {
        if (controller.signal.aborted) return
        setError(
          err instanceof ApiError
            ? err.message
            : 'Failed to load decommission status.',
        )
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    })()
    return () => controller.abort()
  }, [deploymentId, operationIdParam, refreshStatus, stepParam])

  // Poll active Operation.
  useEffect(() => {
    if (!operationIdParam) return
    if (
      operation?.status === 'SUCCEEDED' ||
      operation?.status === 'FAILED' ||
      operation?.status === 'CANCELLED'
    ) {
      return
    }
    const timer = window.setInterval(() => {
      void getOperation(operationIdParam)
        .then((op) => {
          setOperation(op)
          if (op.status === 'SUCCEEDED') {
            void refreshStatus()
          }
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [operationIdParam, operation?.status, refreshStatus])

  const onUnpublish = async () => {
    if (!status?.active_routes[0] || busy) return
    const route = status.active_routes[0]
    setBusy(true)
    setActionError(null)
    try {
      const result = await unpublishEndpoint(route.endpoint_id, {
        expectedDeploymentId: deploymentId,
        reason: 'M7-D decommission unpublish',
        verifyGateway: true,
      })
      setUnpublishResult(result)
      await refreshStatus()
      syncStep('stop')
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Unpublish failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const onStop = async () => {
    if (busy) return
    setBusy(true)
    setActionError(null)
    try {
      const enqueued = await stopDeployment(deploymentId)
      const detail = await getOperation(enqueued.id)
      setOperation(detail)
      syncStep('stop', { operationId: enqueued.id })
      await refreshStatus()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Stop failed.')
    } finally {
      setBusy(false)
    }
  }

  const onRemove = async () => {
    if (busy) return
    setBusy(true)
    setActionError(null)
    try {
      const enqueued = await removeDeployment(deploymentId)
      const detail = await getOperation(enqueued.id)
      setOperation(detail)
      syncStep('remove', { operationId: enqueued.id })
      await refreshStatus()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Remove failed.')
    } finally {
      setBusy(false)
    }
  }

  const onRetire = async () => {
    if (busy) return
    setBusy(true)
    setActionError(null)
    try {
      await retireDeployment(deploymentId)
      await refreshStatus()
      syncStep('purge', { operationId: null })
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Retire failed.')
    } finally {
      setBusy(false)
    }
  }

  const onPurge = async () => {
    if (!status?.source_cache || busy) return
    if (purgeConfirm !== 'PURGE') {
      setActionError('Type PURGE to confirm destructive disk purge.')
      return
    }
    setBusy(true)
    setActionError(null)
    try {
      await purgeModelCache(status.source_cache.cache_id)
      await refreshStatus()
      syncStep('archive')
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Purge failed.')
    } finally {
      setBusy(false)
    }
  }

  const onArchiveVersion = async () => {
    if (!version || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const ver = await archiveModelVersion(version.id)
      setVersion(ver)
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Archive Version failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  const onArchiveModel = async () => {
    if (!model || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const mod = await archiveModel(model.id)
      setModel(mod)
      syncStep('done')
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : 'Archive Model failed.',
      )
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <AppShell
        title="Decommission"
        description="Safe teardown workflow"
        onRefresh={() => undefined}
        refreshing={false}
        lastUpdated={null}
        refreshDisabled
      >
        <LoadingBlock label="Loading decommission status…" />
      </AppShell>
    )
  }

  if (error || !status || !deployment) {
    return (
      <AppShell
        title="Decommission"
        description="Safe teardown workflow"
        onRefresh={() => undefined}
        refreshing={false}
        lastUpdated={null}
        refreshDisabled
      >
        <SectionError
          title="Unable to load"
          message={error || 'Deployment not found.'}
        />
        <Link className="back-link" to={`/deployments/${deploymentId}`}>
          ← Deployment
        </Link>
      </AppShell>
    )
  }

  const route = status.active_routes[0]

  return (
    <AppShell
      title={`Decommission: ${deployment.name}`}
      description="Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠ Purged ≠ Archived"
      lastUpdated={lastUpdated}
      refreshing={refreshing}
      onRefresh={() => {
        setRefreshing(true)
        void refreshStatus().finally(() => setRefreshing(false))
      }}
    >
      <Link className="back-link" to={`/deployments/${deploymentId}`}>
        ← Deployment
      </Link>

      <nav className="wizard-steps" aria-label="Decommission stages">
        {STEPS.map((s) => (
          <button
            key={s}
            type="button"
            className={s === step ? 'btn btn--ghost active' : 'btn btn--ghost'}
            onClick={() => syncStep(s)}
          >
            {s}
          </button>
        ))}
      </nav>

      {actionError ? (
        <SectionError title="Action error" message={actionError} />
      ) : null}

      {step === 'inspect' ? (
        <section className="panel">
          <h2>1. Inspect</h2>
          <dl className="kv">
            <div>
              <dt>Type</dt>
              <dd>{status.deployment_type}</dd>
            </div>
            <div>
              <dt>Desired / Runtime / Health</dt>
              <dd>
                {status.desired_state} / {status.runtime_status} /{' '}
                {status.health_status}
              </dd>
            </div>
            <div>
              <dt>Active routes</dt>
              <dd>
                {status.active_routes.length === 0
                  ? 'None'
                  : status.active_routes
                      .map((r) => `${r.alias} (${r.endpoint_id})`)
                      .join(', ')}
              </dd>
            </div>
            <div>
              <dt>Container present</dt>
              <dd>
                {status.container_present === null
                  ? 'UNKNOWN'
                  : String(status.container_present)}
              </dd>
            </div>
            <div>
              <dt>Source cache</dt>
              <dd className="mono">
                {status.source_cache
                  ? `${status.source_cache.status} ${status.source_cache.local_path || ''}`
                  : '—'}
              </dd>
            </div>
          </dl>
          {status.blockers.length > 0 ? (
            <ul>
              {status.blockers.map((b) => (
                <li key={`${b.code}-${b.message}`}>
                  <code>{b.code}</code>: {b.message}
                </li>
              ))}
            </ul>
          ) : (
            <p className="secondary-text">No blockers reported.</p>
          )}
          <p className="secondary-text">{status.note}</p>
          <button
            type="button"
            className="btn"
            onClick={() =>
              syncStep(status.active_routes.length ? 'unpublish' : 'stop')
            }
          >
            Next
          </button>
        </section>
      ) : null}

      {step === 'unpublish' ? (
        <section className="panel">
          <h2>2. Unpublish</h2>
          {!route ? (
            <>
              <p>No ACTIVE route targets this Deployment.</p>
              <button
                type="button"
                className="btn"
                onClick={() => syncStep('stop')}
              >
                Continue to Stop
              </button>
            </>
          ) : (
            <>
              <p>
                Alias <strong>{route.alias}</strong> currently routes to this
                Deployment. Unpublish deactivates that route only.
              </p>
              {unpublishResult?.gateway_verification ? (
                <p>
                  Last verification:{' '}
                  <StatusBadge
                    status={unpublishResult.gateway_verification.status}
                  />
                </p>
              ) : null}
              <button
                type="button"
                className="btn"
                disabled={busy || !status.can_unpublish}
                onClick={() => void onUnpublish()}
              >
                {busy ? 'Unpublishing…' : 'Unpublish + verify Gateway'}
              </button>
            </>
          )}
        </section>
      ) : null}

      {step === 'stop' ? (
        <section className="panel">
          <h2>3. Stop</h2>
          {status.active_routes.length > 0 ? (
            <SectionError
              title="Active route"
              message="Unpublish first. Guided Stop stays blocked while routed."
            />
          ) : null}
          {operation && operationIdParam ? (
            <p>
              Operation <Link to={`/operations/${operation.id}`}>{operation.id}</Link>{' '}
              <StatusBadge status={operation.status} />
            </p>
          ) : null}
          <button
            type="button"
            className="btn"
            disabled={busy || !status.can_stop}
            onClick={() => void onStop()}
          >
            {busy ? 'Enqueueing…' : 'Enqueue STOP'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={
              status.runtime_status !== 'STOPPED' &&
              operation?.status !== 'SUCCEEDED'
            }
            onClick={() => syncStep('remove', { operationId: null })}
          >
            Next: Remove Container
          </button>
        </section>
      ) : null}

      {step === 'remove' ? (
        <section className="panel">
          <h2>4. Remove Container</h2>
          <p>
            Enqueues DELETE Operation (stop+remove). Metadata is retained until
            Retire.
          </p>
          {operation && operationIdParam ? (
            <p>
              Operation <Link to={`/operations/${operation.id}`}>{operation.id}</Link>{' '}
              <StatusBadge status={operation.status} />
            </p>
          ) : null}
          <p>
            Container present:{' '}
            {status.container_present === null
              ? 'UNKNOWN'
              : String(status.container_present)}
          </p>
          <button
            type="button"
            className="btn"
            disabled={busy || !status.can_remove_container}
            onClick={() => void onRemove()}
          >
            {busy ? 'Enqueueing…' : 'Enqueue REMOVE'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={status.container_present === true}
            onClick={() => syncStep('retire', { operationId: null })}
          >
            Next: Retire
          </button>
        </section>
      ) : null}

      {step === 'retire' ? (
        <section className="panel">
          <h2>5. Retire Deployment</h2>
          <p>
            Sets <code>retired_at</code>. Keeps Operations, GPU assignments, and
            Version references. Does not purge cache.
          </p>
          {status.retired_at ? (
            <p>
              Already retired at <code>{status.retired_at}</code>
            </p>
          ) : null}
          <button
            type="button"
            className="btn"
            disabled={busy || (!status.can_retire && !status.retired_at)}
            onClick={() => void onRetire()}
          >
            {busy ? 'Retiring…' : status.retired_at ? 'Already retired' : 'Retire'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!status.retired_at}
            onClick={() => syncStep('purge')}
          >
            Next: Purge Cache (optional)
          </button>
        </section>
      ) : null}

      {step === 'purge' ? (
        <section className="panel">
          <h2>6. Purge Cache (optional, destructive)</h2>
          <p>
            Retire ≠ Purge. This deletes local model files on the Node. Type{' '}
            <strong>PURGE</strong> to confirm.
          </p>
          {status.source_cache ? (
            <dl className="kv">
              <div>
                <dt>Path</dt>
                <dd className="mono">{status.source_cache.local_path || '—'}</dd>
              </div>
              <div>
                <dt>Revision</dt>
                <dd className="mono">{status.source_cache.revision || '—'}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{status.source_cache.status}</dd>
              </div>
            </dl>
          ) : (
            <p>No source cache linked.</p>
          )}
          <label>
            Confirmation
            <input
              value={purgeConfirm}
              onChange={(e) => setPurgeConfirm(e.target.value)}
              placeholder="PURGE"
            />
          </label>
          <button
            type="button"
            className="btn"
            disabled={busy || !status.can_purge_cache}
            onClick={() => void onPurge()}
          >
            {busy ? 'Purging…' : 'Purge local cache'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => syncStep('archive')}
          >
            Skip / Next: Archive
          </button>
        </section>
      ) : null}

      {step === 'archive' ? (
        <section className="panel">
          <h2>7. Archive Registry (optional)</h2>
          <p>No hard-delete. Version archive keeps history; Model sets is_active=false.</p>
          <p>
            Version: {version?.version_label}{' '}
            {version?.archived_at ? (
              <StatusBadge status="ARCHIVED" />
            ) : (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => void onArchiveVersion()}
              >
                Archive Version
              </button>
            )}
          </p>
          <p>
            Model: {model?.name}{' '}
            {model && !model.is_active ? (
              <StatusBadge status="INACTIVE" />
            ) : (
              <button
                type="button"
                className="btn"
                disabled={busy || !version?.archived_at}
                onClick={() => void onArchiveModel()}
              >
                Archive Model
              </button>
            )}
          </p>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => syncStep('done')}
          >
            Finish
          </button>
        </section>
      ) : null}

      {step === 'done' ? (
        <section className="panel">
          <h2>Done</h2>
          <p>{status.note}</p>
          <ul>
            <li>Routes: {status.active_routes.length === 0 ? 'none' : 'still active'}</li>
            <li>Runtime: {status.runtime_status}</li>
            <li>Retired: {status.retired_at ? 'yes' : 'no'}</li>
            <li>
              Cache:{' '}
              {status.source_cache?.status || 'n/a'}
            </li>
          </ul>
          <button
            type="button"
            className="btn"
            onClick={() => navigate('/deployments')}
          >
            Back to Deployments
          </button>
        </section>
      ) : null}
    </AppShell>
  )
}
