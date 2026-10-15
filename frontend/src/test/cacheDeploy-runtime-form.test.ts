import { describe, expect, it } from 'vitest'
import {
  collectRuntimeFormErrors,
  hasRuntimeFormErrors,
  partitionRuntimeReview,
  repositorySuggestsQwen3,
  validateOptionalGpuMemoryUtilization,
  validateOptionalPositiveInt,
  validateRuntimePort,
} from '../utils/cacheDeployRuntimeForm'

describe('cache deploy runtime form validation (P1-8C)', () => {
  it('accepts valid runtimePort and rejects NaN/out-of-range', () => {
    expect(validateRuntimePort(8000)).toBeUndefined()
    expect(validateRuntimePort(1)).toBeUndefined()
    expect(validateRuntimePort(65535)).toBeUndefined()
    expect(validateRuntimePort(0)).toBeTruthy()
    expect(validateRuntimePort(65536)).toBeTruthy()
    expect(validateRuntimePort(Number.NaN)).toBeTruthy()
    expect(validateRuntimePort(Number.POSITIVE_INFINITY)).toBeTruthy()
    expect(validateRuntimePort(1.5)).toBeTruthy()
  })

  it('treats unset max_model_len / max_num_seqs as valid', () => {
    expect(validateOptionalPositiveInt(null, 'max_model_len')).toBeUndefined()
    expect(validateOptionalPositiveInt(undefined, 'max_model_len')).toBeUndefined()
    expect(validateOptionalPositiveInt(4096, 'max_model_len')).toBeUndefined()
    expect(validateOptionalPositiveInt(0, 'max_model_len')).toBeTruthy()
    expect(validateOptionalPositiveInt(-1, 'max_num_seqs')).toBeTruthy()
    expect(validateOptionalPositiveInt(1.2, 'max_num_seqs')).toBeTruthy()
    expect(validateOptionalPositiveInt(Number.NaN, 'max_num_seqs')).toBeTruthy()
  })

  it('validates gpu_memory_utilization in (0, 1]', () => {
    expect(validateOptionalGpuMemoryUtilization(null)).toBeUndefined()
    expect(validateOptionalGpuMemoryUtilization(0.15)).toBeUndefined()
    expect(validateOptionalGpuMemoryUtilization(1)).toBeUndefined()
    expect(validateOptionalGpuMemoryUtilization(0)).toBeTruthy()
    expect(validateOptionalGpuMemoryUtilization(1.01)).toBeTruthy()
    expect(validateOptionalGpuMemoryUtilization(Number.NaN)).toBeTruthy()
  })

  it('collects field errors and reports overall validity', () => {
    const ok = collectRuntimeFormErrors(8000, {
      max_model_len: 8192,
      max_num_seqs: 4,
      gpu_memory_utilization: 0.15,
    })
    expect(hasRuntimeFormErrors(ok)).toBe(false)

    const bad = collectRuntimeFormErrors(Number.NaN, {
      max_model_len: -1,
      max_num_seqs: 0,
      gpu_memory_utilization: 2,
    })
    expect(bad.runtimePort).toBeTruthy()
    expect(bad.max_model_len).toBeTruthy()
    expect(bad.max_num_seqs).toBeTruthy()
    expect(bad.gpu_memory_utilization).toBeTruthy()
    expect(hasRuntimeFormErrors(bad)).toBe(true)
  })

  it('partitions applied vs unset runtime review rows', () => {
    const part = partitionRuntimeReview(
      {
        runner: 'pooling',
        max_model_len: 8192,
        probe_type: 'EMBEDDING',
        health_path: '/health',
        dtype: null,
      },
      1,
    )
    expect(part.applied.map((r) => r.key)).toEqual(
      expect.arrayContaining([
        'runner',
        'max_model_len',
        'probe_type',
        'health_path',
        'tensor_parallel_size',
      ]),
    )
    expect(part.unset).toEqual(
      expect.arrayContaining([
        'dtype',
        'quantization',
        'reasoning_parser',
        'vllm_model_runner',
      ]),
    )
    expect(
      part.applied.find((r) => r.key === 'tensor_parallel_size')?.value,
    ).toBe('1')
  })

  it('uses name substring only for qwen3 advisory (not a matrix)', () => {
    expect(repositorySuggestsQwen3('Qwen/Qwen3-8B')).toBe(true)
    expect(repositorySuggestsQwen3('org/llama-3')).toBe(false)
    expect(repositorySuggestsQwen3(null, 'demo-qwen3-chat')).toBe(true)
  })
})
