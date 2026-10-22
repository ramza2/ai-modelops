import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createIdempotencyKey,
  mutationPayloadFingerprint,
} from '../utils/idempotencyKey'

describe('idempotencyKey utils', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses randomUUID when available', () => {
    vi.stubGlobal('crypto', {
      randomUUID: () => '11111111-2222-4333-8444-555555555555',
    })
    expect(createIdempotencyKey()).toBe(
      '11111111-2222-4333-8444-555555555555',
    )
  })

  it('falls back to getRandomValues UUIDv4 on insecure origins', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = i
      return bytes
    })
    vi.stubGlobal('crypto', { getRandomValues })
    const key = createIdempotencyKey()
    expect(key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    expect(getRandomValues).toHaveBeenCalled()
  })

  it('throws when no secure RNG is available', () => {
    vi.stubGlobal('crypto', {})
    expect(() => createIdempotencyKey()).toThrow(/안전한 Idempotency-Key/)
  })

  it('fingerprints payloads stably', () => {
    expect(mutationPayloadFingerprint({ a: 1, b: null })).toBe(
      mutationPayloadFingerprint({ a: 1, b: null }),
    )
  })
})
