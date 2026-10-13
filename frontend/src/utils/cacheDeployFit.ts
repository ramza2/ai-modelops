import type { CacheDeployRuntimeConfig } from '../api/cacheDeploy'

// Fingerprint only what the operator sees as fit-relevant. Runtime length/seqs
// currently do not enter the backend estimate; changing them still invalidates
// a displayed FIT so the operator explicitly rechecks the estimate.
export function fitInputFingerprint(
  gpuIds: readonly string[],
  expectedVramMb: string,
  runtime: CacheDeployRuntimeConfig,
): string {
  return JSON.stringify({
    gpuIds,
    expectedVramMb: expectedVramMb.trim(),
    dtype: runtime.dtype ?? null,
    quantization: runtime.quantization ?? null,
    maxModelLen: runtime.max_model_len ?? null,
    maxNumSeqs: runtime.max_num_seqs ?? null,
    memoryUtilization: runtime.gpu_memory_utilization ?? null,
  })
}

// Never silently render an old persisted resource snapshot as live capacity.
export function isFreshResourceSnapshot(
  sampledAt: string | null | undefined,
  nowMs = Date.now(),
  maxAgeMs = 30_000,
): boolean {
  if (!sampledAt) return false
  const sampledMs = Date.parse(sampledAt)
  return Number.isFinite(sampledMs) &&
    sampledMs <= nowMs + 5_000 &&
    nowMs - sampledMs <= maxAgeMs
}

/** Live UNKNOWN(null) must never be replaced with a persisted snapshot value. */
export function displayedGpuFreeMb(
  live: { vram_free_mb: number | null } | undefined,
  snapshot: { vram_free_mb: number | null; sampled_at: string | null } | null | undefined,
  nowMs = Date.now(),
): number | null {
  if (live !== undefined) return live.vram_free_mb
  return isFreshResourceSnapshot(snapshot?.sampled_at, nowMs)
    ? (snapshot?.vram_free_mb ?? null)
    : null
}
