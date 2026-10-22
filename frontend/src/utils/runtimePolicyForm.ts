/**
 * Parse Runtime Policy form fields for Management API PUT full replacement.
 * Empty → null. Reject decimals / scientific notation / NaN / Infinity.
 */

const INT32_MIN = -2147483648
const INT32_MAX = 2147483647
const POSITIVE_MAX = 2147483647

const STRICT_INT_RE = /^-?\d+$/

export type ParseResult =
  | { ok: true; value: number | null }
  | { ok: false; error: string }

function rejectNonStrictInteger(raw: string, label: string): string | null {
  const trimmed = raw.trim()
  if (trimmed === '') return null
  if (/[eE.]/.test(trimmed) || !STRICT_INT_RE.test(trimmed)) {
    return `${label}은(는) 정수만 입력할 수 있습니다. 소수·지수 표기·문자는 사용할 수 없습니다.`
  }
  return null
}

/** Optional positive limit: empty→null, else 1..2147483647. */
export function parseOptionalPositiveLimit(
  raw: string,
  label: string,
): ParseResult {
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: true, value: null }
  const bad = rejectNonStrictInteger(trimmed, label)
  if (bad) return { ok: false, error: bad }
  const n = Number(trimmed)
  if (!Number.isSafeInteger(n) || !Number.isFinite(n)) {
    return { ok: false, error: `${label}은(는) 유효한 정수가 아닙니다.` }
  }
  if (n < 1 || n > POSITIVE_MAX) {
    return {
      ok: false,
      error: `${label}은(는) 1 이상 ${POSITIVE_MAX} 이하여야 합니다. 0은 사용할 수 없습니다(제한 없음은 빈 칸).`,
    }
  }
  return { ok: true, value: n }
}

/** Optional priority: empty→null, else PostgreSQL INT range (incl. 0). */
export function parseOptionalPriority(raw: string): ParseResult {
  const label = '요청 우선순위'
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: true, value: null }
  const bad = rejectNonStrictInteger(trimmed, label)
  if (bad) return { ok: false, error: bad }
  const n = Number(trimmed)
  if (!Number.isSafeInteger(n) || !Number.isFinite(n)) {
    return { ok: false, error: `${label}은(는) 유효한 정수가 아닙니다.` }
  }
  if (n < INT32_MIN || n > INT32_MAX) {
    return {
      ok: false,
      error: `${label}은(는) ${INT32_MIN} ~ ${INT32_MAX} 범위의 정수여야 합니다.`,
    }
  }
  return { ok: true, value: n }
}

export type RuntimePolicyFormBody = {
  is_enabled: boolean
  max_input_tokens: number | null
  max_output_tokens: number | null
  max_concurrent_requests: number | null
  priority: number | null
}

export function runtimePolicyBodiesEqual(
  a: RuntimePolicyFormBody,
  b: RuntimePolicyFormBody,
): boolean {
  return (
    a.is_enabled === b.is_enabled &&
    a.max_input_tokens === b.max_input_tokens &&
    a.max_output_tokens === b.max_output_tokens &&
    a.max_concurrent_requests === b.max_concurrent_requests &&
    a.priority === b.priority
  )
}

export function formatPolicyScalar(value: number | null | boolean): string {
  if (typeof value === 'boolean') return value ? '사용' : '사용 안함'
  if (value === null) return 'null (제한 없음 / 미설정)'
  return String(value)
}

export function formatPriorityCompare(value: number | null): string {
  if (value === null) return 'null (미설정 — 제한/전달 의도 없음)'
  if (value === 0) return '0 (중립값 — null과 다름)'
  return `${value} (숫자가 작을수록 높은 우선순위 의도; 비0은 신뢰된 scheduler 필요 가능)`
}
