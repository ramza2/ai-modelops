import { describe, expect, it } from 'vitest'
import {
  parseOptionalPositiveLimit,
  parseOptionalPriority,
  runtimePolicyBodiesEqual,
} from '../utils/runtimePolicyForm'

describe('runtimePolicyForm parsers', () => {
  it('maps empty to null and accepts positive limits', () => {
    expect(parseOptionalPositiveLimit('', '입력')).toEqual({
      ok: true,
      value: null,
    })
    expect(parseOptionalPositiveLimit(' 8192 ', '입력')).toEqual({
      ok: true,
      value: 8192,
    })
  })

  it('rejects 0, decimals, scientific notation for limits', () => {
    expect(parseOptionalPositiveLimit('0', '입력').ok).toBe(false)
    expect(parseOptionalPositiveLimit('1.5', '입력').ok).toBe(false)
    expect(parseOptionalPositiveLimit('1e3', '입력').ok).toBe(false)
    expect(parseOptionalPositiveLimit('abc', '입력').ok).toBe(false)
  })

  it('allows priority 0 and distinguishes null', () => {
    expect(parseOptionalPriority('')).toEqual({ ok: true, value: null })
    expect(parseOptionalPriority('0')).toEqual({ ok: true, value: 0 })
    expect(parseOptionalPriority('-1')).toEqual({ ok: true, value: -1 })
    expect(parseOptionalPriority('1.0').ok).toBe(false)
  })

  it('compares policy bodies', () => {
    const a = {
      is_enabled: false,
      max_input_tokens: null,
      max_output_tokens: 10,
      max_concurrent_requests: null,
      priority: 0,
    }
    expect(runtimePolicyBodiesEqual(a, { ...a })).toBe(true)
    expect(
      runtimePolicyBodiesEqual(a, { ...a, priority: null }),
    ).toBe(false)
  })
})
