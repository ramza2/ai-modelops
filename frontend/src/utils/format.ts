/** Format raw byte sizes with binary units. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  if (value < 0) return '—'
  if (value < 1024) return `${Math.round(value)} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GiB`
}

/** Abbreviate long strings with a middle ellipsis; short values unchanged. */
export function abbreviateMiddle(
  value: string | null | undefined,
  head = 8,
  tail = 4,
): string {
  if (value === null || value === undefined || value === '') return '—'
  if (value.length <= head + tail + 1) return value
  return `${value.slice(0, head)}…${value.slice(-tail)}`
}

/**
 * Safe display for allowlisted runtime_config scalars.
 * Objects/arrays are rejected rather than stringified as [object Object].
 */
export function safeRuntimeConfigValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') {
    const trimmed = value.trim()
    return trimmed === '' ? '—' : trimmed
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : '유효하지 않은 값'
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false'
  }
  if (typeof value === 'object') {
    return '유효하지 않은 복합 값'
  }
  return '유효하지 않은 값'
}

export const RUNTIME_CONFIG_ALLOWLIST = [
  'max_model_len',
  'max_num_seqs',
  'tensor_parallel_size',
  'gpu_memory_utilization',
  'dtype',
  'quantization',
  'scheduling_policy',
] as const

export type RuntimeConfigKey = (typeof RUNTIME_CONFIG_ALLOWLIST)[number]

export function pickRuntimeConfigEntries(
  config: Record<string, unknown> | null | undefined,
): {
  known: Array<{ key: RuntimeConfigKey; value: unknown }>
  otherCount: number
} {
  const source = config && typeof config === 'object' ? config : {}
  const known: Array<{ key: RuntimeConfigKey; value: unknown }> = []
  for (const key of RUNTIME_CONFIG_ALLOWLIST) {
    if (Object.prototype.hasOwnProperty.call(source, key)) {
      known.push({ key, value: source[key] })
    }
  }
  const allow = new Set<string>(RUNTIME_CONFIG_ALLOWLIST)
  const otherCount = Object.keys(source).filter((k) => !allow.has(k)).length
  return { known, otherCount }
}
