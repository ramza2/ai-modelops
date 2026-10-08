import { describe, expect, it } from 'vitest'
import { defaultsForModelType } from '../pages/CacheDeployWizardPage'

describe('cache deploy wizard defaults policy', () => {
  it('generic EMBEDDING leaves capacity knobs unset', () => {
    const d = defaultsForModelType('EMBEDDING', 'org/some-embedder')
    expect(d.runtime.runner).toBe('pooling')
    expect(d.runtime.probe_type).toBe('EMBEDDING')
    expect(d.runtime.health_path).toBe('/health')
    expect(d.runtime.max_model_len).toBeUndefined()
    expect(d.runtime.max_num_seqs).toBeUndefined()
    expect(d.runtime.gpu_memory_utilization).toBeUndefined()
    expect(d.runtime.dtype).toBeUndefined()
    expect(d.knownProfileLabel).toBeNull()
  })

  it('BAAI/bge-m3 applies known-profile prefill only', () => {
    const d = defaultsForModelType('EMBEDDING', 'BAAI/bge-m3')
    expect(d.runtime.max_model_len).toBe(8192)
    expect(d.runtime.max_num_seqs).toBe(4)
    expect(d.runtime.gpu_memory_utilization).toBe(0.15)
    expect(d.runtime.runner).toBe('pooling')
    expect(d.knownProfileLabel).toMatch(/known profile/i)
  })

  it('LLM/VLM defaults to CHAT probe without capacity knobs', () => {
    const d = defaultsForModelType('LLM')
    expect(d.runtime.probe_type).toBe('CHAT')
    expect(d.runtime.max_model_len).toBeUndefined()
    expect(d.runtime.runner).toBeUndefined()
  })
})
