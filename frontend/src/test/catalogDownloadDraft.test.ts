import { describe, expect, it } from 'vitest'
import {
  catalogDownloadDraftConfig,
  catalogDownloadDraftKey,
  catalogDownloadTrackKey,
  clearCatalogDownloadDraft,
  normalizeRequestedRevision,
  readCatalogDownloadDraft,
  writeCatalogDownloadDraft,
  type CatalogDownloadDraft,
  type DraftStorage,
} from '../utils/catalogDownloadDraft'

function memoryStorage(): DraftStorage & { store: Map<string, string> } {
  const store = new Map<string, string>()
  return {
    store,
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => {
      store.set(k, v)
    },
    removeItem: (k) => {
      store.delete(k)
    },
    get length() {
      return store.size
    },
    key: (index) => Array.from(store.keys())[index] ?? null,
  }
}

describe('catalogDownloadDraft', () => {
  it('builds stable track and draft keys for node+repo+revision', () => {
    expect(catalogDownloadTrackKey('n1', 'org/m', 'abc')).toBe(
      ['n1', 'org/m', 'abc'].join('\u0001'),
    )
    expect(catalogDownloadTrackKey('n1', 'org/m', null)).toBe(
      ['n1', 'org/m', ''].join('\u0001'),
    )
    expect(catalogDownloadTrackKey('n1', 'org/m', 'abc')).not.toBe(
      catalogDownloadTrackKey('n2', 'org/m', 'abc'),
    )
    expect(catalogDownloadTrackKey('n1', 'org/m', 'abc')).not.toBe(
      catalogDownloadTrackKey('n1', 'org/m', 'def'),
    )
    expect(catalogDownloadDraftKey({
      nodeId: 'n1',
      repositoryId: 'org/m',
      requestedRevision: 'abc',
    })).toContain(catalogDownloadDraftConfig.keyPrefix)
    expect(normalizeRequestedRevision(undefined)).toBeNull()
  })

  it('round-trips a valid draft and rejects mismatched target', () => {
    const storage = memoryStorage()
    const now = 1_700_000_000_000
    const draft: CatalogDownloadDraft = {
      schema: 1,
      jobId: 'job-1',
      nodeId: 'node-1',
      repositoryId: 'org/model',
      requestedRevision: 'abc',
      savedAt: now,
    }
    writeCatalogDownloadDraft(storage, draft)
    expect(
      readCatalogDownloadDraft(
        storage,
        {
          nodeId: 'node-1',
          repositoryId: 'org/model',
          requestedRevision: 'abc',
        },
        now,
      ),
    ).toEqual(draft)
    expect(
      readCatalogDownloadDraft(
        storage,
        {
          nodeId: 'node-2',
          repositoryId: 'org/model',
          requestedRevision: 'abc',
        },
        now,
      ),
    ).toBeNull()
  })

  it('clears corrupted JSON and expired drafts', () => {
    const storage = memoryStorage()
    const now = 1_700_000_000_000
    const key = catalogDownloadDraftKey({
      nodeId: 'node-1',
      repositoryId: 'org/model',
      requestedRevision: 'abc',
    })
    storage.setItem(key, '{not-json')
    expect(
      readCatalogDownloadDraft(
        storage,
        {
          nodeId: 'node-1',
          repositoryId: 'org/model',
          requestedRevision: 'abc',
        },
        now,
      ),
    ).toBeNull()
    expect(storage.getItem(key)).toBeNull()

    writeCatalogDownloadDraft(storage, {
      schema: 1,
      jobId: 'job-old',
      nodeId: 'node-1',
      repositoryId: 'org/model',
      requestedRevision: 'abc',
      savedAt: now - catalogDownloadDraftConfig.ttlMs - 1,
    })
    // Force-write expired savedAt past writeCatalogDownloadDraft validation path
    storage.setItem(
      key,
      JSON.stringify({
        schema: 1,
        jobId: 'job-old',
        nodeId: 'node-1',
        repositoryId: 'org/model',
        requestedRevision: 'abc',
        savedAt: now - catalogDownloadDraftConfig.ttlMs - 1,
      }),
    )
    expect(
      readCatalogDownloadDraft(
        storage,
        {
          nodeId: 'node-1',
          repositoryId: 'org/model',
          requestedRevision: 'abc',
        },
        now,
      ),
    ).toBeNull()
  })

  it('prunes oldest entries beyond max and supports clear', () => {
    const storage = memoryStorage()
    const now = 1_700_000_000_000
    const max = catalogDownloadDraftConfig.maxEntries
    for (let i = 0; i < max + 5; i += 1) {
      writeCatalogDownloadDraft(storage, {
        schema: 1,
        jobId: `job-${i}`,
        nodeId: 'node-1',
        repositoryId: `org/model-${i}`,
        requestedRevision: null,
        savedAt: now + i,
      })
    }
    const remaining = Array.from(storage.store.keys()).filter((k) =>
      k.startsWith(catalogDownloadDraftConfig.keyPrefix),
    )
    expect(remaining.length).toBe(max)

    clearCatalogDownloadDraft(storage, {
      nodeId: 'node-1',
      repositoryId: 'org/model-10',
      requestedRevision: null,
    })
    expect(
      readCatalogDownloadDraft(
        storage,
        {
          nodeId: 'node-1',
          repositoryId: 'org/model-10',
          requestedRevision: null,
        },
        now + max + 10,
      ),
    ).toBeNull()
  })

  it('no-ops safely when storage is null or throws', () => {
    expect(
      readCatalogDownloadDraft(null, {
        nodeId: 'n',
        repositoryId: 'r',
        requestedRevision: null,
      }),
    ).toBeNull()
    writeCatalogDownloadDraft(null, {
      schema: 1,
      jobId: 'j',
      nodeId: 'n',
      repositoryId: 'r',
      requestedRevision: null,
      savedAt: Date.now(),
    })
    clearCatalogDownloadDraft(null, {
      nodeId: 'n',
      repositoryId: 'r',
      requestedRevision: null,
    })

    const throwing: DraftStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
      removeItem: () => {
        throw new Error('blocked')
      },
    }
    expect(
      readCatalogDownloadDraft(throwing, {
        nodeId: 'n',
        repositoryId: 'r',
        requestedRevision: null,
      }),
    ).toBeNull()
    writeCatalogDownloadDraft(throwing, {
      schema: 1,
      jobId: 'j',
      nodeId: 'n',
      repositoryId: 'r',
      requestedRevision: null,
      savedAt: Date.now(),
    })
  })
})
