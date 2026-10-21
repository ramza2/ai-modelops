import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
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
import { getOperation } from '../api/operations'
import type {
  Deployment,
  Endpoint,
  EndpointRoute,
  OperationDetail,
  PreflightPreview,
  SwitchOperation,
} from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { EndpointEnabledConfirmDialog } from '../components/EndpointEnabledConfirmDialog'
import { EndpointTrafficDisplay } from '../components/EndpointTrafficDisplay'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { useEndpointEnabledAction } from '../hooks/useEndpointEnabledAction'
import { formatApiDateTime, shortId } from '../utils/date'
import { formatMemoryMb } from '../utils/number'
import {
  formatOperationError,
  isActiveOperationStatus,
  isSwitchOperationForEndpoint,
  isTerminalOperationStatus,
  OPERATION_POLL_MS,
  operationPollConfig,
} from '../utils/operationLifecycle'
import { ENDPOINT_FIELD_HELP } from '../utils/uiLabels'

const SWITCH_OPERATION_QUERY = 'switch_operation_id'

function toSwitchOperation(
  op: SwitchOperation | OperationDetail,
): SwitchOperation {
  const id = op.id
  const operationId =
    'operation_id' in op && typeof op.operation_id === 'string' && op.operation_id
      ? op.operation_id
      : id
  return {
    id,
    operation_id: operationId,
    operation_type: op.operation_type,
    switch_strategy: op.switch_strategy,
    status: op.status,
    endpoint_alias_id: op.endpoint_alias_id,
    source_deployment_id: op.source_deployment_id,
    target_deployment_id: op.target_deployment_id,
    current_step: op.current_step,
    created_at: op.created_at,
    error: op.error,
  }
}

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
  const [searchParams, setSearchParams] = useSearchParams()
  const switchOperationIdParam =
    searchParams.get(SWITCH_OPERATION_QUERY)?.trim() || ''

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
  const [switchOpRestoreError, setSwitchOpRestoreError] = useState<
    string | null
  >(null)
  const [switchOpPollWarning, setSwitchOpPollWarning] = useState<string | null>(
    null,
  )
  /** True while a URL restore GET for switch_operation_id is in flight. */
  const [switchOpRestoring, setSwitchOpRestoring] = useState(false)

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
  const switchOpAbortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const targetsAbortRef = useRef<AbortController | null>(null)
  const activeEndpointIdRef = useRef(endpointId)
  activeEndpointIdRef.current = endpointId
  const trackedSwitchOpIdRef = useRef<string | null>(null)
  trackedSwitchOpIdRef.current = lastOperation?.id ?? null
  const switchOperationIdParamRef = useRef(switchOperationIdParam)
  switchOperationIdParamRef.current = switchOperationIdParam
  /** Bumped on every Switch Operation fetch so older responses cannot win. */
  const switchOpFetchGenRef = useRef(0)
  const terminalRefreshOpIdRef = useRef<string | null>(null)
  const refreshingRef = useRef(false)
  refreshingRef.current = refreshing
  const switchingRef = useRef(false)
  switchingRef.current = switching
  const unpublishingRef = useRef(false)
  unpublishingRef.current = unpublishing

  const switchBlockedReason = endpoint
    ? switchWorkflowBlockedReason(endpoint)
    : null
  const switchOpVerified = Boolean(
    switchOperationIdParam &&
      lastOperation &&
      lastOperation.id === switchOperationIdParam &&
      isSwitchOperationForEndpoint(lastOperation, endpointId),
  )
  /**
   * Fail-closed: URL tracks a Switch Operation but we do not yet hold a
   * verified snapshot for that id → block Switch/Unpublish until restore
   * succeeds (or URL is cleared for definitive 404/mismatch).
   */
  const switchTrackingUnresolved = Boolean(
    switchOperationIdParam && !switchOpVerified,
  )
  const trackedSwitchActive = isActiveOperationStatus(lastOperation?.status)
  const canSwitchWorkflow =
    switchBlockedReason === null &&
    !trackedSwitchActive &&
    !switchTrackingUnresolved &&
    !unpublishOpen &&
    !unpublishing

  const syncSwitchOperationQuery = useCallback(
    (opId: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (opId) next.set(SWITCH_OPERATION_QUERY, opId)
          else next.delete(SWITCH_OPERATION_QUERY)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const clearTrackedSwitchOperation = useCallback(
    (opts?: { clearQuery?: boolean; clearWarning?: boolean }) => {
      setLastOperation(null)
      if (opts?.clearWarning !== false) {
        setSwitchOpPollWarning(null)
      }
      terminalRefreshOpIdRef.current = null
      if (opts?.clearQuery !== false) {
        syncSwitchOperationQuery(null)
      }
    },
    [syncSwitchOperationQuery],
  )

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

  const loadRef = useRef(load)
  loadRef.current = load

  const enabledAction = useEndpointEnabledAction({
    onSuccess: async () => {
      await loadRef.current('refresh')
    },
    isExternallyBusy: () =>
      switchingRef.current || unpublishingRef.current || refreshingRef.current,
  })
  const enabledActionCloseRef = useRef(enabledAction.close)
  enabledActionCloseRef.current = enabledAction.close

  /**
   * First time a Switch Operation is observed as terminal for this id,
   * re-read Endpoint + Route History once. SUCCEEDED ≠ invent ACTIVE Route;
   * load() failures keep prior snapshot / error UI.
   */
  const refreshEndpointOnFirstTerminal = useCallback(
    async (
      opId: string,
      status: string,
      stillCurrent: () => boolean,
    ): Promise<void> => {
      if (!isTerminalOperationStatus(status)) return
      if (terminalRefreshOpIdRef.current === opId) return
      if (!stillCurrent()) return
      terminalRefreshOpIdRef.current = opId
      await loadRef.current('refresh')
      // Ignore late completion if Endpoint/Operation identity changed.
      if (!stillCurrent()) return
    },
    [],
  )

  useEffect(() => {
    closeUnpublishDialog()
    setUnpublishResult(null)
    setUnpublishError(null)
    // Drop prior Endpoint's Unpublish busy; do not leave buttons stuck.
    setUnpublishing(false)
    enabledActionCloseRef.current()
    // Drop prior Endpoint Switch tracking UI (URL restore runs separately).
    setLastOperation(null)
    setSwitchOpRestoreError(null)
    setSwitchOpPollWarning(null)
    setSwitchOpRestoring(false)
    terminalRefreshOpIdRef.current = null
    switchOpFetchGenRef.current += 1
    void load('initial')
    return () => {
      abortRef.current?.abort()
      targetsAbortRef.current?.abort()
      unpublishAbortRef.current?.abort()
      switchOpAbortRef.current?.abort()
      enabledActionCloseRef.current()
    }
  }, [load, closeUnpublishDialog])

  // F5 / deep-link: restore Switch Operation from switch_operation_id query.
  useEffect(() => {
    const epId = endpointId
    const opId = switchOperationIdParam
    if (!epId || !opId) {
      setSwitchOpRestoring(false)
      return
    }

    switchOpAbortRef.current?.abort()
    const controller = new AbortController()
    switchOpAbortRef.current = controller
    const gen = ++switchOpFetchGenRef.current
    setSwitchOpRestoreError(null)
    setSwitchOpRestoring(true)
    // Do not display a previous Operation while restoring a different URL id.
    if (
      trackedSwitchOpIdRef.current &&
      trackedSwitchOpIdRef.current !== opId
    ) {
      setLastOperation(null)
    }

    const stillCurrent = () =>
      !controller.signal.aborted &&
      gen === switchOpFetchGenRef.current &&
      activeEndpointIdRef.current === epId &&
      switchOperationIdParamRef.current === opId

    void getOperation(opId, controller.signal)
      .then(async (data) => {
        if (!stillCurrent()) return
        if (!isSwitchOperationForEndpoint(data, epId) || data.id !== opId) {
          setLastOperation(null)
          setSwitchOpRestoreError(
            'URL의 Operation이 이 Endpoint의 SWITCH가 아니므로 표시하지 않습니다.',
          )
          syncSwitchOperationQuery(null)
          return
        }
        const tracked = toSwitchOperation(data)
        setLastOperation(tracked)
        setSwitchOpPollWarning(null)
        setSwitchOpRestoreError(null)
        // F5 may finish Endpoint GET before Operation; re-read Routes once
        // when Terminal is first confirmed (do not invent ACTIVE Route).
        await refreshEndpointOnFirstTerminal(
          tracked.id,
          tracked.status,
          stillCurrent,
        )
      })
      .catch((err: unknown) => {
        if (!stillCurrent()) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (err instanceof ApiError && err.status === 404) {
          setLastOperation(null)
          setSwitchOpRestoreError(
            'Switch Operation을 찾을 수 없습니다. URL 추적을 해제합니다.',
          )
          syncSwitchOperationQuery(null)
          return
        }
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Switch Operation을 불러오지 못했습니다.'
        // Keep last good snapshot for the same Endpoint/Operation id.
        // Do not auto-clear URL or unblock — manual refresh must re-verify.
        setSwitchOpRestoreError(
          `${message} 마지막 정상 Operation 값은 유지합니다. 새로고침으로 다시 확인하세요.`,
        )
      })
      .finally(() => {
        if (gen === switchOpFetchGenRef.current) {
          setSwitchOpRestoring(false)
        }
      })

    return () => {
      controller.abort()
    }
  }, [
    endpointId,
    switchOperationIdParam,
    syncSwitchOperationQuery,
    refreshEndpointOnFirstTerminal,
  ])

  // Poll while tracked Switch Operation is in-flight (4s).
  useEffect(() => {
    const epId = endpointId
    const opId = lastOperation?.id
    if (!epId || !opId) return
    if (!isActiveOperationStatus(lastOperation?.status)) return
    // Only poll once URL tracking is verified for this Operation.
    if (switchOperationIdParam !== opId) return

    let cancelled = false
    const timer = window.setInterval(() => {
      if (
        refreshingRef.current ||
        switchingRef.current ||
        unpublishingRef.current
      ) {
        return
      }

      switchOpAbortRef.current?.abort()
      const controller = new AbortController()
      switchOpAbortRef.current = controller
      const gen = ++switchOpFetchGenRef.current
      void getOperation(opId, controller.signal)
        .then(async (data) => {
          if (
            cancelled ||
            controller.signal.aborted ||
            gen !== switchOpFetchGenRef.current ||
            activeEndpointIdRef.current !== epId ||
            switchOperationIdParamRef.current !== opId ||
            trackedSwitchOpIdRef.current !== opId ||
            data.id !== opId
          ) {
            return
          }
          if (!isSwitchOperationForEndpoint(data, epId)) {
            // Stop tracking but keep the mismatch warning on screen.
            setLastOperation(null)
            terminalRefreshOpIdRef.current = null
            syncSwitchOperationQuery(null)
            setSwitchOpPollWarning(
              'Polling 중 Operation 소속이 일치하지 않아 추적을 중단합니다. 잘못된 Operation 결과는 표시하지 않습니다.',
            )
            return
          }
          const tracked = toSwitchOperation(data)
          setLastOperation(tracked)
          setSwitchOpPollWarning(null)
          setLastUpdated(new Date())
          const stillCurrent = () =>
            !cancelled &&
            !controller.signal.aborted &&
            gen === switchOpFetchGenRef.current &&
            activeEndpointIdRef.current === epId &&
            switchOperationIdParamRef.current === opId &&
            trackedSwitchOpIdRef.current === opId
          // SUCCEEDED display ≠ Route change; re-read Endpoint/Routes once.
          await refreshEndpointOnFirstTerminal(
            opId,
            tracked.status,
            stillCurrent,
          )
        })
        .catch((err: unknown) => {
          if (
            cancelled ||
            controller.signal.aborted ||
            gen !== switchOpFetchGenRef.current ||
            activeEndpointIdRef.current !== epId ||
            switchOperationIdParamRef.current !== opId ||
            trackedSwitchOpIdRef.current !== opId
          ) {
            return
          }
          if (err instanceof DOMException && err.name === 'AbortError') return
          // Keep last known Operation snapshot; do not invent terminal state.
          setSwitchOpPollWarning(
            'Switch Operation 조회에 실패했습니다. 마지막 정상값을 유지합니다. 새로고침으로 다시 확인하세요.',
          )
        })
    }, operationPollConfig.ms)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [
    endpointId,
    lastOperation?.id,
    lastOperation?.status,
    switchOperationIdParam,
    syncSwitchOperationQuery,
    refreshEndpointOnFirstTerminal,
  ])

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
    if (
      !endpoint?.active_route?.deployment_id ||
      unpublishing ||
      switching ||
      trackedSwitchActive ||
      switchTrackingUnresolved
    ) {
      return
    }
    setUnpublishExpectedDepId(endpoint.active_route.deployment_id)
    setUnpublishAliasInput('')
    setUnpublishError(null)
    setUnpublishOpen(true)
  }

  const confirmUnpublish = async () => {
    if (
      !endpointId ||
      unpublishing ||
      switching ||
      previewing ||
      refreshing ||
      trackedSwitchActive ||
      switchTrackingUnresolved
    ) {
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
    // Ownership for this POST: Endpoint ID only. Do not gate busy-clear on
    // readGen — load('refresh') after Unpublish intentionally bumps it.
    const epId = endpointId
    const expectedDep = unpublishExpectedDepId
    const stillThisEndpoint = () =>
      !controller.signal.aborted && activeEndpointIdRef.current === epId

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
      if (!stillThisEndpoint()) return
      setUnpublishResult(result)
      closeUnpublishDialog()
      await load('refresh')
    } catch (err) {
      if (!stillThisEndpoint()) return
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
      // Refresh bumps readGen; still clear busy when this Endpoint owns the work.
      if (activeEndpointIdRef.current === epId) {
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
      unpublishOpen ||
      trackedSwitchActive ||
      switchTrackingUnresolved ||
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
    if (
      !endpointId ||
      !targetId ||
      !strategy ||
      switching ||
      unpublishing ||
      unpublishOpen ||
      trackedSwitchActive ||
      switchTrackingUnresolved
    ) {
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
    setSwitchOpRestoreError(null)
    setSwitchOpPollWarning(null)
    try {
      const op = await switchEndpoint(endpointId, {
        targetDeploymentId: targetId,
        strategy,
        reason,
      })
      if (activeEndpointIdRef.current !== endpointId) return
      const tracked = toSwitchOperation(op)
      terminalRefreshOpIdRef.current = null
      setLastOperation(tracked)
      syncSwitchOperationQuery(tracked.id)
      // Do not auto-retry on uncertain errors; Endpoint refresh is separate.
      await load('refresh')
      if (activeEndpointIdRef.current !== endpointId) return
      // If enqueue already returned Terminal, this refresh covers Routes —
      // mark so a concurrent restore GET does not duplicate.
      if (isTerminalOperationStatus(tracked.status)) {
        terminalRefreshOpIdRef.current = tracked.id
      }
    } catch (err) {
      if (activeEndpointIdRef.current !== endpointId) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Switch Operation enqueue에 실패했습니다.'
      // Uncertain outcome — do not automatically re-POST.
      setSwitchError(message)
    } finally {
      if (activeEndpointIdRef.current === endpointId) {
        setSwitching(false)
      }
    }
  }

  if (notFound) {
    return (
      <AppShell
        title="API 연결 관리"
        description="API 연결 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/endpoints">
          ← API 연결 관리
        </Link>
        <SectionError
          title="API 연결을 찾을 수 없습니다."
          message="요청한 Endpoint Alias가 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  // Enqueue/Unpublish/사용중지 mutations block refresh; in-flight Switch poll
  // does not (operators must be able to recover after a poll failure).
  const busy =
    switching ||
    unpublishing ||
    enabledAction.submitting ||
    enabledAction.isDialogOpen
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
      title={endpoint?.display_name ?? endpoint?.alias ?? 'API 연결 상세'}
      description="API 별칭·현재 활성 연결·Route 이력과 Preflight Preview 기반 HOT/COLD Switch enqueue입니다. Preview는 참고용이며 Worker가 실행 직전 자원 상태를 다시 검증합니다."
      onRefresh={() => {
        void (async () => {
          const epId = endpointId
          const opId = switchOperationIdParamRef.current
          const gen = ++switchOpFetchGenRef.current
          switchOpAbortRef.current?.abort()
          const controller = new AbortController()
          switchOpAbortRef.current = controller

          const stillCurrent = () =>
            !controller.signal.aborted &&
            gen === switchOpFetchGenRef.current &&
            activeEndpointIdRef.current === epId &&
            (!opId || switchOperationIdParamRef.current === opId)

          await load('refresh')
          if (!stillCurrent()) return
          if (!opId) return
          // URL may have changed during Endpoint refresh — drop stale Op fetch.
          if (switchOperationIdParamRef.current !== opId) return

          try {
            const data = await getOperation(opId, controller.signal)
            if (
              !stillCurrent() ||
              switchOperationIdParamRef.current !== opId ||
              data.id !== opId
            ) {
              return
            }
            if (!isSwitchOperationForEndpoint(data, epId)) {
              setSwitchOpRestoreError(
                'URL의 Operation이 이 Endpoint의 SWITCH가 아니므로 표시하지 않습니다.',
              )
              clearTrackedSwitchOperation({ clearWarning: true })
              return
            }
            const tracked = toSwitchOperation(data)
            setLastOperation(tracked)
            setSwitchOpPollWarning(null)
            setSwitchOpRestoreError(null)
            // Endpoint GET ran before Operation; if Terminal is first seen here,
            // re-read Endpoint/Routes once so ACTIVE Route is not stale.
            await refreshEndpointOnFirstTerminal(
              tracked.id,
              tracked.status,
              () =>
                stillCurrent() &&
                switchOperationIdParamRef.current === opId &&
                trackedSwitchOpIdRef.current === tracked.id,
            )
          } catch (err: unknown) {
            if (
              !stillCurrent() ||
              switchOperationIdParamRef.current !== opId
            ) {
              return
            }
            if (err instanceof DOMException && err.name === 'AbortError') return
            if (err instanceof ApiError && err.status === 404) {
              setLastOperation(null)
              setSwitchOpRestoreError(
                'Switch Operation을 찾을 수 없습니다. URL 추적을 해제합니다.',
              )
              syncSwitchOperationQuery(null)
              return
            }
            // Preserve last good snapshot; stay fail-closed until verified.
            setSwitchOpPollWarning(
              'Switch Operation 재조회에 실패했습니다. 마지막 정상값을 유지합니다. 다시 새로고침하세요.',
            )
          }
        })()
      }}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={busy || previewing}
    >
      <Link className="back-link" to="/endpoints">
        ← API 연결 관리
      </Link>

      <p className="panel-hint" role="note">
        {ENDPOINT_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="API 별칭 vs Served Model Name">
            {ENDPOINT_FIELD_HELP.alias}
          </HelpHint>
          <HelpHint showTopic label="사용 설정 vs 요청 처리">
            {ENDPOINT_FIELD_HELP.enabled} {ENDPOINT_FIELD_HELP.traffic}
          </HelpHint>
          <HelpHint showTopic label="Traffic 표시 (사용 중지·미연결)">
            {ENDPOINT_FIELD_HELP.trafficDisplay}
          </HelpHint>
          <HelpHint showTopic label="현재 활성 연결">
            {ENDPOINT_FIELD_HELP.activeRoute}
          </HelpHint>
          <HelpHint showTopic label="Preflight Preview">
            {ENDPOINT_FIELD_HELP.preflight}
          </HelpHint>
          <HelpHint showTopic label="HOT Switch">
            {ENDPOINT_FIELD_HELP.hotSwitch}
          </HelpHint>
          <HelpHint showTopic label="COLD Switch">
            {ENDPOINT_FIELD_HELP.coldSwitch}
          </HelpHint>
          <HelpHint showTopic label="연결 해제 (Unpublish)">
            {ENDPOINT_FIELD_HELP.unpublish}
          </HelpHint>
          <HelpHint showTopic label="사용 중지 / 다시 사용">
            {ENDPOINT_FIELD_HELP.disable} {ENDPOINT_FIELD_HELP.enable}
          </HelpHint>
          <HelpHint showTopic label="사용 중지 vs 다른 정리 작업">
            {ENDPOINT_FIELD_HELP.disableVsOthers}
          </HelpHint>
        </div>
      </details>

      {enabledAction.resultMessage ? (
        <div
          className="operation-result"
          role="status"
          data-testid="endpoint-enabled-result"
        >
          <p>{enabledAction.resultMessage}</p>
          <p className="secondary-text">
            {ENDPOINT_FIELD_HELP.gatewayAfterEnabledChange}
          </p>
        </div>
      ) : null}
      {enabledAction.openError ? (
        <SectionError
          title="사용 설정 변경"
          message={enabledAction.openError}
        />
      ) : null}

      {loading && !endpoint ? (
        <LoadingBlock label="API 연결을 불러오는 중…" />
      ) : null}

      {error && !notFound ? (
        <SectionError
          title={endpoint ? 'API 연결 새로고침 실패' : 'API 연결 오류'}
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
                <dt>
                  <TermLabel
                    text="API 별칭"
                    apiTerm="Alias"
                    help={ENDPOINT_FIELD_HELP.alias}
                  />
                </dt>
                <dd className="mono">{endpoint.alias}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="API 유형" apiTerm="API Type" />
                </dt>
                <dd>{endpoint.api_type || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="요청 처리 상태"
                    apiTerm="Traffic"
                    help={ENDPOINT_FIELD_HELP.traffic}
                  />
                </dt>
                <dd>
                  <EndpointTrafficDisplay
                    isEnabled={endpoint.is_enabled}
                    trafficState={endpoint.traffic_state}
                    hasActiveRoute={Boolean(endpoint.active_route?.deployment_id)}
                  />
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="생성 시각" apiTerm="Created" />
                </dt>
                <dd>{formatApiDateTime(endpoint.created_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="수정 시각" apiTerm="Updated" />
                </dt>
                <dd>{formatApiDateTime(endpoint.updated_at)}</dd>
              </div>
            </dl>
            <div className="description-block">
              <h3 className="section-subheading">설명 (Description)</h3>
              <p className="description-text">
                {endpoint.description?.trim() ? endpoint.description : '—'}
              </p>
            </div>

            <div className="toolbar toolbar--wrap">
              <div className="toolbar__actions">
                {endpoint.is_enabled ? (
                  <button
                    type="button"
                    className="btn btn--danger"
                    data-testid="endpoint-disable-open"
                    disabled={
                      busy ||
                      previewing ||
                      refreshing ||
                      trackedSwitchActive ||
                      switchTrackingUnresolved
                    }
                    onClick={() =>
                      void enabledAction.open(endpoint.id, 'disable')
                    }
                  >
                    사용 중지
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn--primary"
                    data-testid="endpoint-enable-open"
                    disabled={
                      busy ||
                      previewing ||
                      refreshing ||
                      trackedSwitchActive ||
                      switchTrackingUnresolved
                    }
                    onClick={() =>
                      void enabledAction.open(endpoint.id, 'enable')
                    }
                  >
                    다시 사용 (재활성화)
                  </button>
                )}
              </div>
            </div>
            <p className="panel-hint" role="note">
              {ENDPOINT_FIELD_HELP.disableVsOthers} Route History·Operation
              이력은 유지됩니다. 영구 삭제는 제공하지 않습니다.
            </p>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="active-route-heading"
          >
            <h2 id="active-route-heading">
              현재 활성 연결 (ACTIVE Route)
            </h2>
            {active ? (
              <>
                <dl className="meta-grid">
                  <div>
                    <dt>
                      <TermLabel
                        text="API 별칭"
                        apiTerm="Alias"
                        help={ENDPOINT_FIELD_HELP.alias}
                      />
                    </dt>
                    <dd className="mono">{endpoint.alias}</dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel
                        text="연결된 모델 배포"
                        apiTerm="Deployment"
                        help={ENDPOINT_FIELD_HELP.activeRoute}
                      />
                    </dt>
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
                    <dt>
                      <TermLabel text="실행 상태" apiTerm="Runtime" />
                    </dt>
                    <dd>
                      <StatusBadge
                        status={active.deployment?.runtime_status}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="헬스" apiTerm="Health" />
                    </dt>
                    <dd>
                      <StatusBadge status={active.deployment?.health_status} />
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel
                        text="Served Model Name"
                        apiTerm="Rewrite"
                        help={ENDPOINT_FIELD_HELP.alias}
                      />
                    </dt>
                    <dd className="mono">
                      {active.rewrite_model_name || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>
                      <TermLabel text="활성화 시각" apiTerm="Activated" />
                    </dt>
                    <dd>{formatApiDateTime(active.activated_at)}</dd>
                  </div>
                </dl>

                <div className="toolbar toolbar--wrap">
                  <div className="toolbar__actions">
                    <button
                      type="button"
                      className="btn btn--danger"
                      data-testid="unpublish-open"
                      disabled={
                        busy ||
                        previewing ||
                        refreshing ||
                        trackedSwitchActive ||
                        switchTrackingUnresolved
                      }
                      onClick={openUnpublishDialog}
                    >
                      연결 해제 (Unpublish)
                    </button>
                  </div>
                </div>
                <p className="panel-hint panel-hint--warn" role="note">
                  {ENDPOINT_FIELD_HELP.unpublish} Unpublished ≠ Stopped ≠
                  Removed ≠ Retired ≠ Purged ≠ Archived.
                </p>
              </>
            ) : (
              <>
                <p className="empty-state" role="status">
                  현재 활성 연결(ACTIVE Route)이 없습니다. Switch workflow를
                  사용할 수 없습니다.
                </p>
                <p className="panel-hint" role="note">
                  {ENDPOINT_FIELD_HELP.unusedAlias}
                </p>
              </>
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
            <h2 id="switch-heading">모델 전환 Switch (HOT / COLD)</h2>
            <p className="panel-hint" role="note">
              {ENDPOINT_FIELD_HELP.preflight}
            </p>
            <p className="panel-hint" role="note">
              {ENDPOINT_FIELD_HELP.hotSwitch} {ENDPOINT_FIELD_HELP.coldSwitch}
            </p>
            <p className="panel-hint">
              Preview 응답의 `preview_only` / `worker_must_revalidate` 값을
              확인하세요. 합산 VRAM으로 가능 여부를 추론하지 말고 GPU별 결과를
              보세요.
            </p>

            {switchTrackingUnresolved ? (
              <p
                className="panel-hint"
                role="status"
                data-testid="switch-op-unresolved"
              >
                {switchOpRestoring
                  ? 'URL의 Switch Operation을 확인하는 중입니다. 확인이 끝나기 전까지 Switch Enqueue, Preflight Preview, Unpublish는 차단됩니다.'
                  : 'Switch Operation 추적이 아직 확인되지 않아 Switch/Unpublish를 차단합니다. 새로고침으로 다시 확인하세요.'}
              </p>
            ) : null}

            {trackedSwitchActive ? (
              <p className="panel-hint" role="status" data-testid="switch-op-active">
                Switch Operation이 진행 중입니다 ({lastOperation?.status}
                {lastOperation?.current_step
                  ? ` / ${lastOperation.current_step}`
                  : ''}
                ). 추가 Switch Enqueue는 차단되며 {OPERATION_POLL_MS / 1000}
                초마다 Operation 상태를 자동 조회합니다. ACTIVE Route는
                Operation 성공 표시와 별개로 Endpoint 재조회 결과를 따릅니다.
              </p>
            ) : null}

            {switchOpRestoreError ? (
              <SectionError
                title="Switch Operation 복원"
                message={switchOpRestoreError}
              />
            ) : null}
            {switchOpPollWarning ? (
              <div data-testid="switch-op-poll-warning">
                <SectionError
                  title="Switch Operation 조회"
                  message={switchOpPollWarning}
                />
              </div>
            ) : null}

            {!canSwitchWorkflow ? (
              <p className="metric-line" role="status">
                {switchTrackingUnresolved
                  ? 'Switch Operation 확인 전이라 Switch workflow를 사용할 수 없습니다.'
                  : trackedSwitchActive
                    ? '진행 중인 Switch Operation이 있어 Switch workflow를 사용할 수 없습니다.'
                    : unpublishOpen || unpublishing
                      ? 'Unpublish 진행 중에는 Switch를 사용할 수 없습니다.'
                      : switchBlockedReason}
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
                    <span>변경 대상 Target Deployment</span>
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
                      {previewing
                        ? 'Preview 중…'
                        : '사전 검사 (Preflight Preview)'}
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
                    <h3 className="section-subheading">
                      사전 검사 결과 (Preflight Preview)
                    </h3>
                    <p className="panel-hint panel-hint--warn" role="note">
                      {ENDPOINT_FIELD_HELP.preflight}
                    </p>
                    <dl className="meta-grid">
                      <div>
                        <dt>
                          <TermLabel text="판정" apiTerm="Result" />
                        </dt>
                        <dd>
                          <StatusBadge status={preview.result} />
                        </dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel
                            text="필요 Peak VRAM"
                            apiTerm="Required Peak VRAM"
                          />
                        </dt>
                        <dd>{formatMemoryMb(preview.required_peak_vram_mb)}</dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel
                            text="Hot 가용 VRAM"
                            apiTerm="Available Hot VRAM"
                          />
                        </dt>
                        <dd>
                          {formatMemoryMb(preview.available_hot_vram_mb)}
                        </dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel
                            text="회수 가능 VRAM"
                            apiTerm="Reclaimable VRAM"
                          />
                        </dt>
                        <dd>{formatMemoryMb(preview.reclaimable_vram_mb)}</dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel
                            text="회수 후 가용"
                            apiTerm="After Reclaim"
                          />
                        </dt>
                        <dd>
                          {formatMemoryMb(preview.available_after_reclaim_mb)}
                        </dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel
                            text="안전 여유"
                            apiTerm="Safety Margin"
                          />
                        </dt>
                        <dd>{formatMemoryMb(preview.safety_margin_mb)}</dd>
                      </div>
                      <div>
                        <dt>
                          <TermLabel text="평가 시각" apiTerm="Evaluated" />
                        </dt>
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

                    <h3 className="section-subheading">GPU별 결과 (GPU Results)</h3>
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
                          <span>전환 방식 Strategy</span>
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
                          <span>사유 Reason (optional)</span>
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
                            {switching
                              ? 'Enqueue 중…'
                              : '전환 실행 (Switch Enqueue)'}
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
              <div
                className="operation-result"
                role="status"
                data-testid="switch-operation-result"
              >
                <h3 className="section-subheading">
                  추적 중 Switch Operation
                </h3>
                {isActiveOperationStatus(lastOperation.status) ? (
                  <p className="panel-hint">
                    진행 중 — Operation을 {OPERATION_POLL_MS / 1000}초마다 자동
                    갱신합니다. 성공 여부는 Backend 응답 상태 그대로 표시하며
                    ACTIVE Route 변경은 추정하지 않습니다.
                  </p>
                ) : null}
                {isTerminalOperationStatus(lastOperation.status) ? (
                  <p className="panel-hint" data-testid="switch-op-terminal">
                    Operation이 {lastOperation.status} 상태입니다. Route 변경
                    여부는 위 현재 활성 연결(GET Endpoint)을 확인하세요.
                  </p>
                ) : null}
                <dl className="meta-grid">
                  <div>
                    <dt>
                      <TermLabel text="작업 ID" apiTerm="Operation ID" />
                    </dt>
                    <dd>
                      <Link
                        className="table-link mono"
                        to={`/operations/${lastOperation.id}`}
                        data-testid="switch-op-detail-link"
                      >
                        {lastOperation.operation_id || lastOperation.id}
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
                      <TermLabel text="전환 방식" apiTerm="Strategy" />
                    </dt>
                    <dd>{lastOperation.switch_strategy || '—'}</dd>
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
                      <TermLabel text="현재 작업 단계" apiTerm="Current Step" />
                    </dt>
                    <dd>{lastOperation.current_step || '—'}</dd>
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
            aria-labelledby="route-history-heading"
          >
            <h2 id="route-history-heading">연결 이력 (Route History)</h2>
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

      {enabledAction.isDialogOpen && enabledAction.mode ? (
        <EndpointEnabledConfirmDialog
          mode={enabledAction.mode}
          endpoint={enabledAction.draft}
          refreshing={enabledAction.refreshing}
          submitting={enabledAction.submitting}
          error={enabledAction.error}
          consent={enabledAction.consent}
          onConsentChange={enabledAction.setConsent}
          aliasInput={enabledAction.aliasInput}
          onAliasInputChange={enabledAction.setAliasInput}
          onCancel={enabledAction.close}
          onConfirm={() => void enabledAction.confirm()}
        />
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
            <h2 id="unpublish-dialog-title">연결 해제 (Unpublish) 확인</h2>
            <p>
              Alias <strong className="mono">{endpoint.alias}</strong> 의
              현재 활성 연결(ACTIVE Route)을 해제합니다.
            </p>
            <dl className="meta-grid">
              <div>
                <dt>API 별칭 (Alias)</dt>
                <dd className="mono">{endpoint.alias}</dd>
              </div>
              <div>
                <dt>모델 배포 (Deployment)</dt>
                <dd className="mono">
                  {endpoint.active_route.deployment?.name ??
                    shortId(endpoint.active_route.deployment_id)}
                </dd>
              </div>
              <div>
                <dt>Served Model Name (Rewrite)</dt>
                <dd className="mono">
                  {endpoint.active_route.rewrite_model_name || '—'}
                </dd>
              </div>
            </dl>
            <p className="secondary-text">
              경고: {ENDPOINT_FIELD_HELP.unpublish}
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
                {unpublishing
                  ? 'Unpublish 중…'
                  : '연결 해제 실행 (Unpublish)'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
