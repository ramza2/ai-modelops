import { describe, expect, it } from 'vitest'
import {
  formatMemoryMb,
  formatPowerW,
  formatTemperatureC,
  formatUtilizationPct,
  usagePercent,
} from '../utils/number'

describe('usagePercent', () => {
  it('returns null for missing values', () => {
    expect(usagePercent(null, 100)).toBeNull()
    expect(usagePercent(50, null)).toBeNull()
    expect(usagePercent(undefined, undefined)).toBeNull()
  })

  it('returns null for zero or negative denominator', () => {
    expect(usagePercent(10, 0)).toBeNull()
    expect(usagePercent(10, -1)).toBeNull()
  })

  it('computes normal percentage', () => {
    expect(usagePercent(50, 100)).toBe(50)
    expect(usagePercent(8192, 16384)).toBe(50)
  })

  it('clamps above 100 and below 0', () => {
    expect(usagePercent(150, 100)).toBe(100)
    expect(usagePercent(-10, 100)).toBe(0)
  })
})

describe('formatMemoryMb', () => {
  it('formats null as em dash', () => {
    expect(formatMemoryMb(null)).toBe('—')
    expect(formatMemoryMb(undefined)).toBe('—')
  })

  it('keeps small values in MB', () => {
    expect(formatMemoryMb(512)).toBe('512 MB')
  })

  it('formats GiB with 1024 divisor', () => {
    expect(formatMemoryMb(16384)).toBe('16.0 GiB')
    expect(formatMemoryMb(49152)).toBe('48.0 GiB')
  })
})

describe('formatTemperatureC / formatPowerW', () => {
  it('formats known values', () => {
    expect(formatTemperatureC(42)).toBe('42.0 °C')
    expect(formatPowerW(80.5)).toBe('80.5 W')
  })

  it('formats missing as em dash', () => {
    expect(formatTemperatureC(null)).toBe('—')
    expect(formatPowerW(Number.NaN)).toBe('—')
    expect(formatUtilizationPct(null)).toBe('—')
  })
})
