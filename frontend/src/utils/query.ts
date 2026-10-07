/**
 * Strict positive page query parser.
 * Accepts only decimal digit strings that form a safe integer >= 1.
 */
export function parsePositivePage(raw: string | null | undefined): number {
  if (!raw) return 1
  if (!/^\d+$/.test(raw)) return 1
  const n = Number(raw)
  if (!Number.isSafeInteger(n) || n < 1) return 1
  return n
}

/** Canonical page query value for URLSearchParams (omit page 1). */
export function pageQueryValue(page: number): string | null {
  return page > 1 ? String(page) : null
}

/**
 * Bounded positive integer query parser (decimal digits only).
 * Out-of-range / invalid values fall back to `defaultValue`.
 */
export function parseBoundedInt(
  raw: string | null | undefined,
  opts: { min: number; max: number; defaultValue: number },
): number {
  if (!raw) return opts.defaultValue
  if (!/^\d+$/.test(raw)) return opts.defaultValue
  const n = Number(raw)
  if (!Number.isSafeInteger(n)) return opts.defaultValue
  if (n < opts.min || n > opts.max) return opts.defaultValue
  return n
}
