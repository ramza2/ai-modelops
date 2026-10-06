export function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return new Intl.NumberFormat().format(value)
}

export function formatPercent(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${(value * 100).toFixed(digits)}%`
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${Math.round(value)} ms`
}

/** Format Backend MB values as MB or GiB (1024-based). */
export function formatMemoryMb(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  if (Math.abs(value) < 1024) {
    return `${Math.round(value)} MB`
  }
  return `${(value / 1024).toFixed(1)} GiB`
}

/**
 * Compute a 0..100 display percentage from used/total.
 * Returns null when unknown / invalid denominator.
 */
export function usagePercent(
  used: number | null | undefined,
  total: number | null | undefined,
): number | null {
  if (
    used === null ||
    used === undefined ||
    total === null ||
    total === undefined
  ) {
    return null
  }
  if (!Number.isFinite(used) || !Number.isFinite(total) || total <= 0) {
    return null
  }
  const raw = (used / total) * 100
  if (!Number.isFinite(raw)) return null
  return Math.min(100, Math.max(0, raw))
}

export function formatTemperatureC(
  value: number | null | undefined,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${value.toFixed(1)} °C`
}

export function formatPowerW(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${value.toFixed(1)} W`
}

export function formatUtilizationPct(
  value: number | null | undefined,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${value.toFixed(1)}%`
}
