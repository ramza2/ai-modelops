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
