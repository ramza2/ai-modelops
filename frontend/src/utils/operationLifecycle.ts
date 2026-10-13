/** P1-4A: client-side lifecycle Operation polling helpers (no Backend changes). */

/** Mutable config so focused tests can shorten the interval without fake timers. */
export const operationPollConfig = {
  ms: 4000,
}

export const OPERATION_POLL_MS = 4000

/** In-flight statuses that warrant automatic detail refresh. */
export const ACTIVE_OPERATION_STATUSES = new Set([
  'QUEUED',
  'RUNNING',
  'ROLLING_BACK',
])

export function isActiveOperationStatus(
  status: string | null | undefined,
): boolean {
  if (!status) return false
  return ACTIVE_OPERATION_STATUSES.has(status.toUpperCase())
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
