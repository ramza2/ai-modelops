import { describe, expect, it } from 'vitest'
import {
  abbreviateMiddle,
  formatBytes,
  pickRuntimeConfigEntries,
  safeRuntimeConfigValue,
} from '../utils/format'
import { parsePositivePage } from '../utils/query'

describe('formatBytes', () => {
  it.each([
    [null, '—'],
    [0, '0 B'],
    [512, '512 B'],
    [1048576, '1.0 MiB'],
    [1073741824, '1.0 GiB'],
  ] as const)('formats %s → %s', (raw, expected) => {
    expect(formatBytes(raw)).toBe(expected)
  })
})

describe('abbreviateMiddle', () => {
  it('keeps short values', () => {
    expect(abbreviateMiddle('abc')).toBe('abc')
  })
  it('abbreviates long checksums', () => {
    expect(abbreviateMiddle('abcdef1234567890zzzz', 8, 4)).toBe(
      'abcdef12…zzzz',
    )
  })
  it('handles null', () => {
    expect(abbreviateMiddle(null)).toBe('—')
  })
})

describe('safeRuntimeConfigValue', () => {
  it('formats scalars safely', () => {
    expect(safeRuntimeConfigValue(null)).toBe('—')
    expect(safeRuntimeConfigValue('  x  ')).toBe('x')
    expect(safeRuntimeConfigValue(0.9)).toBe('0.9')
    expect(safeRuntimeConfigValue(true)).toBe('true')
    expect(safeRuntimeConfigValue(false)).toBe('false')
  })
  it('rejects complex values', () => {
    expect(safeRuntimeConfigValue({ a: 1 })).toBe('유효하지 않은 복합 값')
    expect(safeRuntimeConfigValue([1, 2])).toBe('유효하지 않은 복합 값')
  })
})

describe('pickRuntimeConfigEntries', () => {
  it('keeps allowlist and counts others', () => {
    const { known, otherCount } = pickRuntimeConfigEntries({
      max_num_seqs: 4,
      secret_or_unknown: 'x',
      another: 1,
    })
    expect(known.map((k) => k.key)).toEqual(['max_num_seqs'])
    expect(otherCount).toBe(2)
  })
})

describe('parsePositivePage shared util', () => {
  it('rejects parseInt-style prefixes', () => {
    expect(parsePositivePage('2abc')).toBe(1)
    expect(parsePositivePage('2.5')).toBe(1)
  })
})
