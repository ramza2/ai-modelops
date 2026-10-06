import { describe, expect, it } from 'vitest'
import { formatApiDateTime, parseApiDate } from '../utils/date'

describe('formatApiDateTime', () => {
  it('renders null/invalid as em dash', () => {
    expect(formatApiDateTime(null)).toBe('—')
    expect(formatApiDateTime(undefined)).toBe('—')
    expect(formatApiDateTime('not-a-date')).toBe('—')
  })

  it('uses Intl local timezone formatting (not raw ISO slice)', () => {
    const iso = '2026-10-06T01:02:03.000Z'
    const expected = new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(new Date(iso))
    expect(formatApiDateTime(iso)).toBe(expected)
    // Must not be the naive UTC clock slice.
    expect(formatApiDateTime(iso)).not.toBe('2026-10-06 01:02:03')
    expect(parseApiDate(iso)?.toISOString()).toBe(iso)
  })
})
