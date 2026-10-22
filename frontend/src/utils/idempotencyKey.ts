/**
 * Cryptographically secure Idempotency-Key for Management API mutations.
 *
 * Prefer ``crypto.randomUUID`` (HTTPS / secure contexts). On LAN HTTP Admin
 * origins where randomUUID is missing, fall back to ``crypto.getRandomValues``
 * UUIDv4. Do not use Math.random or timestamps.
 */
export function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  if (
    typeof crypto !== 'undefined' &&
    typeof crypto.getRandomValues === 'function'
  ) {
    const bytes = crypto.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (byte) =>
      byte.toString(16).padStart(2, '0'),
    ).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  throw new Error(
    '이 브라우저에서는 안전한 Idempotency-Key를 만들 수 없습니다. 지원되는 브라우저에서 다시 시도하세요.',
  )
}

/** Stable fingerprint so the same logical payload reuses one Idempotency-Key. */
export function mutationPayloadFingerprint(payload: unknown): string {
  return JSON.stringify(payload)
}
