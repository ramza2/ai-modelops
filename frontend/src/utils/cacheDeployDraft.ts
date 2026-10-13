import type { CacheDeployRuntimeConfig } from '../api/cacheDeploy'
import type { ModelCacheEntry } from '../api/types'

// Per-tab persistence survives F5 and menu navigation. No Fit, GPU readings,
// operations, credentials or server-owned publication state are stored.
export type WizardDraftStep = 'cache' | 'runtime' | 'gpu' | 'review'
export type PublishDraftMode = 'new' | 'existing'

export type WizardDraft = {
  schema: 1
  cacheId: string
  nodeId: string
  modelVersionId: string | null
  revision: string | null
  savedAt: number
  step: WizardDraftStep
  name: string
  containerName: string
  servedModelName: string
  runtimePort: number
  runtime: CacheDeployRuntimeConfig
  selectedGpuIds: string[]
  expectedVramMb: string
}

export type PublishDraft = {
  schema: 1
  deploymentId: string
  savedAt: number
  mode: PublishDraftMode
  alias: string
  endpointId: string
  rewriteModelName: string
}

export type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
const TTL_MS = 24 * 60 * 60 * 1000
const KEY_PREFIX = 'modelops:cache-deploy-draft:v1:'
const PUBLISH_PREFIX = 'modelops:cache-publish-draft:v1:'
const STEPS: WizardDraftStep[] = ['cache', 'runtime', 'gpu', 'review']

const isRecord = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

const recent = (v: unknown, now: number): v is number =>
  typeof v === 'number' && Number.isFinite(v) &&
  v <= now + 5000 && now - v <= TTL_MS

const validText = (v: unknown): v is string =>
  typeof v === 'string' && v.length <= 1024

const validRuntime = (v: unknown): v is CacheDeployRuntimeConfig => {
  if (!isRecord(v)) return false
  const positive = ['max_model_len', 'max_num_seqs', 'tensor_parallel_size']
  for (const field of positive) {
    const x = v[field]
    if (x != null && (typeof x !== 'number' || !Number.isInteger(x) || x < 1)) return false
  }
  const utilization = v.gpu_memory_utilization
  if (utilization != null &&
      (typeof utilization !== 'number' || !Number.isFinite(utilization) ||
       utilization <= 0 || utilization > 1)) return false
  for (const field of ['dtype', 'quantization', 'runner', 'health_path', 'probe_type', 'scheduling_policy']) {
    if (v[field] != null && !validText(v[field])) return false
  }
  return true
}

export function wizardDraftKey(cacheId: string): string {
  return `${KEY_PREFIX}${cacheId}`
}

export function publishDraftKey(deploymentId: string): string {
  return `${PUBLISH_PREFIX}${deploymentId}`
}

export function readWizardDraft(
  storage: DraftStorage | null,
  cache: Pick<ModelCacheEntry, 'id' | 'node_id' | 'model_version_id' | 'resolved_revision'>,
  now = Date.now(),
): WizardDraft | null {
  if (!storage) return null
  const key = wizardDraftKey(cache.id)
  try {
    const raw = storage.getItem(key)
    if (!raw) return null
    const v: unknown = JSON.parse(raw)
    if (!isRecord(v) || v.schema !== 1 || v.cacheId !== cache.id ||
        v.nodeId !== cache.node_id ||
        v.modelVersionId !== cache.model_version_id ||
        v.revision !== cache.resolved_revision ||
        !recent(v.savedAt, now) || !STEPS.includes(v.step as WizardDraftStep) ||
        !validText(v.name) || !validText(v.containerName) ||
        !validText(v.servedModelName) || typeof v.runtimePort !== 'number' || !Number.isInteger(v.runtimePort) ||
        (v.runtimePort as number) < 1 || (v.runtimePort as number) > 65535 ||
        !validRuntime(v.runtime) || !Array.isArray(v.selectedGpuIds) ||
        v.selectedGpuIds.length > 32 ||
        !v.selectedGpuIds.every(validText) || !validText(v.expectedVramMb)) {
      storage.removeItem(key)
      return null
    }
    return v as WizardDraft
  } catch {
    // Browser storage is optional: privacy mode/quota/corrupt JSON must not break Deploy.
    return null
  }
}

export function writeWizardDraft(storage: DraftStorage | null, draft: WizardDraft): void {
  if (!storage) return
  try { storage.setItem(wizardDraftKey(draft.cacheId), JSON.stringify(draft)) }
  catch { /* Storage blocked/quota: wizard still works in memory. */ }
}

export function clearWizardDraft(storage: DraftStorage | null, cacheId: string): void {
  if (!storage) return
  try { storage.removeItem(wizardDraftKey(cacheId)) } catch { /* optional */ }
}

export function readPublishDraft(
  storage: DraftStorage | null,
  deploymentId: string,
  now = Date.now(),
): PublishDraft | null {
  if (!storage) return null
  const key = publishDraftKey(deploymentId)
  try {
    const raw = storage.getItem(key)
    if (!raw) return null
    const v: unknown = JSON.parse(raw)
    if (!isRecord(v) || v.schema !== 1 || v.deploymentId !== deploymentId ||
        !recent(v.savedAt, now) ||
        (v.mode !== 'new' && v.mode !== 'existing') ||
        !validText(v.alias) || !validText(v.endpointId) ||
        !validText(v.rewriteModelName)) {
      storage.removeItem(key)
      return null
    }
    return v as PublishDraft
  } catch { return null }
}

export function writePublishDraft(storage: DraftStorage | null, draft: PublishDraft): void {
  if (!storage) return
  try { storage.setItem(publishDraftKey(draft.deploymentId), JSON.stringify(draft)) }
  catch { /* optional */ }
}

export function clearPublishDraft(storage: DraftStorage | null, deploymentId: string): void {
  if (!storage) return
  try { storage.removeItem(publishDraftKey(deploymentId)) } catch { /* optional */ }
}

export function resumableStep(saved: WizardDraftStep, urlStep: string | null): WizardDraftStep {
  const requested = urlStep && STEPS.includes(urlStep as WizardDraftStep)
    ? (urlStep as WizardDraftStep)
    : saved
  // A Fit preview must be fresh after F5; never restore directly to Review.
  return requested === 'review' ? 'gpu' : requested
}
