/**
 * ClientApp ``client_key`` helpers for Admin registration UX.
 *
 * Format: ``svc-`` + 32 lowercase hex chars (128-bit via crypto.getRandomValues).
 * Not an API auth secret — Gateway optional identification (X-AI-Client).
 */

const HEX_BYTES = 16

/** ``svc-`` + 32 hex = 36 chars (under client_key max 120). */
export const CLIENT_SERVICE_KEY_PATTERN = /^svc-[0-9a-f]{32}$/

export function createClientServiceKey(): string {
  if (
    typeof crypto === 'undefined' ||
    typeof crypto.getRandomValues !== 'function'
  ) {
    throw new Error(
      '이 브라우저에서는 안전한 서비스 키를 만들 수 없습니다. 지원되는 브라우저에서 다시 시도하세요.',
    )
  }
  const bytes = crypto.getRandomValues(new Uint8Array(HEX_BYTES))
  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, '0'),
  ).join('')
  return `svc-${hex}`
}

export function formatXAiClientHeader(clientKey: string): string {
  return `X-AI-Client: ${clientKey}`
}

export function isClientServiceKeyFormat(value: string): boolean {
  return CLIENT_SERVICE_KEY_PATTERN.test(value)
}
