import type { RuntimeSnapshot } from '../api/types'
import { parseApiDate } from './date'

/** Default when fewer than two positive intervals exist. */
export const RUNTIME_HISTORY_DEFAULT_GAP_MS = 15 * 60 * 1000
/** Floor for adaptive gap (3× median interval). */
export const RUNTIME_HISTORY_MIN_GAP_MS = 5 * 60 * 1000

export type TimedSample = {
  sampledAt: string
  t: number
  instanceKey: string
  kvPercent: number | null
  running: number | null
  waiting: number | null
}

export type ChartPoint = {
  sampledAt: string
  t: number
  value: number
  instanceKey: string
}

export type ChartSegment = ChartPoint[]

export function runtimeInstanceKey(
  snap: Pick<RuntimeSnapshot, 'runtime_instance'>,
): string {
  const ri = snap.runtime_instance
  if (!ri) return 'unknown'
  return `${ri.container_id ?? ''}|${ri.started_at ?? ''}`
}

/** Measured non-negative finite values only; null/NaN/Inf/negative excluded. */
export function measuredNonNegative(
  value: number | null | undefined,
): number | null {
  if (value === null || value === undefined) return null
  if (!Number.isFinite(value) || value < 0) return null
  return value
}

/** KV ratio 0–1 → display percent; invalid ratios stay null (not synthesized). */
export function kvRatioToPercent(
  ratio: number | null | undefined,
): number | null {
  const v = measuredNonNegative(ratio)
  if (v === null) return null
  return v * 100
}

export function toTimedSamples(items: RuntimeSnapshot[]): TimedSample[] {
  const out: TimedSample[] = []
  for (const item of items) {
    const sampledAt = item.sampled_at
    if (!sampledAt) continue
    const d = parseApiDate(sampledAt)
    if (!d) continue
    out.push({
      sampledAt,
      t: d.getTime(),
      instanceKey: runtimeInstanceKey(item),
      kvPercent: kvRatioToPercent(item.kv_cache_usage_ratio),
      running: measuredNonNegative(item.num_requests_running),
      waiting: measuredNonNegative(item.num_requests_waiting),
    })
  }
  out.sort((a, b) => a.t - b.t || a.sampledAt.localeCompare(b.sampledAt))
  return out
}

export function computeGapThresholdMs(timesAsc: number[]): number {
  if (timesAsc.length < 2) return RUNTIME_HISTORY_DEFAULT_GAP_MS
  const deltas: number[] = []
  for (let i = 1; i < timesAsc.length; i += 1) {
    const d = timesAsc[i]! - timesAsc[i - 1]!
    if (d > 0) deltas.push(d)
  }
  if (deltas.length === 0) return RUNTIME_HISTORY_DEFAULT_GAP_MS
  deltas.sort((a, b) => a - b)
  const median = deltas[Math.floor(deltas.length / 2)]!
  return Math.max(median * 3, RUNTIME_HISTORY_MIN_GAP_MS)
}

function pickValue(
  sample: TimedSample,
  metric: 'kvPercent' | 'running' | 'waiting',
): number | null {
  return sample[metric]
}

/**
 * Build polyline segments for one metric.
 * Breaks on: null value, runtime instance identity change, long observation gap.
 * Does not interpolate across breaks; does not invent values.
 */
export function buildMetricSegments(
  samples: TimedSample[],
  metric: 'kvPercent' | 'running' | 'waiting',
  gapMs?: number,
): ChartSegment[] {
  const threshold =
    gapMs ?? computeGapThresholdMs(samples.map((s) => s.t))
  const segments: ChartSegment[] = []
  let current: ChartPoint[] = []

  const flush = () => {
    if (current.length > 0) {
      segments.push(current)
      current = []
    }
  }

  for (const sample of samples) {
    const value = pickValue(sample, metric)
    if (value === null) {
      flush()
      continue
    }
    const point: ChartPoint = {
      sampledAt: sample.sampledAt,
      t: sample.t,
      value,
      instanceKey: sample.instanceKey,
    }
    if (current.length > 0) {
      const prev = current[current.length - 1]!
      if (
        prev.instanceKey !== point.instanceKey ||
        point.t - prev.t > threshold
      ) {
        flush()
      }
    }
    current.push(point)
  }
  flush()
  return segments
}

export function countPlottablePoints(segments: ChartSegment[]): number {
  return segments.reduce((n, seg) => n + seg.length, 0)
}

export function canDrawTimeline(segments: ChartSegment[]): boolean {
  return countPlottablePoints(segments) >= 2
}
