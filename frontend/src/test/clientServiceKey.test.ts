import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CLIENT_SERVICE_KEY_PATTERN,
  createClientServiceKey,
  formatXAiClientHeader,
  isClientServiceKeyFormat,
} from '../utils/clientServiceKey'

describe('clientServiceKey utils', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('creates svc- + 32 lowercase hex via getRandomValues', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = (i * 17) & 0xff
      return bytes
    })
    vi.stubGlobal('crypto', { getRandomValues })
    const key = createClientServiceKey()
    expect(key).toMatch(CLIENT_SERVICE_KEY_PATTERN)
    expect(key).toBe('svc-00112233445566778899aabbccddeeff')
    expect(key.length).toBe(36)
    expect(getRandomValues).toHaveBeenCalledTimes(1)
    const arg = getRandomValues.mock.calls[0]![0] as Uint8Array
    expect(arg).toHaveLength(16)
  })

  it('throws when getRandomValues is unavailable (LAN-safe fail-closed)', () => {
    vi.stubGlobal('crypto', {})
    expect(() => createClientServiceKey()).toThrow(/안전한 서비스 키/)
  })

  it('does not use Math.random', () => {
    const mathSpy = vi.spyOn(Math, 'random')
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.fill(1)
      return bytes
    })
    vi.stubGlobal('crypto', { getRandomValues })
    createClientServiceKey()
    expect(mathSpy).not.toHaveBeenCalled()
  })

  it('formats X-AI-Client header and validates format', () => {
    expect(formatXAiClientHeader('svc-abc')).toBe('X-AI-Client: svc-abc')
    expect(isClientServiceKeyFormat(`svc-${'a'.repeat(32)}`)).toBe(true)
    expect(isClientServiceKeyFormat('AlZi')).toBe(false)
  })
})
