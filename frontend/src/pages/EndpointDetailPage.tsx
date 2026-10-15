import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  unpublishEndpoint,
  type UnpublishResult,
} from '../api/decommission'
import { listDeployments } from '../api/deployments'
import {
  createPreflightPreview,
  getEndpoint,
  listEndpointRoutes,
  switchEndpoint,
} from '../api/endpoints'
import type {
  Deployment,
  Endpoint,
  EndpointRoute,
  PreflightPreview,
  SwitchOperation,
} from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { formatMemoryMb } from '../utils/number'

type SwitchStrategy = 'HOT' | 'COLD'

/** Management API max page_size for Deployment list. */
const TARGET_PAGE_SIZE = 200

const UNPUBLISH_ROUTE_CONFLICT_CODES = new Set([
  'ROUTE_TARGET_CHANGED',
  'ROUTE_MUTATION_BUSY',
])

function switchWorkflowBlockedReason(endpoint: Endpoint): string | null {
  if (!endpoint.is_enabled) {
    return 'Disabled Endpoint에서는 Switch를 사용할 수 없습니다.'
  }
  if (!endpoint.active_route?.deployment_id) {
    return 'ACTIVE Source Route가 있어야 Switch를 사용할 수 있습니다.'
  }
  if (endpoint.traffic_state !== 'SERVING') {
    return `traffic_state가 ${endpoint.traffic_state}입니다. SERVING일 때만 Switch enqueue를 사용할 수 있습니다 (DRAINING/MAINTENANCE 불가).`
  }
  return null
}

/** Gateway verification is separate from Backend Route release. */
function gatewayVerificationPassed(status: string | null | undefined): boolean {
  return (status || '').toUpperCase() === 'PASSED'
}

function gatewayVerificationHint(status: string | null | undefined): string {
  const s = (status || '').toUpperCase()
  if (s === 'PASSED') {
    return 'Gateway 검증 통과 (PASSED).'
  }
  if (s === 'ROUTING_PENDING') {
    return 'Gateway 라우팅 반영이 아직 대기 중입니다 (ROUTING_PENDING). Backend Route 해제와는 별개이며 검증 통과로 보지 마세요.'
  }
  if (s === 'ROUTE_STILL_ACTIVE') {
    return 'Gateway에 Route가 아직 활성으로 보입니다 (ROUTE_STILL_ACTIVE). Backend 해제와 Gateway 검증은 별개입니다.'
  }
  if (s === 'GATEWAY_UNAVAILABLE') {
    return 'Gateway 검증을 수행할 수 없습니다 (GATEWAY_UNAVAILABLE). 성공으로 오인하지 마세요.'
  }
  if (s === 'SKIPPED') {
    return 'Gateway 검증이 건너뛰어졌습니다 (SKIPPED). 검증 통과로 보지 마세요.'
  }
  if (!s) {
    return 'Gateway verification 상태가 없습니다.'
  }
  return `Gateway verification.status=${s}. PASSED만 검증 통과로 표시합니다.`
}

export function EndpointDetailPage() {
  const { endpointId = '' } = useParams<{ endpointId: string }>()

  const [endpoint, setEndpoint] = useState<Endpoint | null>(null)
  const [routes, setRoutes] = useState<EndpointRoute[] | null>(null)

  const [loading, setLoading] = useState(true)
  const [routesLoading, setRoutesLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [routesError, setRoutesError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const [targets, setTargets] = useState<Deployment[] | null>(null)
  const [targetsError, setTargetsError] = useState<string | null>(null)
  const [targetsLoading, setTargetsLoading] = useState(false)
  const [targetId, setTargetId] = useState('')

  const [preview, setPreview] = useState<PreflightPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const [strategy, setStrategy] = useState<SwitchStrategy | ''>('')
  const [reason, setReason] = useState('')
  const [switching, setSwitching] = useState(false)
  const [switchError, setSwitchError] = useState<string | null>(null)
  const [lastOperation, setLastOperation] = useState<SwitchOperation | null>(
    null,
  )

  const [unpublishOpen, setUnpublishOpen] = useState(false)
  const [unpublishAliasInput, setUnpublishAliasInput] = useState('')
  const [unpublishExpectedDepId, setUnpublishExpectedDepId] = useState<
    string | null
  >(null)
  const [unpublishing, setUnpublishing] = useState(false)
  const [unpublishError, setUnpublishError] = useState<string | null>(null)
  const [unpublishResult, setUnpublishResult] =
    useState<UnpublishResult | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const unpublishAbortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const targetsAbortRef = useRef<AbortController | null>(null)
  const activeEndpointIdRef = useRef(endpointId)
  activeEndpointIdRef.current = endpointId

  const switchBlockedReason = endpoint
    ? switchWorkflowBlockedReason(endpoint)
    : null
  const canSwitchWorkflow = switchBlockedReason === null

  const closeUnpublishDialog = useCallback(() => {
    setUnpublishOpen(false)
    setUnpublishAliasInput('')
    setUnpublishExpectedDepId(null)
  }, [])

  const invalidatePreview = useCallback(() => {
    setPreview(null)
    setPreviewError(null)
    setStrategy('')
    setSwitchError(null)
  }, [])

  const loadTargets = useCallback(
    async (sourceId: string | null, gen: number) => {
      targetsAbortRef.current?.abort()
      const controller = new AbortController()
      targetsAbortRef.current = controller
      setTargetsLoading(true)
      setTargetsError(null)
      try {
        const collected: Deployment[] = []
        let page = 1
        let maxPage = 1
        do {
          const result = await listDeployments({
            retired: false,
            page,
            pageSize: TARGET_PAGE_SIZE,
            signal: controller.signal,
          })
          if (controller.signal.aborted || gen !== readGenRef.current) return
          collected.push(...result.items)
          maxPage = Math.max(
            1,
            Math.ceil(result.total / TARGET_PAGE_SIZE) || 1,
          )
          if (
            result.items.length === 0 ||
            result.items.length < TARGET_PAGE_SIZE
          ) {
            break
          }
          page += 1
        } while (page <= maxPage)

        if (controller.signal.aborted || gen !== readGenRef.current) return
        setTargets(collected.filter((d) => d.id !== sourceId))
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Target Deployment 목록을 불러오지 못했습니다.'
        setTargetsError(message)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setTargetsLoading(false)
        }
      }
    },
    [],
  )

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!endpointId) {
        setNotFound(true)
        setLoading(false)
        setRoutesLoading(false)
        setError('Endpoint를 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial') {
        setLoading(true)
        setRoutesLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)
      setRoutesError(null)
      setNotFound(false)

      const endpointPromise = getEndpoint(endpointId, controller.signal)
        .then(async (data) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          // Successful re-read invalidates prior preview (including post-switch).
          invalidatePreview()
          setEndpoint(data)
          setNotFound(false)
          setLastUpdated(new Date())
          if (
            data.is_enabled &&
            data.active_route?.deployment_id &&
            data.traffic_state === 'SERVING'
          ) {
            await loadTargets(data.active_route.deployment_id, gen)
          } else {
            setTargets(null)
          }
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            setEndpoint(null)
            setRoutes(null)
            setTargets(null)
            setPreview(null)
            setStrategy('')
            setNotFound(true)
            setError('Endpoint를 찾을 수 없습니다.')
            return
          }
          // Non-404: keep stale Endpoint + visible preview.
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Endpoint를 불러오지 못했습니다.'
          setError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setLoading(false)
          }
        })

      const routesPromise = listEndpointRoutes(endpointId, controller.signal)
        .then((result) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          setRoutes(result.items)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Route 이력을 불러오지 못했습니다.'
          setRoutesError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setRoutesLoading(false)
          }
        })

      await Promise.all([endpointPromise, routesPromise])
      if (!controller.signal.aborted && gen === readGenRef.current) {
        setRefreshing(false)
      }
    },
    [endpointId, loadTargets, invalidatePreview],
  )

  useEffect(() => {
    closeUnpublishDialog()
    setUnpublishResult(null)
    setUnpublishError(null)
    void load('initial')
    return () => {
      abortRef.current?.abort()
      targetsAbortRef.current?.abort()
      unpublishAbortRef.current?.abort()
    }
  }, [load, closeUnpublishDialog])

  // Close confirm dialog if ACTIVE Route disappears or its Deployment changes.
  useEffect(() => {
    if (!unpublishOpen) return
    const currentDep = endpoint?.active_route?.deployment_id ?? null
    if (currentDep !== unpublishExpectedDepId) {
      closeUnpublishDialog()
    }
  }, [
    endpoint?.active_route?.deployment_id,
    unpublishOpen,
    unpublishExpectedDepId,
    closeUnpublishDialog,
  ])

  const onTargetChange = (nextId: string) => {
    setTargetId(nextId)
    invalidatePreview()
  }

  const openUnpublishDialog = () => {
    if (!endpoint?.active_route?.deployment_id || unpublishing || switching) {
      return
    }
    setUnpublishExpectedDepId(endpoint.active_route.deployment_id)
    setUnpublishAliasInput('')
    setUnpublishError(null)
    setUnpublishOpen(true)
  }

  const confirmUnpublish = async () => {
    if (!endpointId || unpublishing || switching || previewing || refreshing) {
      return
    }
    if (!endpoint) {
      setUnpublishError('Endpoint를 불러온 뒤 다시 시도하세요.')
      closeUnpublishDialog()
      return
    }
    const active = endpoint.active_route
    if (!active?.deployment_id || !unpublishExpectedDepId) {
      setUnpublishError('ACTIVE Route가 없습니다. 재조회 후 다시 시도하세요.')
      closeUnpublishDialog()
      await load('refresh')
      return
    }
    if (unpublishAliasInput !== endpoint.alias) {
      setUnpublishError('입력한 Alias가 현재 Endpoint Alias와 일치하지 않습니다.')
      return
    }
    if (active.deployment_id !== unpublishExpectedDepId) {
      setUnpublishError(
        '확인 중 ACTIVE Route가 변경되었습니다. 실행을 차단하고 재조회합니다.',
      )
      closeUnpublishDialog()
      await load('refresh')
      return
    }

    unpublishAbortRef.current?.abort()
    const controller = new AbortController()
    unpublishAbortRef.current = controller
    const gen = readGenRef.current
    const epId = endpointId
    const expectedDep = unpublishExpectedDepId

    setUnpublishing(true)
    setUnpublishError(null)
    try {
      const result = await unpublishEndpoint(
        epId,
        {
          expectedDeploymentId: expectedDep,
          verifyGateway: true,
          reason: 'Endpoint detail unpublish',
        },
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        gen !== readGenRef.current ||
        activeEndpointIdRef.current !== epId
      ) {
        return
      }
      setUnpublishResult(result)
      closeUnpublishDialog()
      await load('refresh')
    } catch (err) {
      if (
        controller.signal.aborted ||
        gen !== readGenRef.current ||
        activeEndpointIdRef.current !== epId
      ) {
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') return

      if (
        err instanceof ApiError &&
        err.status === 409 &&
        err.code &&
        UNPUBLISH_ROUTE_CONFLICT_CODES.has(err.code)
      ) {
        setUnpublishError(
          `Route 충돌 (${err.code}): 현재 Route가 바뀌었을 수 있습니다. 재조회합니다.`,
        )
        closeUnpublishDialog()
        await load('refresh')
        return
      }

      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Unpublish 요청에 실패했습니다.'
      // Do not invent success/failure of route release — re-read authoritative state.
      setUnpublishError(
        `${message} 실행 결과를 추정하지 않습니다. Endpoint/Route를 재조회합니다.`,
      )
      closeUnpublishDialog()
      await load('refresh')
    } finally {
      if (
        !controller.signal.aborted &&
        gen === readGenRef.current &&
        activeEndpointIdRef.current === epId
      ) {
        setUnpublishing(false)
      }
    }
  }

  const runPreview = async () => {
    if (
      !endpointId ||
      !targetId ||
      switching ||
      unpublishing ||
      !canSwitchWorkflow
    ) {
      return
    }
    setPreviewing(true)
    setPreviewError(null)
    setSwitchError(null)
    try {
      const result = await createPreflightPreview(endpointId, targetId)
      setPreview(result)
      if (result.result === 'HOT_SWITCH_AVAILABLE') {
        setStrategy('HOT')
      } else if (result.result === 'COLD_SWITCH_ONLY') {
        setStrategy('COLD')
      } else {
        setStrategy('')
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Preflight preview에 실패했습니다.'
      setPreviewError(message)
      setPreview(null)
      setStrategy('')
    } finally {
      setPreviewing(false)
    }
  }

  const runSwitch = async () => {
    if (!endpointId || !targetId || !strategy || switching || unpublishing) {
      return
    }
    if (!canSwitchWorkflow || !preview) return
    if (preview.result === 'RESOURCE_INSUFFICIENT') return
    if (strategy === 'HOT' && preview.result !== 'HOT_SWITCH_AVAILABLE') return
    if (
      strategy === 'COLD' &&
      preview.result !== 'HOT_SWITCH_AVAILABLE' &&
      preview.result !== 'COLD_SWITCH_ONLY'
    ) {
      return
    }

    setSwitching(true)
    setSwitchError(null)
    try {
      const op = await switchEndpoint(endpointId, {
        targetDeploymentId: targetId,
        strategy,
        reason,
      })
      setLastOperation(op)
      await load('refresh')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Switch Operation enqueue에 실패했습니다.'
      setSwitchError(message)
    } finally {
      setSwitching(false)
    }
  }

  if (notFound) {
    return (
      <AppShell
        title="Endpoints"
        description="Endpoint 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/endpoints">
          ← Endpoints
        </Link>
        <SectionError
          title="Endpoint를 찾을 수 없습니다."
          message="요청한 Endpoint가 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const busy = switching || unpublishing
  const hotAllowed = preview?.result === 'HOT_SWITCH_AVAILABLE'
  const coldAllowed =
    preview?.result === 'HOT_SWITCH_AVAILABLE' ||
    preview?.result === 'COLD_SWITCH_ONLY'
  const switchAllowed =
    Boolean(preview) &&
    Boolean(strategy) &&
    preview?.result !== 'RESOURCE_INSUFFICIENT' &&
    ((strategy === 'HOT' && hotAllowed) || (strategy === 'COLD' && coldAllowed))

  const active = endpoint?.active_route

  return (
    <AppShell
      title={endpoint?.display_name ?? endpoint?.alias ?? 'Endpoint 상세'}
      description="Endpoint Alias, ACTIVE Route, Route 이력과 Preflight preview 기반 HOT/COLD Switch enqueue입니다. Preview는 분석 전용이며 Worker가 실행 직전 fresh Preflight를 다시 수행합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={busy || previewing}
    >
      <Link className="back-link" to="/endpoints">
        ← Endpoints
      </Link>

      {loading && !endpoint ? (
        <LoadingBlock label="Endpoint를 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={endpoint ? 'Endpoint 새로고침 실패' : 'Endpoint 오류'}
          message={
            endpoint
              ? `기존 Endpoint 정보를 표시하고 있습니다. 새로고침 실패: ${error}`
              : error
          }
        />
      ) : null}

      {endpoint ? (
        <>
          <section
            className="detail-panel"
            aria-labelledby="endpoint-identity-heading"
          >
            <div className="detail-panel__header">
              <h2 id="endpoint-identity-heading">{endpoint.display_name}</h2>
              <ActiveBadge active={endpoint.is_enabled} />
            </div>
            <dl className="meta-grid">
              <div>
                <dt>Alias</dt>
                <dd className="mono">{endpoint.alias}</dd>
              </div>
              <div>
                <dt>API Type</dt>
                <dd>{endpoint.api_type || '—'}</dd>
              </div>
              <div>
                <dt>Traffic State</dt>
                <dd>
                  <StatusBadge status={endpoint.traffic_state} />
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{formatApiDateTime(endpoint.created_at)}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{formatApiDateTime(endpoint.updated_at)}</dd>
              </div>
            </dl>
            <div className="description-block">
              <h3 className="section-subheading">Description</h3>
              <p className="description-text">
                {endpoint.description?.trim() ? endpoint.description : '—'}
              </p>
            </div>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="active-route-heading"
          >
            <h2 id="active-route-heading">ACTIVE Route</h2>
            {active ? (
              <>
                <dl className="meta-grid">
                  <div>
                    <dt>Alias</dt>
                    <dd className="mono">{endpoint.alias}</dd>
                  </div>
                  <div>
                    <dt>Deployment</dt>
                    <dd>
                      <Link
                        className="table-link"
                        to={`/deployments/${active.deployment_id}`}
                      >
                        {active.deployment?.name ??
                          shortId(active.deployment_id)}
                      </Link>
                    </dd>
                  </div>
                  <div>
                    <dt>Runtime</dt>
                    <dd>
                      <StatusBadge
                        status={active.deployment?.runtime_status}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt>Health</dt>
                    <dd>
                      <StatusBadge status={active.deployment?.health_status} />
                    </dd>
                  </div>
                  <div>
                    <dt>Rewrite Model Name</dt>
                    <dd className="mono">
                      {active.rewrite_model_name || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Activated</dt>
                    <dd>{formatApiDateTime(active.activated_at)}</dd>
                  </div>
                </dl>

                <div className="toolbar toolbar--wrap">
                  <div className="toolbar__actions">
                    <button
                      type="button"
                      className="btn btn--danger"
                      data-testid="unpublish-open"
                      disabled={busy || previewing || refreshing}
                      onClick={openUnpublishDialog}
                    >
                      Unpublish
                    </button>
                  </div>
                </div>
                <p className="panel-hint">
                  Unpublish는 Gateway Alias 호출 경로(ACTIVE Route)만
                  해제합니다. Deployment Stop / Remove / Cache Purge와는 다른
                  작업입니다. Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠
                  Purged ≠ Archived.
                </p>
              </>
            ) : (
              <p className="empty-state" role="status">
                ACTIVE Route가 없습니다. Switch workflow를 사용할 수 없습니다.
              </p>
            )}

            {unpublishError && !unpublishOpen ? (
              <SectionError title="Unpublish" message={unpublishError} />
            ) : null}

            {unpublishResult ? (
              <div
                className="operation-result"
                role="status"
                data-testid="unpublish-result"
              >
                <h3 className="section-subheading">최근 Unpublish 결과</h3>
                <dl className="meta-grid">
                  <div>
                    <dt>Backend Route</dt>
                    <dd data-testid="unpublish-changed">
                      {unpublishResult.changed
                        ? '해제됨 (changed=true)'
                        : '이미 해제된 상태 (changed=false)'}
                    </dd>
                  </div>
                  <div>
                    <dt>Routing Version</dt>
                    <dd>
                      {unpublishResult.routing_version != null
                        ? unpublishResult.routing_version
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Previous Deployment</dt>
                    <dd className="mono">
                      {unpublishResult.previous_route?.deployment_id
                        ? shortId(
                            unpublishResult.previous_route.deployment_id,
                          )
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Gateway verification</dt>
                    <dd>
                      {unpublishResult.gateway_verification?.status ? (
                        <StatusBadge
                          status={
                            unpublishResult.gateway_verification.status
                          }
                        />
                      ) : (
                        '—'
                      )}
                    </dd>
                  </div>
                </dl>
                <p
                  className="panel-hint"
                  data-testid="unpublish-gateway-hint"
                >
                  {gatewayVerificationPassed(
                    unpublishResult.gateway_verification?.status,
                  )
                    ? 'Gateway 검증 통과.'
                    : gatewayVerificationHint(
                        unpublishResult.gateway_verification?.status,
                      )}{' '}
                  Backend Route 해제와 Gateway 검증은 별개 상태입니다.
                </p>
              </div>
            ) : null}
          </section>

          <section
            className="detail-panel"
            aria-labelledby="switch-heading"
          >
            <h2 id="switch-heading">Switch (HOT / COLD)</h2>
            <p className="panel-hint">
              Preflight preview는 분석 전용입니다 (`preview_only`). 합산 VRAM으로
              가능 여부를 추론하지 말고 GPU별 결과를 확인하세요. Switch enqueue
              후에도 Worker가 fresh Preflight를 다시 수행합니다
              (`worker_must_revalidate`).
            </p>

            {!canSwitchWorkflow ? (
              <p className="metric-line" role="status">
                {switchBlockedReason}
              </p>
            ) : (
              <>
                {targetsError ? (
                  <SectionError
                    title="Target Deployment 목록 오류"
                    message={targetsError}
                  />
                ) : null}
                {targetsLoading && targets === null ? (
                  <LoadingBlock label="Target Deployment 목록을 불러오는 중…" />
                ) : null}

                <div className="toolbar toolbar--wrap">
                  <label className="toolbar__field" htmlFor="switch-target">
                    <span>Target Deployment</span>
                    <select
                      id="switch-target"
                      value={targetId}
                      disabled={busy || previewing}
                      onChange={(e) => onTargetChange(e.target.value)}
                    >
                      <option value="">선택하세요</option>
                      {(targets ?? []).map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name} ({d.runtime_status}/{d.health_status})
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="toolbar__actions">
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={!targetId || busy || previewing}
                      aria-busy={previewing}
                      onClick={() => void runPreview()}
                    >
                      {previewing ? 'Preview 중…' : 'Preflight Preview'}
                    </button>
                  </div>
                </div>

                {previewError ? (
                  <SectionError
                    title="Preflight preview 실패"
                    message={previewError}
                  />
                ) : null}

                {preview ? (
                  <div className="preflight-result">
                    <h3 className="section-subheading">Preflight Preview</h3>
                    <dl className="meta-grid">
                      <div>
                        <dt>Result</dt>
                        <dd>
                          <StatusBadge status={preview.result} />
                        </dd>
                      </div>
                      <div>
                        <dt>Required Peak VRAM</dt>
                        <dd>{formatMemoryMb(preview.required_peak_vram_mb)}</dd>
                      </div>
                      <div>
                        <dt>Available Hot VRAM</dt>
                        <dd>
                          {formatMemoryMb(preview.available_hot_vram_mb)}
                        </dd>
                      </div>
                      <div>
                        <dt>Reclaimable VRAM</dt>
                        <dd>{formatMemoryMb(preview.reclaimable_vram_mb)}</dd>
                      </div>
                      <div>
                        <dt>After Reclaim</dt>
                        <dd>
                          {formatMemoryMb(preview.available_after_reclaim_mb)}
                        </dd>
                      </div>
                      <div>
                        <dt>Safety Margin</dt>
                        <dd>{formatMemoryMb(preview.safety_margin_mb)}</dd>
                      </div>
                      <div>
                        <dt>Evaluated</dt>
                        <dd>{formatApiDateTime(preview.evaluated_at)}</dd>
                      </div>
                      <div>
                        <dt>preview_only</dt>
                        <dd>{preview.preview_only ? 'true' : 'false'}</dd>
                      </div>
                      <div>
                        <dt>worker_must_revalidate</dt>
                        <dd>
                          {preview.worker_must_revalidate ? 'true' : 'false'}
                        </dd>
                      </div>
                    </dl>

                    <h3 className="section-subheading">GPU Results</h3>
                    <p className="panel-hint">
                      GPU별 판정입니다. 부모 aggregate 합계만으로 HOT/COLD 가능
                      여부를 결정하지 마세요.
                    </p>
                    {preview.gpu_results.length > 0 ? (
                      <div className="table-wrap">
                        <table className="data-table">
                          <thead>
                            <tr>
                              <th scope="col">GPU Device</th>
                              <th scope="col">Free</th>
                              <th scope="col">Required</th>
                              <th scope="col">Hot Available</th>
                              <th scope="col">After Reclaim</th>
                              <th scope="col">Margin</th>
                              <th scope="col">Result</th>
                            </tr>
                          </thead>
                          <tbody>
                            {preview.gpu_results.map((g) => (
                              <tr key={g.gpu_device_id}>
                                <td className="mono">
                                  {shortId(g.gpu_device_id, 12)}
                                </td>
                                <td>{formatMemoryMb(g.free_vram_mb)}</td>
                                <td>{formatMemoryMb(g.required_vram_mb)}</td>
                                <td>
                                  {formatMemoryMb(g.available_hot_vram_mb)}
                                </td>
                                <td>
                                  {formatMemoryMb(
                                    g.available_after_reclaim_mb,
                                  )}
                                </td>
                                <td>{formatMemoryMb(g.safety_margin_mb)}</td>
                                <td>
                                  <StatusBadge status={g.result} />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <p className="empty-state" role="status">
                        GPU 결과가 없습니다.
                      </p>
                    )}

                    {preview.result === 'RESOURCE_INSUFFICIENT' ? (
                      <SectionError
                        title="자원 부족"
                        message="RESOURCE_INSUFFICIENT preview에서는 Switch를 enqueue할 수 없습니다."
                      />
                    ) : (
                      <div className="toolbar toolbar--wrap">
                        <label
                          className="toolbar__field"
                          htmlFor="switch-strategy"
                        >
                          <span>Strategy</span>
                          <select
                            id="switch-strategy"
                            value={strategy}
                            disabled={busy}
                            onChange={(e) =>
                              setStrategy(e.target.value as SwitchStrategy | '')
                            }
                          >
                            <option value="">선택하세요</option>
                            <option value="HOT" disabled={!hotAllowed}>
                              HOT
                            </option>
                            <option value="COLD" disabled={!coldAllowed}>
                              COLD
                            </option>
                          </select>
                        </label>
                        <label
                          className="toolbar__field"
                          htmlFor="switch-reason"
                        >
                          <span>Reason (optional)</span>
                          <input
                            id="switch-reason"
                            type="text"
                            value={reason}
                            disabled={busy}
                            onChange={(e) => setReason(e.target.value)}
                            placeholder="maintenance / upgrade"
                          />
                        </label>
                        <div className="toolbar__actions">
                          <button
                            type="button"
                            className="btn btn--primary"
                            disabled={!switchAllowed || busy}
                            aria-busy={switching}
                            onClick={() => void runSwitch()}
                          >
                            {switching ? 'Enqueue 중…' : 'Switch Enqueue'}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                ) : null}

                {switchError ? (
                  <SectionError
                    title="Switch enqueue 실패"
                    message={switchError}
                  />
                ) : null}
              </>
            )}

            {lastOperation ? (
              <div className="operation-result" role="status">
                <h3 className="section-subheading">최근 Switch Operation</h3>
                <dl className="meta-grid">
                  <div>
                    <dt>Operation ID</dt>
                    <dd className="mono">
                      {lastOperation.operation_id || lastOperation.id}
                    </dd>
                  </div>
                  <div>
                    <dt>Type</dt>
                    <dd>{lastOperation.operation_type}</dd>
                  </div>
                  <div>
                    <dt>Strategy</dt>
                    <dd>{lastOperation.switch_strategy || '—'}</dd>
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
            aria-labelledby="route-history-heading"
          >
            <h2 id="route-history-heading">Route History</h2>
            {routesError ? (
              <SectionError title="Route 이력 오류" message={routesError} />
            ) : null}
            {routesLoading && routes === null ? (
              <LoadingBlock label="Route 이력을 불러오는 중…" />
            ) : null}
            {routes !== null && routes.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Status</th>
                      <th scope="col">Deployment</th>
                      <th scope="col">Rewrite</th>
                      <th scope="col">Activated</th>
                      <th scope="col">Deactivated</th>
                      <th scope="col">Operation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routes.map((r) => (
                      <tr key={r.id}>
                        <td>
                          <StatusBadge status={r.status} />
                        </td>
                        <td>
                          <Link
                            className="table-link mono"
                            to={`/deployments/${r.deployment_id}`}
                          >
                            {shortId(r.deployment_id)}
                          </Link>
                        </td>
                        <td className="mono">
                          {r.rewrite_model_name || '—'}
                        </td>
                        <td>{formatApiDateTime(r.activated_at)}</td>
                        <td>{formatApiDateTime(r.deactivated_at)}</td>
                        <td className="mono">
                          {r.operation_id ? shortId(r.operation_id) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {routes !== null && routes.length === 0 ? (
              <p className="empty-state" role="status">
                Route 이력이 없습니다.
              </p>
            ) : null}
          </section>
        </>
      ) : null}

      {unpublishOpen && endpoint?.active_route ? (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="unpublish-dialog-title"
          data-testid="unpublish-dialog"
        >
          <div className="modal-card">
            <h2 id="unpublish-dialog-title">Unpublish 확인</h2>
            <p>
              Alias <strong className="mono">{endpoint.alias}</strong> 의
              ACTIVE Route를 해제합니다.
            </p>
            <dl className="meta-grid">
              <div>
                <dt>Alias</dt>
                <dd className="mono">{endpoint.alias}</dd>
              </div>
              <div>
                <dt>Deployment</dt>
                <dd className="mono">
                  {endpoint.active_route.deployment?.name ??
                    shortId(endpoint.active_route.deployment_id)}
                </dd>
              </div>
              <div>
                <dt>Rewrite Model Name</dt>
                <dd className="mono">
                  {endpoint.active_route.rewrite_model_name || '—'}
                </dd>
              </div>
            </dl>
            <p className="secondary-text">
              경고: Unpublish는 Gateway에서 이 Alias로의 호출 경로를
              해제합니다. Deployment를 Stop/Remove 하거나 Cache를 Purge하지
              않습니다.
            </p>
            <label className="toolbar__field" htmlFor="unpublish-alias-confirm">
              <span>확인을 위해 현재 Alias를 입력하세요</span>
              <input
                id="unpublish-alias-confirm"
                type="text"
                value={unpublishAliasInput}
                disabled={unpublishing}
                autoComplete="off"
                onChange={(e) => setUnpublishAliasInput(e.target.value)}
                placeholder={endpoint.alias}
              />
            </label>
            {unpublishError ? (
              <SectionError title="확인 오류" message={unpublishError} />
            ) : null}
            <div className="toolbar__actions">
              <button
                type="button"
                className="btn"
                disabled={unpublishing}
                onClick={() => {
                  setUnpublishError(null)
                  closeUnpublishDialog()
                }}
              >
                취소
              </button>
              <button
                type="button"
                className="btn btn--danger"
                data-testid="unpublish-confirm"
                disabled={
                  unpublishing ||
                  unpublishAliasInput !== endpoint.alias ||
                  busy ||
                  previewing
                }
                aria-busy={unpublishing}
                onClick={() => void confirmUnpublish()}
              >
                {unpublishing ? 'Unpublish 중…' : 'Unpublish 실행'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
