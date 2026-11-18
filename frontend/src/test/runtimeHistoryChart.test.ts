import { describe, expect, it } from 'vitest'
import type { RuntimeSnapshot } from '../api/types'
import {
  buildMetricSegments,
  canDrawTimeline,
  computeGapThresholdMs,
  kvRatioToPercent,
  measuredNonNegative,
  RUNTIME_HISTORY_MIN_GAP_MS,
  RUNTIME_HISTORY_DEFAULT_GAP_MS,
  runtimeInstanceKey,
  toTimedSamples,
} from '../utils/runtimeHistoryChart'

function snap(
  overrides: Partial<RuntimeSnapshot> & { sampled_at: string | null },
): RuntimeSnapshot {
  return {
    deployment_id: 'dep-a',
    availability: 'AVAILABLE',
    kv_cache_usage_ratio: 0.2,
    num_requests_running: 1,
    num_requests_waiting: 0,
    prompt_tokens_total: 1,
    generation_tokens_total: 1,
    error_code: null,
    error_message: null,
    runtime_instance: {
      container_id: 'ctr-a',
      started_at: '2026-10-06T00:00:00Z',
      restart_count: 0,
    },
    ...overrides,
  }
}

describe('runtimeHistoryChart helpers (UX20-C1)', () => {
  it('distinguishes 0 from null and rejects non-finite/negative', () => {
    expect(measuredNonNegative(0)).toBe(0)
    expect(measuredNonNegative(null)).toBeNull()
    expect(measuredNonNegative(undefined)).toBeNull()
    expect(measuredNonNegative(Number.NaN)).toBeNull()
    expect(measuredNonNegative(Number.POSITIVE_INFINITY)).toBeNull()
    expect(measuredNonNegative(-1)).toBeNull()
    expect(kvRatioToPercent(0)).toBe(0)
    expect(kvRatioToPercent(0.25)).toBe(25)
    expect(kvRatioToPercent(1)).toBe(100)
    expect(kvRatioToPercent(1.1)).toBeNull()
    expect(kvRatioToPercent(null)).toBeNull()
  })

  it('excludes rows without sampled_at from timed samples but keeps order', () => {
    const samples = toTimedSamples([
      snap({ sampled_at: '2026-10-07T02:00:00Z', kv_cache_usage_ratio: 0.3 }),
      snap({ sampled_at: null, kv_cache_usage_ratio: 0.9 }),
      snap({ sampled_at: '2026-10-07T01:00:00Z', kv_cache_usage_ratio: 0.1 }),
      snap({ sampled_at: 'not-a-date', kv_cache_usage_ratio: 0.5 }),
    ])
    expect(samples.map((s) => s.sampledAt)).toEqual([
      '2026-10-07T01:00:00Z',
      '2026-10-07T02:00:00Z',
    ])
    expect(samples[0]?.kvPercent).toBe(10)
  })

  it('breaks segments on null values without inventing points', () => {
    const samples = toTimedSamples([
      snap({
        sampled_at: '2026-10-07T00:00:00Z',
        num_requests_running: 1,
      }),
      snap({
        sampled_at: '2026-10-07T00:01:00Z',
        num_requests_running: null,
      }),
      snap({
        sampled_at: '2026-10-07T00:02:00Z',
        num_requests_running: 2,
      }),
    ])
    const segs = buildMetricSegments(samples, 'running')
    expect(segs).toHaveLength(2)
    expect(segs[0]?.map((p) => p.value)).toEqual([1])
    expect(segs[1]?.map((p) => p.value)).toEqual([2])
    // Two measured points remain plottable as disconnected markers (not one false line).
    expect(canDrawTimeline(segs)).toBe(true)
  })

  it('breaks on runtime instance identity change even with dense samples', () => {
    const samples = toTimedSamples([
      snap({
        sampled_at: '2026-10-07T00:00:00Z',
        kv_cache_usage_ratio: 0.1,
        runtime_instance: {
          container_id: 'ctr-a',
          started_at: '2026-10-06T00:00:00Z',
          restart_count: 0,
        },
      }),
      snap({
        sampled_at: '2026-10-07T00:01:00Z',
        kv_cache_usage_ratio: 0.2,
        runtime_instance: {
          container_id: 'ctr-b',
          started_at: '2026-10-07T00:00:30Z',
          restart_count: 1,
        },
      }),
      snap({
        sampled_at: '2026-10-07T00:02:00Z',
        kv_cache_usage_ratio: 0.3,
        runtime_instance: {
          container_id: 'ctr-b',
          started_at: '2026-10-07T00:00:30Z',
          restart_count: 1,
        },
      }),
    ])
    expect(samples[0]?.instanceKey).not.toBe(samples[1]?.instanceKey)
    expect(runtimeInstanceKey({ runtime_instance: null })).toBe('unknown')
    const segs = buildMetricSegments(samples, 'kvPercent')
    expect(segs).toHaveLength(2)
    expect(segs[0]).toHaveLength(1)
    expect(segs[1]).toHaveLength(2)
    expect(canDrawTimeline(segs)).toBe(true)
  })

  it('breaks on long observation gaps using adaptive threshold', () => {
    const samples = toTimedSamples([
      snap({ sampled_at: '2026-10-07T00:00:00Z', num_requests_waiting: 0 }),
      snap({ sampled_at: '2026-10-07T00:01:00Z', num_requests_waiting: 1 }),
      snap({ sampled_at: '2026-10-07T00:02:00Z', num_requests_waiting: 1 }),
      // 1h gap >> 3×1min median and MIN_GAP
      snap({ sampled_at: '2026-10-07T01:02:00Z', num_requests_waiting: 2 }),
      snap({ sampled_at: '2026-10-07T01:03:00Z', num_requests_waiting: 3 }),
    ])
    const gap = computeGapThresholdMs(samples.map((s) => s.t))
    expect(gap).toBeGreaterThanOrEqual(RUNTIME_HISTORY_MIN_GAP_MS)
    const segs = buildMetricSegments(samples, 'waiting', gap)
    expect(segs).toHaveLength(2)
    expect(segs[0]?.map((p) => p.value)).toEqual([0, 1, 1])
    expect(segs[1]?.map((p) => p.value)).toEqual([2, 3])
  })

  it('breaks on restart count change even when container ID is unchanged', () => {
    const samples = toTimedSamples([
      snap({
        sampled_at: '2026-10-07T00:00:00Z',
        runtime_instance: {
          container_id: 'ctr-a',
          started_at: '2026-10-06T00:00:00Z',
          restart_count: 0,
        },
      }),
      snap({
        sampled_at: '2026-10-07T00:01:00Z',
        runtime_instance: {
          container_id: 'ctr-a',
          started_at: '2026-10-06T00:00:00Z',
          restart_count: 1,
        },
      }),
    ])
    expect(samples[0]?.instanceKey).not.toBe(samples[1]?.instanceKey)
    expect(buildMetricSegments(samples, 'running')).toHaveLength(2)
  })

  it('breaks sparse long gaps instead of using them as the sampling cadence', () => {
    const start = Date.parse('2026-10-07T00:00:00Z')
    expect(computeGapThresholdMs([start, start + 60 * 60_000])).toBe(
      RUNTIME_HISTORY_DEFAULT_GAP_MS,
    )
    expect(computeGapThresholdMs([
      start,
      start + 60_000,
      start + 61 * 60_000,
    ])).toBe(RUNTIME_HISTORY_MIN_GAP_MS)

    const samples = toTimedSamples([
      snap({ sampled_at: '2026-10-07T00:00:00Z' }),
      snap({ sampled_at: '2026-10-07T00:01:00Z' }),
      snap({ sampled_at: '2026-10-07T01:01:00Z' }),
    ])
    expect(buildMetricSegments(samples, 'running')).toHaveLength(2)
  })

  it('keeps a continuous segment for dense same-instance measured zeros', () => {
    const samples = toTimedSamples([
      snap({
        sampled_at: '2026-10-07T00:00:00Z',
        num_requests_running: 0,
        num_requests_waiting: 0,
      }),
      snap({
        sampled_at: '2026-10-07T00:01:00Z',
        num_requests_running: 0,
        num_requests_waiting: 0,
      }),
      snap({
        sampled_at: '2026-10-07T00:02:00Z',
        num_requests_running: 1,
        num_requests_waiting: 0,
      }),
    ])
    const running = buildMetricSegments(samples, 'running')
    expect(running).toHaveLength(1)
    expect(running[0]?.map((p) => p.value)).toEqual([0, 0, 1])
    expect(canDrawTimeline(running)).toBe(true)
  })
})
