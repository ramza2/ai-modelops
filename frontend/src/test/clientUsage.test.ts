import { describe, expect, it } from 'vitest'
import type { InvocationSummaryItem, InvocationSummaryResponse } from '../api/types'
import {
  formatErrorRatePercent,
  indexClientUsageByKey,
  resolveClientUsage,
} from '../utils/clientUsage'

function item(
  overrides: Partial<InvocationSummaryItem> & Pick<InvocationSummaryItem, 'group_key'>,
): InvocationSummaryItem {
  return {
    request_count: 0,
    success_count: 0,
    error_count: 0,
    tokenized_request_count: 0,
    ...overrides,
  }
}

describe('clientUsage helpers', () => {
  it('indexes by exact group_key and skips unknown', () => {
    const summary: InvocationSummaryResponse = {
      hours: 24,
      group_by: 'client',
      items: [
        item({
          group_key: 'svc-a',
          request_count: 3,
          success_count: 2,
          error_count: 1,
          last_called_at: '2026-10-23T01:00:00Z',
        }),
        item({ group_key: 'unknown', request_count: 9 }),
      ],
    }
    const map = indexClientUsageByKey(summary)
    expect(map.has('svc-a')).toBe(true)
    expect(map.has('unknown')).toBe(false)
    expect(map.get('svc-a')?.request_count).toBe(3)
  })

  it('resolves ok / none / unavailable distinctly', () => {
    const map = indexClientUsageByKey({
      hours: 24,
      group_by: 'client',
      items: [
        item({
          group_key: 'hit',
          request_count: 10,
          success_count: 8,
          error_count: 2,
          last_called_at: '2026-10-23T02:00:00Z',
        }),
      ],
    })
    const ok = resolveClientUsage('hit', map, false)
    expect(ok.status).toBe('ok')
    expect(ok.statusLabel).toBe('최근 24시간 호출 확인')
    expect(ok.errorRatePercent).toBeCloseTo(20)
    expect(ok.lastCalledAt).toBe('2026-10-23T02:00:00Z')

    const none = resolveClientUsage('silent', map, false)
    expect(none.status).toBe('none')
    expect(none.statusLabel).toBe('최근 24시간 호출 없음')
    expect(none.requestCount).toBe(0)
    expect(none.lastCalledAt).toBeNull()
    expect(none.errorRatePercent).toBeNull()

    const failed = resolveClientUsage('hit', null, true)
    expect(failed.status).toBe('unavailable')
    expect(failed.statusLabel).toBe('집계 확인 불가')
    expect(failed.requestCount).toBeNull()
  })

  it('formats error rate without inventing 0% for empty windows', () => {
    expect(formatErrorRatePercent(null)).toBe('—')
    expect(formatErrorRatePercent(12.345)).toBe('12.3%')
  })
})
