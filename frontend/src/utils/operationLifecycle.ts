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
