import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
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
import { DangerConfirmDialog } from '../components/DangerConfirmDialog'
import { HelpHint } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { shortId } from '../utils/date'
import { DECOMMISSION_FIELD_HELP } from '../utils/uiLabels'

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

const STEP_LABELS: Record<Step, string> = {
  inspect: '1. 현재 상태 확인',
  unpublish: '2. API 연결 해제',
  stop: '3. 모델 실행 중지',
  remove: '4. 모델 컨테이너 제거',
  retire: '5. 모델 배포 퇴역 처리',
  purge: '6. 로컬 모델 캐시 삭제 (선택)',
  archive: '7. 모델·버전 보관 (선택)',
  done: '8. 정리 결과 확인',
}

const OPTIONAL_STEPS = new Set<Step>(['purge', 'archive'])

type ConfirmKind =
  | 'unpublish'
  | 'stop'
  | 'remove'
  | 'retire'
  | 'purge'
  | 'archive_version'
  | 'archive_model'

type StatusFingerprint = {
  activeRouteKey: string
  runtime_status: string
  container_present: boolean | null
  retired_at: string | null
  can_unpublish: boolean
  can_stop: boolean
  can_remove_container: boolean
  can_retire: boolean
  can_purge_cache: boolean
  active_operation_id: string | null
  cache_id: string | null
  cache_status: string | null
}

function isStep(raw: string | null): raw is Step {
  return Boolean(raw && (STEPS as string[]).includes(raw))
}

function isActiveOperationStatus(status: string | null | undefined): boolean {
  const s = (status || '').toUpperCase()
  return s === 'QUEUED' || s === 'RUNNING' || s === 'ROLLING_BACK'
}

function hasActiveLifecycleOperation(st: DecommissionStatus): boolean {
  return Boolean(
    st.active_operation &&
      isActiveOperationStatus(st.active_operation.status),
  )
}

/** Only PASSED counts as Gateway apply confirmed. Everything else is incomplete. */
export function isGatewayVerificationPassed(
  status: string | null | undefined,
): boolean {
  return (status || '').toUpperCase() === 'PASSED'
}

function containerPresentLabel(value: boolean | null): string {
  if (value === true) return '있음 (true)'
  if (value === false) return '없음 (false)'
  return '확인 불가 (UNKNOWN / null)'
}

function fingerprintOf(st: DecommissionStatus): StatusFingerprint {
  return {
    activeRouteKey: st.active_routes
      .map((r) => `${r.endpoint_id}:${r.route_id}`)
      .sort()
      .join('|'),
    runtime_status: st.runtime_status,
    container_present: st.container_present,
    retired_at: st.retired_at,
    can_unpublish: st.can_unpublish,
    can_stop: st.can_stop,
    can_remove_container: st.can_remove_container,
    can_retire: st.can_retire,
    can_purge_cache: st.can_purge_cache,
    active_operation_id: st.active_operation?.operation_id ?? null,
    cache_id: st.source_cache?.cache_id ?? null,
    cache_status: st.source_cache?.status ?? null,
  }
}

function fingerprintsEqual(a: StatusFingerprint, b: StatusFingerprint): boolean {
  return (
    a.activeRouteKey === b.activeRouteKey &&
    a.runtime_status === b.runtime_status &&
    a.container_present === b.container_present &&
    a.retired_at === b.retired_at &&
    a.can_unpublish === b.can_unpublish &&
    a.can_stop === b.can_stop &&
    a.can_remove_container === b.can_remove_container &&
    a.can_retire === b.can_retire &&
    a.can_purge_cache === b.can_purge_cache &&
    a.active_operation_id === b.active_operation_id &&
    a.cache_id === b.cache_id &&
    a.cache_status === b.cache_status
  )
}

function actionAllowed(
  kind: ConfirmKind,
  st: DecommissionStatus,
  opts?: { gatewayVerifyIncomplete?: boolean },
): boolean {
  // Do not assume safety while a Deployment lifecycle Operation is in flight.
  // Especially block destructive Purge during Stop/Remove.
  if (hasActiveLifecycleOperation(st)) {
    if (
      kind === 'stop' ||
      kind === 'remove' ||
      kind === 'retire' ||
      kind === 'purge' ||
      kind === 'archive_version' ||
      kind === 'archive_model'
    ) {
      return false
    }
  }
  switch (kind) {
    case 'unpublish':
      return st.can_unpublish && st.active_routes.length > 0
    case 'stop':
      if (opts?.gatewayVerifyIncomplete) return false
      return (
        st.can_stop &&
        st.active_routes.length === 0 &&
        st.runtime_status !== 'STOPPED'
      )
    case 'remove':
      return st.can_remove_container && st.active_routes.length === 0
    case 'retire':
      return st.can_retire && !st.retired_at
    case 'purge':
      return (
        st.can_purge_cache &&
        Boolean(st.source_cache) &&
        (st.source_cache?.status || '').toUpperCase() !== 'MISSING'
      )
    case 'archive_version':
    case 'archive_model':
      return Boolean(st.retired_at)
    default:
      return false
  }
}

function canAdvanceFromStop(st: DecommissionStatus): boolean {
  if (st.active_routes.length > 0) return false
  if (hasActiveLifecycleOperation(st)) return false
  // STOP SUCCEEDED alone is not enough — runtime must be STOPPED.
  return st.runtime_status === 'STOPPED'
}

function canAdvanceFromRemove(st: DecommissionStatus): boolean {
  if (hasActiveLifecycleOperation(st)) return false
  // null = UNKNOWN — never treat as removed.
  return st.container_present === false
}

function gateReasonForStep(step: Step, st: DecommissionStatus): string | null {
  switch (step) {
    case 'inspect':
    case 'done':
      return null
    case 'unpublish':
      if (st.active_routes.length === 0) {
        return 'ACTIVE Route가 없어 연결 해제 대상이 없습니다. 다음 단계로 이동할 수 있습니다.'
      }
      if (!st.can_unpublish) {
        return 'Backend can_unpublish=false 입니다. blockers와 작업 이력을 확인하세요.'
      }
      return null
    case 'stop':
      if (st.active_routes.length > 0) {
        return 'ACTIVE Route가 남아 있습니다. 먼저 API 연결 해제를 완료하세요.'
      }
      if (hasActiveLifecycleOperation(st)) {
        return '진행 중인 Lifecycle Operation이 있습니다. 완료·실패를 확인한 뒤 진행하세요.'
      }
      if (st.runtime_status === 'STOPPED') {
        return '이미 runtime_status=STOPPED 입니다. 중복 중지를 보내지 말고 컨테이너 제거로 진행하세요.'
      }
      if (!st.can_stop) {
        return 'Backend can_stop=false 입니다. blockers를 확인하세요.'
      }
      return null
    case 'remove':
      if (st.active_routes.length > 0) {
        return 'ACTIVE Route가 남아 있어 컨테이너 제거를 진행할 수 없습니다.'
      }
      if (st.runtime_status !== 'STOPPED' && st.deployment_type === 'MANAGED') {
        return '실행 상태가 STOPPED가 아닙니다. STOP Operation SUCCEEDED만으로 안전하지 않습니다.'
      }
      if (hasActiveLifecycleOperation(st)) {
        return '진행 중인 Operation이 있어 컨테이너 제거를 시작할 수 없습니다.'
      }
      if (st.container_present === false) {
        return '컨테이너가 이미 없습니다(false). 제거 요청 없이 퇴역 단계로 이동할 수 있습니다.'
      }
      if (st.container_present === null) {
        return '컨테이너 존재 여부를 확인할 수 없습니다(UNKNOWN). 안전하다고 추정하지 마세요.'
      }
      if (!st.can_remove_container) {
        return 'Backend can_remove_container=false 입니다. blockers를 확인하세요.'
      }
      return null
    case 'retire':
      if (st.retired_at) return '이미 퇴역 처리된 배포입니다.'
      if (st.container_present === true) {
        return '컨테이너가 아직 있습니다. 제거가 확인된 뒤에만 퇴역하세요.'
      }
      if (st.container_present === null) {
        return '컨테이너 상태가 UNKNOWN입니다. 확인 없이 퇴역하지 마세요.'
      }
      if (!st.can_retire) {
        return 'Backend can_retire=false 입니다. blockers를 확인하세요.'
      }
      return null
    case 'purge':
      if (hasActiveLifecycleOperation(st)) {
        return '진행 중인 Deployment Operation이 있습니다. 중지·제거가 끝나기 전에 캐시를 삭제하지 마세요.'
      }
      if (!st.retired_at) {
        return '배포 퇴역이 끝나기 전입니다. 선택 단계이므로 건너뛸 수 있지만, 퇴역 완료 후 진행을 권장합니다.'
      }
      if (!st.source_cache) return '연결된 소스 캐시가 없습니다. 이 선택 단계를 건너뛰세요.'
      if ((st.source_cache.status || '').toUpperCase() === 'MISSING') {
        return '캐시 상태가 MISSING — 이미 로컬 캐시가 없는 상태입니다.'
      }
      if (!st.can_purge_cache) {
        return 'Backend can_purge_cache=false 입니다. blockers를 확인하세요.'
      }
      return null
    case 'archive':
      if (hasActiveLifecycleOperation(st)) {
        return '진행 중인 Operation이 있습니다. 레지스트리 보관은 Operation 종료 후 진행하세요.'
      }
      if (!st.retired_at) {
        return '배포 퇴역 전에 레지스트리 보관을 단정하지 마세요. 선택 단계입니다.'
      }
      return null
    default:
      return null
  }
}

const ACTION_ERROR_GUIDANCE =
  '작업 이력과 최신 배포 상태를 확인한 후, 필요한 경우 다시 시도하세요. 응답이 불확실하면 즉시 재실행하지 마세요.'

export function DecommissionWizardPage() {
  const { deploymentId = '' } = useParams<{ deploymentId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()

  const stepParam = searchParams.get('step')
  const operationIdParam = searchParams.get('operation_id') || ''

  const [step, setStep] = useState<Step>(
    isStep(stepParam) ? stepParam : 'inspect',
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
  const [revalidating, setRevalidating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [staleNotice, setStaleNotice] = useState<string | null>(null)
  const [purgeConfirm, setPurgeConfirm] = useState('')
  const [consent, setConsent] = useState(false)
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null)
  const [confirmFingerprint, setConfirmFingerprint] =
    useState<StatusFingerprint | null>(null)
  const [purgeSkipped, setPurgeSkipped] = useState(false)
  const [purgeCompleted, setPurgeCompleted] = useState(false)
  const [archiveVersionDone, setArchiveVersionDone] = useState(false)
  const [archiveModelDone, setArchiveModelDone] = useState(false)
  /** Session-only: Gateway verification incomplete after Unpublish (not durable across F5). */
  const [gatewayVerifyIncomplete, setGatewayVerifyIncomplete] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  /** Sync lock: set before any await in confirm→POST path. */
  const confirmInFlightRef = useRef(false)
  /** Bumped on close / deployment change to invalidate in-flight confirm work. */
  const actionEpochRef = useRef(0)
  const deploymentIdRef = useRef(deploymentId)
  deploymentIdRef.current = deploymentId
  const confirmTargetRef = useRef<{
    versionId: string | null
    modelId: string | null
  }>({ versionId: null, modelId: null })
  const gatewayVerifyIncompleteRef = useRef(false)
  gatewayVerifyIncompleteRef.current = gatewayVerifyIncomplete

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
      return { status: st, deployment: dep }
    },
    [deploymentId],
  )

  const refreshRegistry = useCallback(
    async (dep: Deployment, signal?: AbortSignal) => {
      const ver = await getModelVersion(dep.model_version_id, signal)
      setVersion(ver)
      const mod = await getModel(ver.model_id, signal)
      setModel(mod)
      if (ver.archived_at) setArchiveVersionDone(true)
      if (!mod.is_active) setArchiveModelDone(true)
      return { ver, mod }
    },
    [],
  )

  const closeConfirm = useCallback(() => {
    // Invalidate any in-flight revalidate/POST for this dialog.
    actionEpochRef.current += 1
    confirmInFlightRef.current = false
    confirmTargetRef.current = { versionId: null, modelId: null }
    setConfirmKind(null)
    setConfirmFingerprint(null)
    setConsent(false)
    setPurgeConfirm('')
    setRevalidating(false)
    setBusy(false)
  }, [])

  const openConfirm = useCallback(
    (kind: ConfirmKind) => {
      if (!status || busy || confirmInFlightRef.current) return
      if (kind === 'archive_version' || kind === 'archive_model') {
        if (!version?.id || !model?.id) {
          setActionError(
            `보관 대상 Version/Model을 확인할 수 없습니다. — ${ACTION_ERROR_GUIDANCE}`,
          )
          return
        }
        confirmTargetRef.current = {
          versionId: version.id,
          modelId: model.id,
        }
      } else {
        confirmTargetRef.current = { versionId: null, modelId: null }
      }
      setActionError(null)
      setStaleNotice(null)
      setConsent(false)
      setPurgeConfirm('')
      setConfirmFingerprint(fingerprintOf(status))
      setConfirmKind(kind)
    },
    [status, busy, version, model],
  )

  type RevalidateOk = {
    ok: true
    status: DecommissionStatus
    deployment: Deployment
    version: ModelVersion | null
    model: ModelSummary | null
  }

  /**
   * Fresh GET before mutation. Frontend consent is not an atomic lock:
   * GET→POST can still race with concurrent operators/workers — Backend
   * can_* / blockers remain authoritative; do not claim Frontend-only safety.
   */
  const revalidateBeforeAction = useCallback(
    async (kind: ConfirmKind, epoch: number): Promise<RevalidateOk | { ok: false }> => {
      setRevalidating(true)
      setStaleNotice(null)
      const stillCurrent = () =>
        epoch === actionEpochRef.current &&
        deploymentIdRef.current === deploymentId
      try {
        const { status: st, deployment: dep } = await refreshStatus()
        if (!stillCurrent()) return { ok: false }
        if (
          confirmFingerprint &&
          !fingerprintsEqual(confirmFingerprint, fingerprintOf(st))
        ) {
          setConsent(false)
          setPurgeConfirm('')
          setConfirmFingerprint(fingerprintOf(st))
          setStaleNotice(
            '확인 이후 상태가 변경되었습니다. 최신 내용을 다시 확인한 뒤 동의해 주세요.',
          )
          return { ok: false }
        }

        let ver: ModelVersion | null = version
        let mod: ModelSummary | null = model
        if (kind === 'archive_version' || kind === 'archive_model') {
          const expectedVersionId = confirmTargetRef.current.versionId
          const expectedModelId = confirmTargetRef.current.modelId
          if (!expectedVersionId || !expectedModelId) {
            setActionError(
              `보관 대상 ID를 확인할 수 없습니다. — ${ACTION_ERROR_GUIDANCE}`,
            )
            closeConfirm()
            return { ok: false }
          }
          if (dep.model_version_id !== expectedVersionId) {
            setConsent(false)
            setPurgeConfirm('')
            setStaleNotice(
              '최신 Deployment의 Model Version이 확인 대상과 다릅니다. POST를 중단했습니다. 대상을 다시 확인하세요.',
            )
            const refreshed = await refreshRegistry(dep)
            if (!stillCurrent()) return { ok: false }
            ver = refreshed.ver
            mod = refreshed.mod
            confirmTargetRef.current = {
              versionId: ver.id,
              modelId: mod.id,
            }
            setConfirmFingerprint(fingerprintOf(st))
            return { ok: false }
          }
          const refreshed = await refreshRegistry(dep)
          if (!stillCurrent()) return { ok: false }
          ver = refreshed.ver
          mod = refreshed.mod
          if (ver.id !== expectedVersionId || mod.id !== expectedModelId) {
            setConsent(false)
            setStaleNotice(
              '확인창 대상과 최신 Version/Model ID가 일치하지 않습니다. 동의를 초기화했습니다.',
            )
            confirmTargetRef.current = {
              versionId: ver.id,
              modelId: mod.id,
            }
            setConfirmFingerprint(fingerprintOf(st))
            return { ok: false }
          }
          if (kind === 'archive_version' && ver.archived_at) {
            setStaleNotice(
              '모델 버전이 이미 보관(Archived) 상태입니다. 중복 실행하지 않습니다.',
            )
            setArchiveVersionDone(true)
            closeConfirm()
            return { ok: false }
          }
          if (kind === 'archive_model') {
            if (!ver.archived_at) {
              setConsent(false)
              setStaleNotice(
                '모델 보관 직전 Version이 아직 Archived가 아닙니다. 버전 보관을 먼저 완료하세요.',
              )
              setConfirmFingerprint(fingerprintOf(st))
              return { ok: false }
            }
            if (!mod.is_active) {
              setStaleNotice(
                '모델이 이미 비활성(보관) 상태입니다. 중복 실행하지 않습니다.',
              )
              setArchiveModelDone(true)
              closeConfirm()
              return { ok: false }
            }
          }
        }

        if (
          !actionAllowed(kind, st, {
            gatewayVerifyIncomplete: gatewayVerifyIncompleteRef.current,
          })
        ) {
          setConsent(false)
          setPurgeConfirm('')
          setConfirmFingerprint(fingerprintOf(st))
          setStaleNotice(
            '최신 Backend 판정으로 이 작업을 실행할 수 없습니다. can_* / blockers / Gateway 검증을 확인하세요.',
          )
          return { ok: false }
        }
        setConfirmFingerprint(fingerprintOf(st))
        return {
          ok: true,
          status: st,
          deployment: dep,
          version: ver,
          model: mod,
        }
      } catch (err) {
        if (!stillCurrent()) return { ok: false }
        const message =
          err instanceof ApiError
            ? err.message
            : '최신 상태 조회에 실패했습니다. 요청을 실행하지 않습니다.'
        setActionError(`${message} — ${ACTION_ERROR_GUIDANCE}`)
        closeConfirm()
        return { ok: false }
      } finally {
        if (stillCurrent()) setRevalidating(false)
      }
    },
    [
      refreshStatus,
      confirmFingerprint,
      version,
      model,
      refreshRegistry,
      closeConfirm,
      deploymentId,
    ],
  )

  useEffect(() => {
    if (isStep(stepParam) && stepParam !== step) {
      setStep(stepParam)
    } else if (stepParam && !isStep(stepParam)) {
      syncStep('inspect')
    }
  }, [stepParam, step, syncStep])

  useEffect(() => {
    // New Deployment identity: drop confirm lock / session gateway flag.
    actionEpochRef.current += 1
    confirmInFlightRef.current = false
    setGatewayVerifyIncomplete(false)
    closeConfirm()
  }, [deploymentId, closeConfirm])

  useEffect(() => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const { deployment: dep } = await refreshStatus(controller.signal)
        if (controller.signal.aborted) return
        await refreshRegistry(dep, controller.signal)
        if (controller.signal.aborted) return

        if (operationIdParam) {
          try {
            const op = await getOperation(operationIdParam, controller.signal)
            if (controller.signal.aborted) return
            setOperation(op)
          } catch {
            if (controller.signal.aborted) return
            setActionError(
              `작업 ID ${operationIdParam} 상세 조회에 실패했습니다. 작업 이력에서 상태를 확인하세요. 접수된 작업을 즉시 재실행하지 마세요.`,
            )
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return
        setError(
          err instanceof ApiError
            ? err.message
            : '모델 종료·정리 상태를 불러오지 못했습니다.',
        )
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    })()
    return () => controller.abort()
  }, [deploymentId, operationIdParam, refreshStatus, refreshRegistry])

  // Poll active Operation from URL — poll failure is not Operation failure.
  useEffect(() => {
    if (!operationIdParam) return
    if (
      operation?.status === 'SUCCEEDED' ||
      operation?.status === 'FAILED' ||
      operation?.status === 'CANCELLED' ||
      operation?.status === 'CANCELED'
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
        .catch(() => {
          // Keep last known Operation snapshot; do not treat as FAILED.
        })
    }, 2000)
    return () => window.clearInterval(timer)
  }, [operationIdParam, operation?.status, refreshStatus])

  const runConfirmedAction = async () => {
    // Sync lock before any await — blocks double confirm.
    if (!confirmKind || confirmInFlightRef.current) return
    if (busy) return
    const kind = confirmKind
    if (kind === 'purge' && purgeConfirm !== 'PURGE') {
      setActionError('정확한 PURGE 입력이 필요합니다. 번역·자동 입력하지 마세요.')
      return
    }
    confirmInFlightRef.current = true
    const epoch = actionEpochRef.current
    const lockedDeploymentId = deploymentId
    const stillCurrent = () =>
      epoch === actionEpochRef.current &&
      deploymentIdRef.current === lockedDeploymentId &&
      confirmInFlightRef.current

    setBusy(true)
    setActionError(null)
    try {
      const validated = await revalidateBeforeAction(kind, epoch)
      if (!stillCurrent()) return
      if (!validated.ok) return

      if (kind === 'unpublish') {
        const route = validated.status.active_routes[0]
        if (!route) throw new Error('ACTIVE Route가 없습니다.')
        if (!stillCurrent()) return
        const result = await unpublishEndpoint(route.endpoint_id, {
          expectedDeploymentId: lockedDeploymentId,
          reason: 'M7-D decommission unpublish',
          verifyGateway: true,
        })
        if (!stillCurrent()) return
        setUnpublishResult(result)
        const passed = isGatewayVerificationPassed(
          result.gateway_verification?.status,
        )
        setGatewayVerifyIncomplete(!passed)
        await refreshStatus()
        if (!stillCurrent()) return
        closeConfirm()
        if (passed) {
          syncStep('stop')
        } else {
          const rawStatus =
            result.gateway_verification?.status ?? 'null/없음'
          setStaleNotice(
            `DB Route 해제는 요청되었으나 Gateway 검증이 확인되지 않았습니다(status=${rawStatus}). PASSED가 아니면 적용 확인으로 보지 않습니다. 작업 이력과 Endpoint 연결 상태를 다시 확인하세요. 이 세션에서는 Stop을 진행하지 않습니다. (F5 후에는 Frontend 기억이 사라지며 완전한 안전은 Backend 조건부 검증이 필요합니다.)`,
          )
        }
      } else if (kind === 'stop') {
        if (!stillCurrent()) return
        const enqueued = await stopDeployment(lockedDeploymentId)
        if (!stillCurrent()) return
        syncStep('stop', { operationId: enqueued.id })
        try {
          const detail = await getOperation(enqueued.id)
          if (!stillCurrent()) return
          setOperation(detail)
        } catch {
          if (!stillCurrent()) return
          setActionError(
            `중지 작업은 접수되었을 수 있습니다(operation_id=${enqueued.id}). 작업 이력에서 확인하세요. 즉시 다시 중지하지 마세요.`,
          )
        }
        await refreshStatus()
        if (!stillCurrent()) return
        closeConfirm()
      } else if (kind === 'remove') {
        if (!stillCurrent()) return
        const enqueued = await removeDeployment(lockedDeploymentId)
        if (!stillCurrent()) return
        syncStep('remove', { operationId: enqueued.id })
        try {
          const detail = await getOperation(enqueued.id)
          if (!stillCurrent()) return
          setOperation(detail)
        } catch {
          if (!stillCurrent()) return
          setActionError(
            `컨테이너 제거 작업은 접수되었을 수 있습니다(operation_id=${enqueued.id}). 작업 이력에서 확인하세요. 즉시 다시 제거하지 마세요.`,
          )
        }
        await refreshStatus()
        if (!stillCurrent()) return
        closeConfirm()
      } else if (kind === 'retire') {
        if (!stillCurrent()) return
        await retireDeployment(lockedDeploymentId)
        if (!stillCurrent()) return
        await refreshStatus()
        if (!stillCurrent()) return
        closeConfirm()
        syncStep('purge', { operationId: null })
      } else if (kind === 'purge') {
        const cacheId = validated.status.source_cache?.cache_id
        if (!cacheId) throw new Error('삭제할 캐시가 없습니다.')
        if (!stillCurrent()) return
        await purgeModelCache(cacheId)
        if (!stillCurrent()) return
        setPurgeCompleted(true)
        setPurgeSkipped(false)
        await refreshStatus()
        if (!stillCurrent()) return
        closeConfirm()
        syncStep('archive')
      } else if (kind === 'archive_version') {
        const versionId = validated.version?.id
        if (!versionId) throw new Error('모델 버전 ID를 확인할 수 없습니다.')
        if (!stillCurrent()) return
        const ver = await archiveModelVersion(versionId)
        if (!stillCurrent()) return
        setVersion(ver)
        setArchiveVersionDone(true)
        closeConfirm()
      } else if (kind === 'archive_model') {
        const modelId = validated.model?.id
        if (!modelId) throw new Error('모델 ID를 확인할 수 없습니다.')
        if (!validated.version?.archived_at) {
          setStaleNotice(
            '모델 보관 직전 Version Archived 상태를 재확인할 수 없습니다. POST를 중단했습니다.',
          )
          return
        }
        if (!stillCurrent()) return
        const mod = await archiveModel(modelId)
        if (!stillCurrent()) return
        setModel(mod)
        setArchiveModelDone(true)
        closeConfirm()
        syncStep('done')
      }
    } catch (err) {
      if (!stillCurrent()) return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : '작업 실행에 실패했습니다.'
      setActionError(`원인: ${message} — ${ACTION_ERROR_GUIDANCE}`)
    } finally {
      if (epoch === actionEpochRef.current) {
        confirmInFlightRef.current = false
        setBusy(false)
        setRevalidating(false)
      }
    }
  }

  if (loading) {
    return (
      <AppShell
        title="모델 종료·정리 절차"
        description="단계적 정리 마법사"
        onRefresh={() => undefined}
        refreshing={false}
        lastUpdated={null}
        refreshDisabled
      >
        <LoadingBlock label="종료·정리 상태를 불러오는 중…" />
      </AppShell>
    )
  }

  if (error || !status || !deployment) {
    return (
      <AppShell
        title="모델 종료·정리 절차"
        description="단계적 정리 마법사"
        onRefresh={() => undefined}
        refreshing={false}
        lastUpdated={null}
        refreshDisabled
      >
        <SectionError
          title="상태를 불러오지 못했습니다"
          message={error || '모델 배포를 찾을 수 없습니다. 안전하다고 추정하지 마세요.'}
        />
        <Link className="back-link" to={`/deployments/${deploymentId}`}>
          ← 모델 배포 상세
        </Link>
      </AppShell>
    )
  }

  const route = status.active_routes[0]
  const stepGate = gateReasonForStep(step, status)
  const stopNextOk = canAdvanceFromStop(status)
  const removeNextOk = canAdvanceFromRemove(status)
  const stopSucceededButRunning =
    operation?.status === 'SUCCEEDED' &&
    (operation.operation_type || '').toUpperCase() === 'STOP' &&
    status.runtime_status !== 'STOPPED'
  const allowOpts = { gatewayVerifyIncomplete }
  const stopExecuteAllowed = actionAllowed('stop', status, allowOpts)

  const confirmMeta = confirmKind
    ? confirmDialogCopy(confirmKind, {
        deployment,
        status,
        version,
        model,
        routeAlias: route?.alias,
        routeEndpointId: route?.endpoint_id,
      })
    : null

  return (
    <AppShell
      title={`모델 종료·정리: ${deployment.name}`}
      description="API 연결 해제 ≠ 모델 중지 ≠ 컨테이너 제거 ≠ 배포 퇴역 ≠ 캐시 삭제 ≠ 레지스트리 보관"
      lastUpdated={lastUpdated}
      refreshing={refreshing}
      onRefresh={() => {
        setRefreshing(true)
        void refreshStatus()
          .then(async () => {
            if (deployment) await refreshRegistry(deployment)
          })
          .finally(() => setRefreshing(false))
      }}
    >
      <Link className="back-link" to={`/deployments/${deploymentId}`}>
        ← 모델 배포 상세
      </Link>

      <p className="panel-hint" role="note">
        {DECOMMISSION_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 단계 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="단계 이동 vs 실행">
            {DECOMMISSION_FIELD_HELP.stepNav}
          </HelpHint>
          <HelpHint showTopic label="실행 전 재검증">
            {DECOMMISSION_FIELD_HELP.revalidate}
          </HelpHint>
          <HelpHint showTopic label="UNKNOWN / 조회 실패">
            {DECOMMISSION_FIELD_HELP.unknownSafe}
          </HelpHint>
          <HelpHint showTopic label="STOP vs Runtime">
            {DECOMMISSION_FIELD_HELP.stopVsRuntime}
          </HelpHint>
          <HelpHint showTopic label="컨테이너 UNKNOWN">
            {DECOMMISSION_FIELD_HELP.containerUnknown}
          </HelpHint>
          <HelpHint showTopic label="비동기 Operation">
            {DECOMMISSION_FIELD_HELP.operationEnqueue}
          </HelpHint>
          <HelpHint showTopic label="Gateway 연결 해제 검증">
            {DECOMMISSION_FIELD_HELP.gatewayVerify}
          </HelpHint>
        </div>
      </details>

      <nav className="wizard-steps" aria-label="모델 종료·정리 단계">
        {STEPS.map((s) => {
          const optional = OPTIONAL_STEPS.has(s)
          const lockedHint = s === 'inspect' ? null : gateReasonForStep(s, status)
          return (
            <button
              key={s}
              type="button"
              className={
                s === step
                  ? 'wizard-steps__item wizard-steps__item--current'
                  : 'wizard-steps__item'
              }
              title={
                s === 'inspect'
                  ? '언제든 열 수 있습니다(조회만)'
                  : lockedHint ||
                    (optional ? '선택 단계 — 이동만으로 API를 실행하지 않습니다' : undefined)
              }
              onClick={() => syncStep(s)}
            >
              {STEP_LABELS[s]}
              {optional ? ' ·선택' : ''}
            </button>
          )
        })}
      </nav>

      {staleNotice ? (
        <SectionError title="상태 변경 감지" message={staleNotice} />
      ) : null}
      {actionError ? (
        <SectionError title="작업 오류" message={actionError} />
      ) : null}

      {step === 'inspect' ? (
        <section className="panel">
          <h2>{STEP_LABELS.inspect}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.inspect}</p>
          <dl className="kv">
            <div>
              <dt>배포 유형</dt>
              <dd>{status.deployment_type}</dd>
            </div>
            <div>
              <dt>목표 / 실행 / 정상 동작</dt>
              <dd>
                {status.desired_state} / {status.runtime_status} /{' '}
                {status.health_status}
              </dd>
            </div>
            <div>
              <dt>활성 API 연결 (ACTIVE Route)</dt>
              <dd>
                {status.active_routes.length === 0
                  ? '없음'
                  : status.active_routes
                      .map((r) => `${r.alias} (${r.endpoint_id})`)
                      .join(', ')}
              </dd>
            </div>
            <div>
              <dt>컨테이너 존재</dt>
              <dd>{containerPresentLabel(status.container_present)}</dd>
            </div>
            <div>
              <dt>소스 캐시</dt>
              <dd className="mono">
                {status.source_cache
                  ? `${status.source_cache.status} ${status.source_cache.local_path || ''}`
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>진행 중 Operation</dt>
              <dd>
                {status.active_operation
                  ? `${status.active_operation.operation_type} / ${status.active_operation.status} (${status.active_operation.operation_id})`
                  : '없음'}
              </dd>
            </div>
            <div>
              <dt>퇴역</dt>
              <dd>{status.retired_at ? status.retired_at : '아니오'}</dd>
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
            <p className="secondary-text">Backend blockers 없음</p>
          )}
          <p className="secondary-text">{status.note}</p>
          <button
            type="button"
            className="btn"
            onClick={() =>
              syncStep(status.active_routes.length ? 'unpublish' : 'stop')
            }
          >
            다음
          </button>
        </section>
      ) : null}

      {step === 'unpublish' ? (
        <section className="panel">
          <h2>{STEP_LABELS.unpublish}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.unpublish}</p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          {gatewayVerifyIncomplete ? (
            <SectionError
              title="Gateway 검증 미완료"
              message={DECOMMISSION_FIELD_HELP.gatewayVerify}
            />
          ) : null}
          {!route ? (
            <>
              <p>이 배포를 가리키는 ACTIVE Route가 없습니다.</p>
              <button
                type="button"
                className="btn"
                disabled={gatewayVerifyIncomplete}
                title={
                  gatewayVerifyIncomplete
                    ? 'Gateway 검증 미완료 — 이 세션에서 Stop으로 진행하지 않습니다'
                    : undefined
                }
                onClick={() => syncStep('stop')}
              >
                모델 실행 중지로 계속
              </button>
            </>
          ) : (
            <>
              <p>
                Alias <strong>{route.alias}</strong> (
                <span className="mono">{route.endpoint_id}</span>) 가 이 배포로
                연결되어 있습니다. 연결 해제는 Route만 제거합니다.
              </p>
              {unpublishResult ? (
                <div className="panel-hint">
                  <p>
                    DB Route 해제 요청 결과:{' '}
                    {unpublishResult.changed ? '변경됨' : '변경 없음'} ·
                    routing_version={String(unpublishResult.routing_version)}
                  </p>
                  <p>
                    Gateway 실제 반영 검증:{' '}
                    {unpublishResult.gateway_verification ? (
                      <>
                        <StatusBadge
                          status={unpublishResult.gateway_verification.status}
                        />
                        {isGatewayVerificationPassed(
                          unpublishResult.gateway_verification.status,
                        ) ? (
                          <span> — 적용 확인(PASSED)</span>
                        ) : (
                          <span>
                            {' '}
                            — 확인 미완료 (PASSED만 성공으로 봅니다)
                          </span>
                        )}
                      </>
                    ) : (
                      <span>없음(null) — 확인 미완료</span>
                    )}
                  </p>
                </div>
              ) : null}
              <button
                type="button"
                className="btn"
                disabled={busy || !actionAllowed('unpublish', status, allowOpts)}
                onClick={() => openConfirm('unpublish')}
              >
                API 연결 해제 확인…
              </button>
            </>
          )}
        </section>
      ) : null}

      {step === 'stop' ? (
        <section className="panel">
          <h2>{STEP_LABELS.stop}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.stop}</p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          {gatewayVerifyIncomplete ? (
            <SectionError
              title="Gateway 검증 미완료 — Stop 차단"
              message="이 세션에서 Gateway 연결 해제 검증이 PASSED로 확인되지 않았습니다. 작업 이력·Endpoint 연결 상태를 재확인하세요. F5 후에는 Frontend 상태가 사라지며 완전한 안전은 Backend 조건부 검증이 필요합니다."
            />
          ) : null}
          {status.active_routes.length > 0 ? (
            <SectionError
              title="ACTIVE Route 잔존"
              message="API 연결 해제 전에는 모델 중지를 실행할 수 없습니다."
            />
          ) : null}
          {stopSucceededButRunning ? (
            <SectionError
              title="STOP 완료 ≠ Runtime STOPPED"
              message="STOP Operation은 SUCCEEDED이지만 runtime_status가 아직 STOPPED가 아닙니다. 상태 동기화를 기다린 뒤 진행하세요."
            />
          ) : null}
          {operation && operationIdParam ? (
            <p>
              작업{' '}
              <Link to={`/operations/${operation.id}`}>{operation.id}</Link>{' '}
              <StatusBadge status={operation.status} />
              {isActiveOperationStatus(operation.status) ? (
                <span className="secondary-text"> — 자동 조회 중</span>
              ) : null}
            </p>
          ) : null}
          <button
            type="button"
            className="btn"
            disabled={busy || !stopExecuteAllowed}
            onClick={() => openConfirm('stop')}
          >
            모델 중지 확인…
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!stopNextOk}
            title={
              stopNextOk
                ? undefined
                : 'runtime_status=STOPPED 확인 전에는 컨테이너 제거로 진행할 수 없습니다'
            }
            onClick={() => syncStep('remove', { operationId: null })}
          >
            다음: 모델 컨테이너 제거
          </button>
        </section>
      ) : null}

      {step === 'remove' ? (
        <section className="panel">
          <h2>{STEP_LABELS.remove}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.remove}</p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          {operation && operationIdParam ? (
            <p>
              작업{' '}
              <Link to={`/operations/${operation.id}`}>{operation.id}</Link>{' '}
              <StatusBadge status={operation.status} />
            </p>
          ) : null}
          <p>
            컨테이너 존재: {containerPresentLabel(status.container_present)}
          </p>
          {status.container_present === null ? (
            <SectionError
              title="컨테이너 상태 확인 불가"
              message={DECOMMISSION_FIELD_HELP.containerUnknown}
            />
          ) : null}
          <button
            type="button"
            className="btn"
            disabled={busy || !actionAllowed('remove', status, allowOpts)}
            onClick={() => openConfirm('remove')}
          >
            컨테이너 제거 확인…
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!removeNextOk}
            title={
              removeNextOk
                ? undefined
                : 'container_present=false 가 확인될 때만 퇴역으로 진행합니다'
            }
            onClick={() => syncStep('retire', { operationId: null })}
          >
            다음: 모델 배포 퇴역 처리
          </button>
        </section>
      ) : null}

      {step === 'retire' ? (
        <section className="panel">
          <h2>{STEP_LABELS.retire}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.retire}</p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          {status.retired_at ? (
            <p>
              이미 퇴역 처리됨: <code>{status.retired_at}</code>
            </p>
          ) : null}
          <button
            type="button"
            className="btn"
            disabled={
              busy ||
              Boolean(status.retired_at) ||
              !actionAllowed('retire', status, allowOpts)
            }
            onClick={() => openConfirm('retire')}
          >
            {status.retired_at ? '이미 퇴역됨' : '배포 퇴역 확인…'}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!status.retired_at}
            onClick={() => syncStep('purge')}
          >
            다음: 로컬 캐시 삭제 (선택)
          </button>
        </section>
      ) : null}

      {step === 'purge' ? (
        <section className="panel">
          <h2>{STEP_LABELS.purge}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.purge}</p>
          <p className="panel-hint secondary-text">
            이 단계는 선택 사항입니다. 건너뛰어도 퇴역·중지가 자동 완료된 것은
            아닙니다.
          </p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          {status.source_cache ? (
            <dl className="kv">
              <div>
                <dt>경로</dt>
                <dd className="mono">{status.source_cache.local_path || '—'}</dd>
              </div>
              <div>
                <dt>Revision</dt>
                <dd className="mono">{status.source_cache.revision || '—'}</dd>
              </div>
              <div>
                <dt>상태</dt>
                <dd>{status.source_cache.status}</dd>
              </div>
              <div>
                <dt>크기(참고)</dt>
                <dd>
                  {status.source_cache.size_bytes != null
                    ? String(status.source_cache.size_bytes)
                    : '—'}
                  {(status.source_cache.status || '').toUpperCase() ===
                  'MISSING' ? (
                    <span className="secondary-text">
                      {' '}
                      — {DECOMMISSION_FIELD_HELP.missingCache}
                    </span>
                  ) : null}
                </dd>
              </div>
            </dl>
          ) : (
            <p>연결된 소스 캐시가 없습니다.</p>
          )}
          <button
            type="button"
            className="btn btn--danger"
            disabled={busy || !actionAllowed('purge', status, allowOpts)}
            onClick={() => openConfirm('purge')}
          >
            로컬 캐시 삭제 확인…
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              setPurgeSkipped(true)
              syncStep('archive')
            }}
          >
            건너뛰기 / 다음: 모델·버전 보관
          </button>
        </section>
      ) : null}

      {step === 'archive' ? (
        <section className="panel">
          <h2>{STEP_LABELS.archive}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.archive}</p>
          {stepGate ? (
            <p className="panel-hint panel-hint--warn">{stepGate}</p>
          ) : null}
          <p>
            모델 버전: {version?.version_label || '—'}{' '}
            <span className="mono secondary-text">
              ({version ? shortId(version.id) : '—'})
            </span>{' '}
            {version?.archived_at || archiveVersionDone ? (
              <StatusBadge status="ARCHIVED" />
            ) : (
              <button
                type="button"
                className="btn"
                disabled={
                  busy || !actionAllowed('archive_version', status, allowOpts)
                }
                onClick={() => openConfirm('archive_version')}
              >
                버전 보관 확인…
              </button>
            )}
          </p>
          <p>
            모델: {model?.name || '—'}{' '}
            <span className="mono secondary-text">
              ({model ? shortId(model.id) : '—'})
            </span>{' '}
            {model && (!model.is_active || archiveModelDone) ? (
              <StatusBadge status="INACTIVE" />
            ) : (
              <button
                type="button"
                className="btn"
                disabled={
                  busy ||
                  !version?.archived_at ||
                  !actionAllowed('archive_model', status, allowOpts)
                }
                onClick={() => openConfirm('archive_model')}
              >
                모델 보관 확인…
              </button>
            )}
          </p>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => syncStep('done')}
          >
            정리 결과 확인으로
          </button>
        </section>
      ) : null}

      {step === 'done' ? (
        <section className="panel">
          <h2>{STEP_LABELS.done}</h2>
          <p className="panel-hint">{DECOMMISSION_FIELD_HELP.done}</p>
          {status.note ? <p className="secondary-text">{status.note}</p> : null}
          <DoneSummary
            status={status}
            version={version}
            model={model}
            purgeSkipped={purgeSkipped}
            purgeCompleted={purgeCompleted}
            archiveVersionDone={archiveVersionDone || Boolean(version?.archived_at)}
            archiveModelDone={archiveModelDone || Boolean(model && !model.is_active)}
          />
          <button
            type="button"
            className="btn"
            onClick={() => navigate('/deployments')}
          >
            모델 배포 관리로
          </button>
        </section>
      ) : null}

      {confirmKind && confirmMeta ? (
        <DangerConfirmDialog
          title={confirmMeta.title}
          targetLabel={confirmMeta.targetLabel}
          targetId={confirmMeta.targetId}
          impact={confirmMeta.impact}
          notes={confirmMeta.notes}
          consentLabel={confirmMeta.consentLabel}
          consent={consent}
          onConsentChange={setConsent}
          typedConfirm={
            confirmKind === 'purge'
              ? {
                  expected: 'PURGE',
                  value: purgeConfirm,
                  onChange: setPurgeConfirm,
                  placeholder: 'PURGE',
                  hint: '삭제 확인을 위해 정확히 PURGE 를 입력하세요 (번역·자동 입력 금지)',
                }
              : undefined
          }
          refreshing={revalidating}
          submitting={busy}
          error={null}
          confirmLabel={confirmMeta.confirmLabel}
          submittingLabel="실행 중…"
          canConfirm={Boolean(
            status &&
              confirmKind &&
              actionAllowed(confirmKind, status, allowOpts),
          )}
          onCancel={closeConfirm}
          onConfirm={() => void runConfirmedAction()}
        />
      ) : null}
    </AppShell>
  )
}

function confirmDialogCopy(
  kind: ConfirmKind,
  ctx: {
    deployment: Deployment
    status: DecommissionStatus
    version: ModelVersion | null
    model: ModelSummary | null
    routeAlias?: string
    routeEndpointId?: string
  },
): {
  title: string
  targetLabel: string
  targetId?: string
  impact: ReactNode
  notes: ReactNode
  consentLabel: string
  confirmLabel: string
} {
  const depName = ctx.deployment.name
  const depId = ctx.deployment.id
  switch (kind) {
    case 'unpublish':
      return {
        title: 'API 연결 해제 확인',
        targetLabel: ctx.routeAlias || depName,
        targetId: ctx.routeEndpointId,
        impact: (
          <ul className="panel-hint-list">
            <li>해당 Alias를 이용하는 서비스 요청이 실패할 수 있습니다.</li>
            <li>ACTIVE Route만 제거합니다. 모델 중지·캐시 삭제와 다릅니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.unpublish,
        consentLabel: '영향 범위를 이해했으며 API 연결 해제를 진행합니다.',
        confirmLabel: '연결 해제 실행',
      }
    case 'stop':
      return {
        title: '모델 실행 중지 확인',
        targetLabel: depName,
        targetId: depId,
        impact: (
          <ul className="panel-hint-list">
            <li>실행 중인 모델 서비스가 중단될 수 있습니다.</li>
            <li>작업 접수(202) ≠ Runtime STOPPED 입니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.stop,
        consentLabel: '서비스 중단 가능성을 이해했으며 중지를 요청합니다.',
        confirmLabel: '중지 요청 실행',
      }
    case 'remove':
      return {
        title: '모델 컨테이너 제거 확인',
        targetLabel: depName,
        targetId: depId,
        impact: (
          <ul className="panel-hint-list">
            <li>모델 컨테이너가 삭제됩니다.</li>
            <li>Registry/Cache 삭제와 다르며 배포 메타데이터는 Retire 전까지 유지됩니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.remove,
        consentLabel: '컨테이너 제거 영향을 이해했으며 제거를 요청합니다.',
        confirmLabel: '제거 요청 실행',
      }
    case 'retire':
      return {
        title: '모델 배포 퇴역 확인',
        targetLabel: depName,
        targetId: depId,
        impact: (
          <ul className="panel-hint-list">
            <li>배포가 관리 대상에서 제외됩니다(retired_at).</li>
            <li>이력은 유지되며 캐시 파일은 삭제되지 않습니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.retire,
        consentLabel: '퇴역 처리 의미를 이해했으며 진행합니다.',
        confirmLabel: '퇴역 실행',
      }
    case 'purge':
      return {
        title: '로컬 모델 캐시 삭제 확인',
        targetLabel: ctx.status.source_cache?.local_path || '소스 캐시',
        targetId: ctx.status.source_cache?.cache_id,
        impact: (
          <ul className="panel-hint-list">
            <li>Node의 실제 모델 파일이 삭제될 수 있습니다.</li>
            <li>Model/Version Registry 삭제와 다릅니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.purge,
        consentLabel: '파일 삭제 위험을 이해했으며 캐시 삭제를 진행합니다.',
        confirmLabel: '캐시 삭제 실행',
      }
    case 'archive_version':
      return {
        title: '모델 버전 보관 확인',
        targetLabel: ctx.version?.version_label || 'Model Version',
        targetId: ctx.version?.id,
        impact: (
          <ul className="panel-hint-list">
            <li>해당 Model Version이 Archived로 표시됩니다.</li>
            <li>Runtime 중지·파일 삭제와 다릅니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.archive,
        consentLabel: '버전 보관 영향을 이해했으며 진행합니다.',
        confirmLabel: '버전 보관 실행',
      }
    case 'archive_model':
      return {
        title: '모델 보관 확인',
        targetLabel: ctx.model?.name || 'Model',
        targetId: ctx.model?.id,
        impact: (
          <ul className="panel-hint-list">
            <li>Model 전체 is_active=false로 변경됩니다.</li>
            <li>다른 버전·배포에도 영향할 수 있으며, 공유 여부를 단정하지 않습니다.</li>
          </ul>
        ),
        notes: DECOMMISSION_FIELD_HELP.archive,
        consentLabel: '모델 전체 보관 영향을 이해했으며 진행합니다.',
        confirmLabel: '모델 보관 실행',
      }
  }
}

function DoneSummary({
  status,
  version,
  model,
  purgeSkipped,
  purgeCompleted,
  archiveVersionDone,
  archiveModelDone,
}: {
  status: DecommissionStatus
  version: ModelVersion | null
  model: ModelSummary | null
  purgeSkipped: boolean
  purgeCompleted: boolean
  archiveVersionDone: boolean
  archiveModelDone: boolean
}) {
  const routesClear = status.active_routes.length === 0
  const runtimeStopped = status.runtime_status === 'STOPPED'
  const containerGone = status.container_present === false
  const containerUnknown = status.container_present === null
  const retired = Boolean(status.retired_at)
  const cacheMissing =
    !status.source_cache ||
    (status.source_cache.status || '').toUpperCase() === 'MISSING'
  const requiredDone =
    routesClear && runtimeStopped && containerGone && retired
  const unknownsRemain =
    containerUnknown ||
    status.active_operation != null ||
    (!routesClear && !retired)

  let headline = '필수 정리 항목이 아직 남아 있거나 확인되지 않았습니다'
  if (requiredDone && !unknownsRemain) {
    headline = purgeSkipped || !archiveVersionDone || !archiveModelDone
      ? '필수 정리 항목은 확인되었습니다 (선택 단계 일부 미실행 또는 부분 완료)'
      : '필수 정리 항목이 Backend 상태로 확인되었습니다'
  }

  return (
    <>
      <p className="panel-hint panel-hint--warn" role="status">
        {headline}
      </p>
      <ul>
        <li>
          API 활성 연결:{' '}
          {routesClear ? '없음' : `남아 있음 (${status.active_routes.length})`}
        </li>
        <li>
          모델 실제 실행 상태: {status.runtime_status}
          {runtimeStopped ? '' : ' — 중지가 확인되지 않음'}
        </li>
        <li>컨테이너 존재: {containerPresentLabel(status.container_present)}</li>
        <li>배포 퇴역: {retired ? `예 (${status.retired_at})` : '아니오'}</li>
        <li>
          캐시:{' '}
          {status.source_cache?.status || '연결 없음'}
          {purgeCompleted ? ' · 이번 절차에서 삭제 요청됨' : ''}
          {purgeSkipped ? ' · 선택 단계 건너뜀' : ''}
          {cacheMissing ? ' · MISSING/없음' : ''}
        </li>
        <li>
          모델 버전 보관:{' '}
          {archiveVersionDone || version?.archived_at
            ? '예 (Archived)'
            : '아니오 / 미실행'}
        </li>
        <li>
          모델 보관:{' '}
          {archiveModelDone || (model && !model.is_active)
            ? '예 (is_active=false)'
            : '아니오 / 미실행'}
        </li>
        <li>
          건너뛴 선택 단계:{' '}
          {[
            purgeSkipped ? '캐시 삭제' : null,
            !archiveVersionDone && !version?.archived_at ? '버전 보관' : null,
            !archiveModelDone && !(model && !model.is_active)
              ? '모델 보관'
              : null,
          ]
            .filter(Boolean)
            .join(', ') || '없음(또는 해당 없음)'}
        </li>
      </ul>
      {containerUnknown || !requiredDone ? (
        <p className="secondary-text">
          UNKNOWN·미확인·미완료 항목이 있어 「모든 정리 완료」로 표시하지 않습니다.
        </p>
      ) : null}
    </>
  )
}
