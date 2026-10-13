import { describe, expect, it } from 'vitest'
import { fitInputFingerprint, isFreshResourceSnapshot } from '../utils/cacheDeployFit'

describe('cache deploy live GPU / fit safety', () => {
  it('invalidates a previous FIT after changing GPU or expected VRAM', () => {
    const base = fitInputFingerprint(['gpu-0'], '', { dtype: 'float16' })
    expect(fitInputFingerprint(['gpu-1'], '', { dtype: 'float16' })).not.toBe(base)
    expect(fitInputFingerprint(['gpu-0'], '12000', { dtype: 'float16' })).not.toBe(base)
    expect(fitInputFingerprint(['gpu-0'], ' ', { dtype: 'float16' })).toBe(base)
  })

  it('invalidates FIT after runtime inputs change, even when the estimator is approximate', () => {
    const base = fitInputFingerprint(['gpu-0'], '', {
      max_model_len: 4096,
      max_num_seqs: 1,
      gpu_memory_utilization: 0.85,
      quantization: 'awq',
    })
    for (const config of [
      { max_model_len: 8192, max_num_seqs: 1, gpu_memory_utilization: 0.85, quantization: 'awq' },
      { max_model_len: 4096, max_num_seqs: 2, gpu_memory_utilization: 0.85, quantization: 'awq' },
      { max_model_len: 4096, max_num_seqs: 1, gpu_memory_utilization: 0.75, quantization: 'awq' },
      { max_model_len: 4096, max_num_seqs: 1, gpu_memory_utilization: 0.85, quantization: 'gptq' },
    ]) {
      expect(fitInputFingerprint(['gpu-0'], '', config)).not.toBe(base)
    }
  })

  it('hides stale, missing, invalid and far-future GPU snapshots', () => {
    const now = Date.parse('2026-10-13T02:00:00Z')
    expect(isFreshResourceSnapshot('2026-10-13T01:59:50Z', now)).toBe(true)
    expect(isFreshResourceSnapshot('2026-10-13T01:58:00Z', now)).toBe(false)
    expect(isFreshResourceSnapshot(null, now)).toBe(false)
    expect(isFreshResourceSnapshot('not-a-date', now)).toBe(false)
    expect(isFreshResourceSnapshot('2026-10-13T02:01:00Z', now)).toBe(false)
  })
})
