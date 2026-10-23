import type { InvocationSummaryItem, InvocationSummaryResponse } from '../api/types'

export const CLIENT_USAGE_HOURS = 24

export type ClientUsageStatus = 'ok' | 'none' | 'unavailable'

export type ClientUsageView = {
  status: ClientUsageStatus
  statusLabel: string
  requestCount: number | null
  successCount: number | null
  errorCount: number | null
  lastCalledAt: string | null
  errorRatePercent: number | null
}

const STATUS_LABEL: Record<ClientUsageStatus, string> = {
  ok: '최근 24시간 호출 확인',
  none: '최근 24시간 호출 없음',
  unavailable: '집계 확인 불가',
}

/** Build a client_key → summary item map (exact key match; ignores unknown). */
export function indexClientUsageByKey(
  summary: InvocationSummaryResponse | null | undefined,
): Map<string, InvocationSummaryItem> {
  const map = new Map<string, InvocationSummaryItem>()
  if (!summary?.items) return map
  for (const item of summary.items) {
    if (!item.group_key || item.group_key === 'unknown') continue
    map.set(item.group_key, item)
  }
  return map
}

export function resolveClientUsage(
  clientKey: string,
  usageByKey: Map<string, InvocationSummaryItem> | null,
  summaryFailed: boolean,
): ClientUsageView {
  if (summaryFailed || usageByKey === null) {
    return {
      status: 'unavailable',
      statusLabel: STATUS_LABEL.unavailable,
      requestCount: null,
      successCount: null,
      errorCount: null,
      lastCalledAt: null,
      errorRatePercent: null,
    }
  }
  const item = usageByKey.get(clientKey)
  const requestCount = item?.request_count ?? 0
  const successCount = item?.success_count ?? 0
  const errorCount = item?.error_count ?? 0
  if (requestCount <= 0) {
    return {
      status: 'none',
      statusLabel: STATUS_LABEL.none,
      requestCount: 0,
      successCount: 0,
      errorCount: 0,
      lastCalledAt: null,
      errorRatePercent: null,
    }
  }
  return {
    status: 'ok',
    statusLabel: STATUS_LABEL.ok,
    requestCount,
    successCount,
    errorCount,
    lastCalledAt: item?.last_called_at ?? null,
    errorRatePercent: (errorCount / requestCount) * 100,
  }
}

export function formatErrorRatePercent(
  rate: number | null | undefined,
): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return '—'
  return `${rate.toFixed(1)}%`
}
