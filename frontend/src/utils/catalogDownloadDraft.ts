/**
 * P1-5C: Persist Catalog download Job IDs in sessionStorage for F5 restore.
 * Stores only job identity — never Job status/progress (server is source of truth),
 * tokens, credentials, or local file contents.
 */

export type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> & {
  readonly length?: number
  key?: (index: number) => string | null
}

export type CatalogDownloadDraft = {
  schema: 1
  jobId: string
  nodeId: string
  repositoryId: string
  requestedRevision: string | null
  savedAt: number
}

export type CatalogDownloadTarget = {
  nodeId: string
  repositoryId: string
  requestedRevision: string | null
}

const SCHEMA = 1
const TTL_MS = 24 * 60 * 60 * 1000
const MAX_ENTRIES = 40
const KEY_PREFIX = 'modelops:catalog-download-job:v1:'

const isRecord = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

const recent = (v: unknown, now: number): v is number =>
  typeof v === 'number' &&
  Number.isFinite(v) &&
  v <= now + 5000 &&
  now - v <= TTL_MS

const validId = (v: unknown): v is string =>
  typeof v === 'string' && v.length > 0 && v.length <= 256

const validRevision = (v: unknown): v is string | null =>
  v === null || (typeof v === 'string' && v.length <= 256)

export function normalizeRequestedRevision(
  revision: string | null | undefined,
): string | null {
  return revision ?? null
}

export function catalogDownloadTrackKey(
  nodeId: string,
  repositoryId: string,
  requestedRevision: string | null | undefined,
): string {
  return [
    nodeId,
    repositoryId,
    normalizeRequestedRevision(requestedRevision) ?? '',
  ].join('\u0001')
}

export function catalogDownloadDraftKey(target: CatalogDownloadTarget): string {
  return (
    KEY_PREFIX +
    [
      encodeURIComponent(target.nodeId),
      encodeURIComponent(target.repositoryId),
      encodeURIComponent(normalizeRequestedRevision(target.requestedRevision) ?? ''),
    ].join(':')
  )
}

function listPrefixedKeys(storage: DraftStorage): string[] {
  if (typeof storage.length !== 'number' || typeof storage.key !== 'function') {
    return []
  }
  const keys: string[] = []
  for (let i = 0; i < storage.length; i += 1) {
    const k = storage.key(i)
    if (k && k.startsWith(KEY_PREFIX)) keys.push(k)
  }
  return keys
}

function readRawDraft(
  storage: DraftStorage,
  key: string,
  now: number,
): CatalogDownloadDraft | null {
  try {
    const raw = storage.getItem(key)
    if (!raw) return null
    const v: unknown = JSON.parse(raw)
    if (
      !isRecord(v) ||
      v.schema !== SCHEMA ||
      !validId(v.jobId) ||
      !validId(v.nodeId) ||
      !validId(v.repositoryId) ||
      !validRevision(v.requestedRevision) ||
      !recent(v.savedAt, now)
    ) {
      try {
        storage.removeItem(key)
      } catch {
        /* optional */
      }
      return null
    }
    return {
      schema: 1,
      jobId: v.jobId,
      nodeId: v.nodeId,
      repositoryId: v.repositoryId,
      requestedRevision: v.requestedRevision,
      savedAt: v.savedAt,
    }
  } catch {
    try {
      storage.removeItem(key)
    } catch {
      /* optional */
    }
    return null
  }
}

function pruneDownloadDrafts(storage: DraftStorage, now: number): void {
  const entries: Array<{ key: string; savedAt: number }> = []
  for (const key of listPrefixedKeys(storage)) {
    const draft = readRawDraft(storage, key, now)
    if (!draft) continue
    entries.push({ key, savedAt: draft.savedAt })
  }
  if (entries.length <= MAX_ENTRIES) return
  entries.sort((a, b) => a.savedAt - b.savedAt)
  const overflow = entries.length - MAX_ENTRIES
  for (let i = 0; i < overflow; i += 1) {
    try {
      storage.removeItem(entries[i].key)
    } catch {
      /* optional */
    }
  }
}

export function readCatalogDownloadDraft(
  storage: DraftStorage | null,
  target: CatalogDownloadTarget,
  now = Date.now(),
): CatalogDownloadDraft | null {
  if (!storage || !target.nodeId || !target.repositoryId) return null
  const key = catalogDownloadDraftKey(target)
  const draft = readRawDraft(storage, key, now)
  if (!draft) return null
  if (
    draft.nodeId !== target.nodeId ||
    draft.repositoryId !== target.repositoryId ||
    normalizeRequestedRevision(draft.requestedRevision) !==
      normalizeRequestedRevision(target.requestedRevision)
  ) {
    try {
      storage.removeItem(key)
    } catch {
      /* optional */
    }
    return null
  }
  return draft
}

export function writeCatalogDownloadDraft(
  storage: DraftStorage | null,
  draft: CatalogDownloadDraft,
): void {
  if (!storage) return
  if (
    draft.schema !== 1 ||
    !validId(draft.jobId) ||
    !validId(draft.nodeId) ||
    !validId(draft.repositoryId) ||
    !validRevision(draft.requestedRevision)
  ) {
    return
  }
  try {
    storage.setItem(
      catalogDownloadDraftKey(draft),
      JSON.stringify({
        schema: 1,
        jobId: draft.jobId,
        nodeId: draft.nodeId,
        repositoryId: draft.repositoryId,
        requestedRevision: normalizeRequestedRevision(draft.requestedRevision),
        savedAt: draft.savedAt,
      } satisfies CatalogDownloadDraft),
    )
    pruneDownloadDrafts(storage, draft.savedAt)
  } catch {
    /* Storage blocked/quota: in-memory tracking still works. */
  }
}

export function clearCatalogDownloadDraft(
  storage: DraftStorage | null,
  target: CatalogDownloadTarget,
): void {
  if (!storage) return
  try {
    storage.removeItem(catalogDownloadDraftKey(target))
  } catch {
    /* optional */
  }
}

/** Test helper: expose TTL/max for focused unit tests. */
export const catalogDownloadDraftConfig = {
  ttlMs: TTL_MS,
  maxEntries: MAX_ENTRIES,
  keyPrefix: KEY_PREFIX,
}
