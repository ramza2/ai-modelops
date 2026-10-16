/** P1-4A: client-side lifecycle Operation polling helpers (no Backend changes). */

/** Mutable config so focused tests can shorten the interval without fake timers. */
export const operationPollConfig = {
  ms: 4000,
  /** Max time to keep polling Deployment after Operation SUCCEEDED. */
  healthWaitMaxMs: 5 * 60 * 1000,
}

export const OPERATION_POLL_MS = 4000
export const HEALTH_WAIT_MAX_MS = 5 * 60 * 1000

/** In-flight statuses that warrant automatic detail refresh. */
export const ACTIVE_OPERATION_STATUSES = new Set([
  'QUEUED',
  'RUNNING',
  'ROLLING_BACK',
])

const FAILED_OPERATION_STATUSES = new Set([
  'FAILED',
  'CANCELLED',
  'ROLLED_BACK',
  'MANUAL_INTERVENTION_REQUIRED',
])

export function isActiveOperationStatus(
  status: string | null | undefined,
): boolean {
  if (!status) return false
  return ACTIVE_OPERATION_STATUSES.has(status.toUpperCase())
}

export function isFailedOperationStatus(
  status: string | null | undefined,
): boolean {
  if (!status) return false
  return FAILED_OPERATION_STATUSES.has(status.toUpperCase())
}

/** Terminal Operation statuses where Switch Detail polling must stop. */
const TERMINAL_OPERATION_STATUSES = new Set([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'ROLLED_BACK',
  'MANUAL_INTERVENTION_REQUIRED',
])

export function isTerminalOperationStatus(
  status: string | null | undefined,
): boolean {
  if (!status) return false
  return TERMINAL_OPERATION_STATUSES.has(status.toUpperCase())
}

/** True when Operation is a SWITCH owned by the given Endpoint Alias id. */
export function isSwitchOperationForEndpoint(
  op:
    | {
        operation_type?: string | null
        endpoint_alias_id?: string | null
      }
    | null
    | undefined,
  endpointId: string,
): boolean {
  if (!op || !endpointId) return false
  return (
    (op.operation_type || '').toUpperCase() === 'SWITCH' &&
    op.endpoint_alias_id === endpointId
  )
}

export function isDeploymentReadyForPublish(
  dep:
    | {
        desired_state?: string | null
        runtime_status?: string | null
        health_status?: string | null
      }
    | null
    | undefined,
): boolean {
  if (!dep) return false
  return (
    dep.desired_state === 'RUNNING' &&
    dep.runtime_status === 'RUNNING' &&
    dep.health_status === 'HEALTHY'
  )
}

/**
 * Elapsed time since Operation finished_at for post-SUCCEEDED health wait.
 * Missing/invalid timestamps return null so F5 does not invent a new clock
 * and we do not false-timeout on bad data.
 *
 * Wizard Start health wait must keep this behavior — do not redirect null/future
 * timestamps through the STOP sync fallback clock below.
 */
export function healthWaitElapsedMsFromFinishedAt(
  finishedAt: string | null | undefined,
  nowMs: number = Date.now(),
): number | null {
  if (finishedAt == null) return null
  if (typeof finishedAt !== 'string') return null
  const trimmed = finishedAt.trim()
  if (!trimmed) return null
  const t = Date.parse(trimmed)
  if (!Number.isFinite(t)) return null
  return Math.max(0, nowMs - t)
}

/**
 * Parse finished_at only when it is usable as a STOP sync timeout anchor:
 * finite and not in the future (future stamps would keep elapsed at 0 forever).
 */
export function parseTrustworthyFinishedAtMs(
  finishedAt: string | null | undefined,
  nowMs: number = Date.now(),
): number | null {
  if (finishedAt == null) return null
  if (typeof finishedAt !== 'string') return null
  const trimmed = finishedAt.trim()
  if (!trimmed) return null
  const t = Date.parse(trimmed)
  if (!Number.isFinite(t)) return null
  if (t > nowMs) return null
  return t
}

/**
 * Per-Operation-ID fallback start times for STOP sync wait when finished_at is
 * missing, invalid, or in the future. Session-local only — not used by Wizard.
 */
export type StopSyncFallbackClock = {
  ensure(operationId: string, nowMs?: number): number
  get(operationId: string): number | undefined
  clear(operationId?: string): void
}

export function createStopSyncFallbackClock(): StopSyncFallbackClock {
  const starts = new Map<string, number>()
  return {
    ensure(operationId: string, nowMs: number = Date.now()): number {
      const existing = starts.get(operationId)
      if (existing != null) return existing
      starts.set(operationId, nowMs)
      return nowMs
    },
    get(operationId: string): number | undefined {
      return starts.get(operationId)
    },
    clear(operationId?: string): void {
      if (operationId == null) {
        starts.clear()
        return
      }
      starts.delete(operationId)
    },
  }
}

/**
 * Elapsed ms for Deployment Detail STOP status sync wait (P1-8G follow-up).
 * If a per-Operation fallback was already pinned (null/invalid/future at first
 * observation), keep that clock so a later-valid finished_at cannot shrink
 * elapsed or extend the wait cap. Otherwise prefer trustworthy finished_at.
 * Always returns a finite number when operationId is provided.
 */
export function stopSyncWaitElapsedMs(input: {
  operationId: string
  finishedAt: string | null | undefined
  fallback: StopSyncFallbackClock
  nowMs?: number
}): number {
  const nowMs = input.nowMs ?? Date.now()
  const pinned = input.fallback.get(input.operationId)
  if (pinned != null) {
    return Math.max(0, nowMs - pinned)
  }
  const trusted = parseTrustworthyFinishedAtMs(input.finishedAt, nowMs)
  if (trusted != null) {
    return Math.max(0, nowMs - trusted)
  }
  const start = input.fallback.ensure(input.operationId, nowMs)
  return Math.max(0, nowMs - start)
}

/** Cache Deploy Wizard Start-step lifecycle poll phases (P1-4B). */
export type WizardLifecyclePhase =
  | 'idle'
  | 'operation_active'
  | 'health_wait'
  | 'ready'
  | 'failed'
  | 'health_timeout'

export function resolveWizardLifecyclePhase(input: {
  step: string
  hasOperationId: boolean
  operationStatus: string | null | undefined
  deploymentReady: boolean
  healthWaitElapsedMs: number | null
  healthWaitMaxMs?: number
}): WizardLifecyclePhase {
  if (input.step !== 'start' || !input.hasOperationId) return 'idle'
  if (isFailedOperationStatus(input.operationStatus)) return 'failed'
  if (isActiveOperationStatus(input.operationStatus)) return 'operation_active'
  if (input.operationStatus === 'SUCCEEDED') {
    if (input.deploymentReady) return 'ready'
    const max = input.healthWaitMaxMs ?? operationPollConfig.healthWaitMaxMs
    if (
      input.healthWaitElapsedMs != null &&
      input.healthWaitElapsedMs >= max
    ) {
      return 'health_timeout'
    }
    return 'health_wait'
  }
  // Operation row present but status not yet loaded / unexpected.
  if (input.operationStatus) return 'operation_active'
  return 'idle'
}

export function shouldPollWizardLifecycle(
  phase: WizardLifecyclePhase,
): boolean {
  return phase === 'operation_active' || phase === 'health_wait'
}

/** True when Deployment runtime_status reports STOPPED (API value only). */
export function isDeploymentStoppedForSync(
  dep:
    | {
        runtime_status?: string | null
      }
    | null
    | undefined,
): boolean {
  if (!dep?.runtime_status) return false
  return dep.runtime_status.toUpperCase() === 'STOPPED'
}

/**
 * Deployment Detail lifecycle poll phases (P1-8G).
 * STOP SUCCEEDED may enter stop_wait until runtime_status is STOPPED
 * (or timeout). START/RESTART never invent a stop-sync wait.
 */
export type DetailLifecyclePhase =
  | 'idle'
  | 'operation_active'
  | 'stop_wait'
  | 'stopped'
  | 'failed'
  | 'stop_timeout'

export function resolveDetailLifecyclePhase(input: {
  operationType: string | null | undefined
  operationStatus: string | null | undefined
  hasOperationId: boolean
  deploymentStopped: boolean
  stopWaitElapsedMs: number | null
  healthWaitMaxMs?: number
}): DetailLifecyclePhase {
  if (!input.hasOperationId) return 'idle'
  if (isFailedOperationStatus(input.operationStatus)) return 'failed'
  if (isActiveOperationStatus(input.operationStatus)) return 'operation_active'

  const type = (input.operationType || '').toUpperCase()
  if (type === 'STOP' && input.operationStatus === 'SUCCEEDED') {
    if (input.deploymentStopped) return 'stopped'
    const max = input.healthWaitMaxMs ?? operationPollConfig.healthWaitMaxMs
    if (
      input.stopWaitElapsedMs != null &&
      input.stopWaitElapsedMs >= max
    ) {
      return 'stop_timeout'
    }
    return 'stop_wait'
  }

  // START/RESTART SUCCEEDED (or unknown) — no post-success Deployment wait here.
  return 'idle'
}

export function shouldPollDetailLifecycle(
  phase: DetailLifecyclePhase,
): boolean {
  return phase === 'operation_active' || phase === 'stop_wait'
}

export function formatOperationError(
  error: { code: string | null; message: string | null } | null | undefined,
): string {
  if (!error) return '—'
  const parts = [error.code, error.message].filter(
    (p): p is string => Boolean(p && p.trim()),
  )
  return parts.length > 0 ? parts.join(': ') : '—'
}

export type DetailFetchKind = 'load' | 'poll'

/**
 * Shared epoch for manual refresh and silent polling on the same detail page.
 * A newer begin() invalidates older in-flight responses even for the same ID.
 */
export function createDetailFetchGate() {
  let gen = 0
  let controller: AbortController | null = null
  let kind: DetailFetchKind | null = null

  return {
    begin(next: DetailFetchKind) {
      controller?.abort()
      controller = new AbortController()
      gen += 1
      kind = next
      return { gen, signal: controller.signal, kind: next }
    },
    isCurrent(token: number) {
      return token === gen
    },
    currentKind() {
      return kind
    },
    abort() {
      controller?.abort()
      controller = null
      gen += 1
      kind = null
    },
  }
}

export type DetailFetchGate = ReturnType<typeof createDetailFetchGate>
