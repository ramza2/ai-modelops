import { describe, expect, it } from 'vitest'
import { aggregateInvocations } from '../api/dashboard'

describe('aggregateInvocations', () => {
  it('sums additive fields and does not invent global p95', () => {
    const result = aggregateInvocations([
      {
        group_key: 'a',
        deployment_name: 'dep-a',
        request_count: 10,
        success_count: 8,
        error_count: 2,
        tokenized_request_count: 5,
        latency_ms_p95: 100,
        input_tokens_p95: 50,
      },
      {
        group_key: 'b',
        deployment_name: 'dep-b',
        request_count: 30,
        success_count: 30,
        error_count: 0,
        tokenized_request_count: 30,
        latency_ms_p95: 900,
        input_tokens_p95: 200,
      },
    ])
    expect(result.requestCount).toBe(40)
    expect(result.successCount).toBe(38)
    expect(result.errorCount).toBe(2)
    expect(result.successRate).toBeCloseTo(38 / 40)
    expect(result.tokenCoverage).toBeCloseTo(35 / 40)
    expect(result.topDeployments[0]?.group_key).toBe('b')
    expect(result.topDeployments).toHaveLength(2)
    // No global p95 field synthesized.
    expect(result).not.toHaveProperty('latency_ms_p95')
  })

  it('returns null rates for empty traffic', () => {
    const result = aggregateInvocations([])
    expect(result.successRate).toBeNull()
    expect(result.tokenCoverage).toBeNull()
    expect(result.requestCount).toBe(0)
  })
})
