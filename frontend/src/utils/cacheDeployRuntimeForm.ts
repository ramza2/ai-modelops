import type { CacheDeployRuntimeConfig } from '../api/cacheDeploy'

export type RuntimeFormFieldErrors = {
  runtimePort?: string
  max_model_len?: string
  max_num_seqs?: string
  gpu_memory_utilization?: string
}

/** Keys operators see in the Runtime Review summary (create payload shape). */
export const RUNTIME_REVIEW_KEYS = [
  'max_model_len',
  'max_num_seqs',
  'gpu_memory_utilization',
  'runner',
  'dtype',
  'quantization',
  'reasoning_parser',
  'vllm_model_runner',
  'enable_auto_tool_choice',
  'tool_call_parser',
  'probe_type',
  'health_path',
  'tensor_parallel_size',
  'scheduling_policy',
] as const

export type RuntimeReviewKey = (typeof RUNTIME_REVIEW_KEYS)[number]

export function validateRuntimePort(port: number): string | undefined {
  if (
    typeof port !== 'number' ||
    !Number.isFinite(port) ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535
  ) {
    return '1–65535 사이의 정수여야 합니다.'
  }
  return undefined
}

export function validateOptionalPositiveInt(
  value: number | null | undefined,
  label: string,
): string | undefined {
  if (value == null) return undefined
  if (
    typeof value !== 'number' ||
    !Number.isFinite(value) ||
    !Number.isInteger(value) ||
    value < 1
  ) {
    return `${label}은(는) 미설정이거나 양의 정수여야 합니다.`
  }
  return undefined
}

export function validateOptionalGpuMemoryUtilization(
  value: number | null | undefined,
): string | undefined {
  if (value == null) return undefined
  if (
    typeof value !== 'number' ||
    !Number.isFinite(value) ||
    value <= 0 ||
    value > 1
  ) {
    return '미설정이거나 0 초과 1 이하 값이어야 합니다.'
  }
  return undefined
}

export function collectRuntimeFormErrors(
  runtimePort: number,
  runtime: CacheDeployRuntimeConfig,
): RuntimeFormFieldErrors {
  const errors: RuntimeFormFieldErrors = {}
  const portErr = validateRuntimePort(runtimePort)
  if (portErr) errors.runtimePort = portErr
  const lenErr = validateOptionalPositiveInt(
    runtime.max_model_len,
    'max_model_len',
  )
  if (lenErr) errors.max_model_len = lenErr
  const seqsErr = validateOptionalPositiveInt(
    runtime.max_num_seqs,
    'max_num_seqs',
  )
  if (seqsErr) errors.max_num_seqs = seqsErr
  const utilErr = validateOptionalGpuMemoryUtilization(
    runtime.gpu_memory_utilization,
  )
  if (utilErr) errors.gpu_memory_utilization = utilErr
  return errors
}

export function hasRuntimeFormErrors(errors: RuntimeFormFieldErrors): boolean {
  return (
    errors.runtimePort != null ||
    errors.max_model_len != null ||
    errors.max_num_seqs != null ||
    errors.gpu_memory_utilization != null
  )
}

/**
 * Advisory name hint only — not a compatibility matrix.
 * Used to warn when qwen3 parser is selected on a non-obvious repository.
 */
export function repositorySuggestsQwen3(
  ...ids: Array<string | null | undefined>
): boolean {
  return ids.some((id) => (id || '').toLowerCase().includes('qwen3'))
}

function isUnsetRuntimeValue(value: unknown): boolean {
  return value == null || value === ''
}

export type RuntimeReviewPartition = {
  applied: Array<{ key: RuntimeReviewKey; value: string }>
  unset: RuntimeReviewKey[]
}

/** Partition runtime_config for Review: values sent vs omitted (default behavior). */
export function partitionRuntimeReview(
  runtime: CacheDeployRuntimeConfig,
  tensorParallelSize: number,
): RuntimeReviewPartition {
  const effective: CacheDeployRuntimeConfig = {
    ...runtime,
    tensor_parallel_size: tensorParallelSize,
  }
  const applied: Array<{ key: RuntimeReviewKey; value: string }> = []
  const unset: RuntimeReviewKey[] = []
  for (const key of RUNTIME_REVIEW_KEYS) {
    const value = effective[key]
    if (isUnsetRuntimeValue(value)) {
      unset.push(key)
    } else {
      applied.push({ key, value: String(value) })
    }
  }
  return { applied, unset }
}
