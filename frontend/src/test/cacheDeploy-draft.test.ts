import { describe, expect, it } from 'vitest'
import type { ModelCacheEntry } from '../api/types'
import {
  clearPublishDraft, clearWizardDraft, publishDraftKey, readPublishDraft,
  readWizardDraft, resumableStep, wizardDraftKey, writePublishDraft,
  writeWizardDraft, type DraftStorage, type WizardDraft,
} from '../utils/cacheDeployDraft'

const now = Date.parse('2026-10-13T01:00:00Z')
const cache = {
  id: 'cache-A', node_id: 'node-A',
  model_version_id: 'version-A', resolved_revision: 'revision-A',
} satisfies Pick<ModelCacheEntry, 'id' | 'node_id' | 'model_version_id' | 'resolved_revision'>

function memoryStorage(): DraftStorage {
  const items = new Map<string, string>()
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => { items.set(key, value) },
    removeItem: (key: string) => { items.delete(key) },
  }
}

function sampleDraft(): WizardDraft {
  return {
    schema: 1, cacheId: cache.id, nodeId: cache.node_id,
    modelVersionId: cache.model_version_id, revision: cache.resolved_revision,
    savedAt: now, step: 'review', name: 'qwen3-production',
    containerName: 'modelops-qwen3', servedModelName: 'Qwen3-14B',
    runtimePort: 8000,
    runtime: {
      max_model_len: 8192, max_num_seqs: 2,
      gpu_memory_utilization: 0.85, quantization: 'awq', dtype: 'float16',
    },
    selectedGpuIds: ['gpu-0'], expectedVramMb: '',
  }
}

describe('P1-3 cache deploy draft persistence', () => {
  it('round-trips exact operator config across F5 and scopes by immutable cache revision', () => {
    const store = memoryStorage()
    const value = sampleDraft()
    writeWizardDraft(store, value)
    expect(readWizardDraft(store, cache, now)).toEqual(value)
    expect(readWizardDraft(store, { ...cache, resolved_revision: 'new' }, now)).toBeNull()
    expect(store.getItem(wizardDraftKey(cache.id))).toBeNull()
  })

  it('does not cross cache/node/model version boundary', () => {
    const store = memoryStorage()
    writeWizardDraft(store, sampleDraft())
    expect(readWizardDraft(store, { ...cache, id: 'cache-B' }, now)).toBeNull()
    expect(readWizardDraft(store, cache, now)?.runtime.max_model_len).toBe(8192)
    expect(readWizardDraft(store, { ...cache, node_id: 'other' }, now)).toBeNull()
  })

  it('rejects expired, future, corrupt, and malformed draft values', () => {
    const store = memoryStorage()
    writeWizardDraft(store, sampleDraft())
    expect(readWizardDraft(store, cache, now + 25 * 60 * 60 * 1000)).toBeNull()
    writeWizardDraft(store, { ...sampleDraft(), savedAt: now + 60_000 })
    expect(readWizardDraft(store, cache, now)).toBeNull()
    store.setItem(wizardDraftKey(cache.id), '{bad json')
    expect(readWizardDraft(store, cache, now)).toBeNull()
    store.setItem(wizardDraftKey(cache.id), JSON.stringify({ ...sampleDraft(), runtimePort: -1 }))
    expect(readWizardDraft(store, cache, now)).toBeNull()
  })

  it('handles blocked sessionStorage without preventing wizard usage', () => {
    const blocked: DraftStorage = {
      getItem: () => { throw Error('security') },
      setItem: () => { throw Error('quota') },
      removeItem: () => { throw Error('security') },
    }
    expect(readWizardDraft(blocked, cache, now)).toBeNull()
    expect(() => writeWizardDraft(blocked, sampleDraft())).not.toThrow()
    expect(() => clearWizardDraft(blocked, cache.id)).not.toThrow()
  })

  it('never resumes directly at Review without a fresh Fit', () => {
    expect(resumableStep('review', null)).toBe('gpu')
    expect(resumableStep('runtime', 'review')).toBe('gpu')
    expect(resumableStep('gpu', null)).toBe('gpu')
    expect(resumableStep('runtime', null)).toBe('runtime')
  })

  it('persists optional reasoning_parser=qwen3 and rejects unknown parsers', () => {
    const store = memoryStorage()
    const withParser: WizardDraft = {
      ...sampleDraft(),
      runtime: { ...sampleDraft().runtime, reasoning_parser: 'qwen3' },
    }
    writeWizardDraft(store, withParser)
    expect(readWizardDraft(store, cache, now)?.runtime.reasoning_parser).toBe(
      'qwen3',
    )
    store.setItem(
      wizardDraftKey(cache.id),
      JSON.stringify({
        ...withParser,
        runtime: { ...withParser.runtime, reasoning_parser: 'deepseek' },
      }),
    )
    expect(readWizardDraft(store, cache, now)).toBeNull()
  })

  it('persists optional vllm_model_runner=v1 and rejects unknown runners', () => {
    const store = memoryStorage()
    const withRunner: WizardDraft = {
      ...sampleDraft(),
      runtime: { ...sampleDraft().runtime, vllm_model_runner: 'v1' },
    }
    writeWizardDraft(store, withRunner)
    expect(readWizardDraft(store, cache, now)?.runtime.vllm_model_runner).toBe(
      'v1',
    )
    store.setItem(
      wizardDraftKey(cache.id),
      JSON.stringify({
        ...withRunner,
        runtime: { ...withRunner.runtime, vllm_model_runner: 'v2' },
      }),
    )
    expect(readWizardDraft(store, cache, now)).toBeNull()
  })

  it('persists auto tool choice + hermes and rejects invalid pairs', () => {
    const store = memoryStorage()
    const withTools: WizardDraft = {
      ...sampleDraft(),
      runtime: {
        ...sampleDraft().runtime,
        enable_auto_tool_choice: true,
        tool_call_parser: 'hermes',
      },
    }
    writeWizardDraft(store, withTools)
    const read = readWizardDraft(store, cache, now)?.runtime
    expect(read?.enable_auto_tool_choice).toBe(true)
    expect(read?.tool_call_parser).toBe('hermes')

    store.setItem(
      wizardDraftKey(cache.id),
      JSON.stringify({
        ...withTools,
        runtime: {
          ...withTools.runtime,
          enable_auto_tool_choice: true,
          tool_call_parser: null,
        },
      }),
    )
    expect(readWizardDraft(store, cache, now)).toBeNull()

    store.setItem(
      wizardDraftKey(cache.id),
      JSON.stringify({
        ...withTools,
        runtime: {
          ...withTools.runtime,
          enable_auto_tool_choice: false,
          tool_call_parser: 'hermes',
        },
      }),
    )
    expect(readWizardDraft(store, cache, now)).toBeNull()
  })

  it('keeps pending publish alias settings distinct from server-owned route status', () => {
    const store = memoryStorage()
    const draft = {
      schema: 1 as const, deploymentId: 'dep-1', savedAt: now,
      mode: 'new' as const, alias: 'qwen3-14b', endpointId: '',
      rewriteModelName: 'Qwen3-14B',
    }
    writePublishDraft(store, draft)
    expect(readPublishDraft(store, 'dep-1', now)).toEqual(draft)
    expect(readPublishDraft(store, 'dep-2', now)).toBeNull()
    clearPublishDraft(store, 'dep-1')
    expect(store.getItem(publishDraftKey('dep-1'))).toBeNull()
    expect(readPublishDraft(store, 'dep-1', now)).toBeNull()
  })
})
