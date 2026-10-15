import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { analyzeHfResourceFit, listHfCatalog } from '../api/catalog'
import {
  getHfDownload,
  isActiveDownloadStatus,
  startHfDownload,
} from '../api/downloads'
import { listNodes } from '../api/nodes'
import type {
  HfCatalogModel,
  HfDownloadJob,
  NodeSummary,
  ResourceFitAnalysis,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import {
  catalogDownloadTrackKey,
  clearCatalogDownloadDraft,
  normalizeRequestedRevision,
  readCatalogDownloadDraft,
  writeCatalogDownloadDraft,
  type DraftStorage,
} from '../utils/catalogDownloadDraft'
import { abbreviateMiddle } from '../utils/format'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'

type TrackedDownload = {
  job: HfDownloadJob
  nodeId: string
  repositoryId: string
  requestedRevision: string | null
  /** Client time of last successful GET/POST job payload. */
  lastSuccessAt: Date | null
}

function jobMatchesTrack(
  job: HfDownloadJob,
  nodeId: string,
  repositoryId: string,
  requestedRevision: string | null,
): boolean {
  return (
    job.job_id.length > 0 &&
    job.node_id === nodeId &&
    job.repository_id === repositoryId &&
    normalizeRequestedRevision(job.requested_revision) ===
      normalizeRequestedRevision(requestedRevision)
  )
}

/** Per-repo detailed Fit analysis (button click), distinct from catalog list summary. */
type FitDetailState = {
  analysis: ResourceFitAnalysis
  /** Client wall-clock when the analyze request was started. */
  requestedAt: Date
  nodeId: string
  revision: string | null
}

function normalizeRevision(revision: string | null | undefined): string | null {
  return revision ?? null
}

/** Only Fit details that match the current catalog row + selected Node are usable. */
function fitDetailMatchesCurrent(
  detail: FitDetailState | undefined,
  item: HfCatalogModel,
  currentNodeId: string,
): detail is FitDetailState {
  if (!detail) return false
  if (!currentNodeId || detail.nodeId !== currentNodeId) return false
  if (detail.analysis.repository_id !== item.repository_id) return false
  if (detail.revision !== normalizeRevision(item.revision)) return false
  if (detail.analysis.node_id !== currentNodeId) return false
  if (
    detail.analysis.revision != null &&
    detail.analysis.revision !== normalizeRevision(item.revision)
  ) {
    return false
  }
  return true
}

const PAGE_SIZE = 20
/** Active download poll cadence; next GET is scheduled only after the prior cycle finishes. */
export const CATALOG_DOWNLOAD_POLL_MS = 2000
const POLL_MS = CATALOG_DOWNLOAD_POLL_MS
const KNOWN_TYPES = new Set(['LLM', 'VLM', 'EMBEDDING'])

function parseModelType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TYPES.has(upper)) return null
  return upper
}

function formatBytes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let v = value / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`
}

/** Catalog list `resource_fit` summary only — not the detailed analyze response. */
function catalogFitSummaryReason(item: HfCatalogModel): string {
  if (item.resource_fit?.reasons?.length) return item.resource_fit.reasons[0]
  if (item.resource_fit?.result) {
    return `목록 요약 ${item.resource_fit.result} (advisory)`
  }
  return 'Node를 선택하면 Fit 분석으로 상세 결과를 확인할 수 있습니다.'
}

function formatMb(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value} MiB`
}

function isDiskInsufficientForDownload(
  detail?: ResourceFitAnalysis | null,
): boolean {
  if (!detail) return false
  return detail.disk_ok === false
}

function diskFitLabel(diskOk: boolean | null | undefined): string {
  if (diskOk === true) return 'OK'
  if (diskOk === false) return 'INSUFFICIENT'
  return 'UNKNOWN'
}

function resolveDownloadModelType(
  item: HfCatalogModel,
  override: string | undefined,
): string | null {
  const raw = (override || item.model_type || '').toUpperCase()
  if (raw === 'LLM' || raw === 'VLM' || raw === 'EMBEDDING') return raw
  return null
}

function downloadDisabledReason(
  nodeId: string,
  detail?: ResourceFitAnalysis | null,
): string | undefined {
  if (!nodeId) return 'Node를 선택하세요.'
  if (isDiskInsufficientForDownload(detail)) {
    return '디스크 공간이 부족합니다.'
  }
  return undefined
}

/** Job MUST be READY with an explicit cache_status=READY + cache id — never guess from repo name. */
function isDownloadCacheReadyForDeploy(job: HfDownloadJob): boolean {
  return (
    job.status.toUpperCase() === 'READY' &&
    typeof job.node_model_cache_id === 'string' &&
    job.node_model_cache_id.length > 0 &&
    (job.cache_status ?? '').toUpperCase() === 'READY'
  )
}

function isTerminalReadyDownload(job: HfDownloadJob | undefined): boolean {
  return Boolean(job && job.status.toUpperCase() === 'READY')
}

export function ModelCatalogPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const modelType = parseModelType(searchParams.get('model_type'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositivePage(searchParams.get('page'))
  const nodeId = searchParams.get('node_id') ?? ''
  const fitOnly = searchParams.get('fit_only') === 'true'

  const [draftQ, setDraftQ] = useState(q)
  const [nodes, setNodes] = useState<NodeSummary[]>([])
  const [items, setItems] = useState<HfCatalogModel[] | null>(null)
  /** Catalog context key that produced the current `items` (stale-list guard). */
  const [itemsBoundKey, setItemsBoundKey] = useState<string | null>(null)
  const itemsBoundKeyRef = useRef<string | null>(null)
  itemsBoundKeyRef.current = itemsBoundKey
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [fitByRepo, setFitByRepo] = useState<Record<string, FitDetailState>>({})
  const [fitErrorByRepo, setFitErrorByRepo] = useState<
    Record<string, { message: string; revision: string | null }>
  >({})
  const [fitLoadingByRepo, setFitLoadingByRepo] = useState<
    Record<string, { revision: string | null }>
  >({})
  const [expandedFitRepos, setExpandedFitRepos] = useState<
    Record<string, boolean>
  >({})
  const [downloadByKey, setDownloadByKey] = useState<
    Record<string, TrackedDownload>
  >({})
  const [downloadRestoringByKey, setDownloadRestoringByKey] = useState<
    Record<string, boolean>
  >({})
  const [downloadPollWarningByKey, setDownloadPollWarningByKey] = useState<
    Record<string, string>
  >({})
  const [downloadTypeByRepo, setDownloadTypeByRepo] = useState<
    Record<string, string>
  >({})
  const [downloadStartingByKey, setDownloadStartingByKey] = useState<
    Record<string, boolean>
  >({})
  const storage = ((): DraftStorage | null => {
    try {
      return window.sessionStorage
    } catch {
      return null
    }
  })()
  const abortRef = useRef<AbortController | null>(null)
  const downloadByKeyRef = useRef(downloadByKey)
  downloadByKeyRef.current = downloadByKey
  const downloadPollGenRef = useRef(0)
  const downloadRestoreGenRef = useRef(0)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)
  const fitAbortByRepoRef = useRef<Record<string, AbortController>>({})
  const fitGenByRepoRef = useRef<Record<string, number>>({})
  /** After successful Fit expand, scroll/focus the new detail once it mounts. */
  const pendingFitRevealRepoRef = useRef<string | null>(null)
  const nodeIdRef = useRef(nodeId)
  nodeIdRef.current = nodeId
  const catalogContextKey = `${nodeId}|${q}|${modelType ?? ''}|${page}|${fitOnly}`
  const catalogContextKeyRef = useRef(catalogContextKey)
  catalogContextKeyRef.current = catalogContextKey
  const itemsRevisionByRepoRef = useRef<Record<string, string | null>>({})

  useEffect(() => {
    setDraftQ(q)
  }, [q])

  const syncUrl = useCallback(
    (next: {
      q: string
      modelType: string | null
      page: number
      nodeId: string
      fitOnly: boolean
    }) => {
      const params = new URLSearchParams()
      const trimmedQ = next.q.trim()
      if (trimmedQ) params.set('q', trimmedQ)
      if (next.modelType) params.set('model_type', next.modelType)
      if (next.nodeId) params.set('node_id', next.nodeId)
      if (next.fitOnly) params.set('fit_only', 'true')
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current
      const requestKey = `${nodeId}|${q}|${modelType ?? ''}|${page}|${fitOnly}`

      if (mode === 'initial' || !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const [nodePage, catalog] = await Promise.all([
          listNodes({ page: 1, pageSize: 100, signal: controller.signal }),
          listHfCatalog({
            q: q.trim() || null,
            modelType,
            page,
            pageSize: PAGE_SIZE,
            fitOnly,
            nodeId: nodeId || null,
            signal: controller.signal,
          }),
        ])
        if (controller.signal.aborted || gen !== readGenRef.current) return
        setNodes(nodePage.items)
        setItems(catalog.items)
        setItemsBoundKey(requestKey)
        setHasMore(Boolean(catalog.has_more))
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Hugging Face 카탈로그를 불러오지 못했습니다.')
        }
        setItems([])
        setItemsBoundKey(requestKey)
        setHasMore(false)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [q, modelType, page, fitOnly, nodeId],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  // Drop detailed Fit results when Node / search / page context changes so a
  // late response cannot paint onto a different catalog view.
  useEffect(() => {
    for (const controller of Object.values(fitAbortByRepoRef.current)) {
      controller.abort()
    }
    fitAbortByRepoRef.current = {}
    pendingFitRevealRepoRef.current = null
    setFitByRepo({})
    setFitErrorByRepo({})
    setFitLoadingByRepo({})
    setExpandedFitRepos({})
  }, [catalogContextKey])

  // Bring auto-expanded Fit detail into view (and focus its heading) when the
  // success state mounts — table rows near the viewport bottom can otherwise
  // expand off-screen with no obvious completion signal.
  useLayoutEffect(() => {
    const repoId = pendingFitRevealRepoRef.current
    if (!repoId || !expandedFitRepos[repoId] || !fitByRepo[repoId]) return
    const detail = document.querySelector(
      `[data-testid="${CSS.escape(`fit-detail-${repoId}`)}"]`,
    )
    if (!(detail instanceof HTMLElement)) return
    pendingFitRevealRepoRef.current = null
    if (typeof detail.scrollIntoView === 'function') {
      detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
    const heading = detail.querySelector('.section-subheading')
    if (heading instanceof HTMLElement) {
      if (!heading.hasAttribute('tabindex')) heading.tabIndex = -1
      heading.focus({ preventScroll: true })
    }
  }, [expandedFitRepos, fitByRepo])

  useEffect(() => {
    return () => {
      for (const controller of Object.values(fitAbortByRepoRef.current)) {
        controller.abort()
      }
      fitAbortByRepoRef.current = {}
    }
  }, [])

  const invalidateFitRepo = useCallback((repoId: string) => {
    fitAbortByRepoRef.current[repoId]?.abort()
    delete fitAbortByRepoRef.current[repoId]
    fitGenByRepoRef.current[repoId] = (fitGenByRepoRef.current[repoId] ?? 0) + 1
  }, [])

  // Same search context can refresh to a new revision or drop a model — drop
  // mismatched Fit detail/loading/errors and abort in-flight analyze calls.
  useEffect(() => {
    const nextRevisions: Record<string, string | null> = {}
    for (const item of items ?? []) {
      nextRevisions[item.repository_id] = normalizeRevision(item.revision)
    }
    const prevRevisions = itemsRevisionByRepoRef.current
    itemsRevisionByRepoRef.current = nextRevisions

    const staleRepos = new Set<string>()
    for (const [repoId, prevRev] of Object.entries(prevRevisions)) {
      if (!(repoId in nextRevisions) || nextRevisions[repoId] !== prevRev) {
        staleRepos.add(repoId)
      }
    }
    for (const repoId of Object.keys(fitAbortByRepoRef.current)) {
      if (!(repoId in nextRevisions)) {
        staleRepos.add(repoId)
      }
    }
    for (const repoId of staleRepos) {
      invalidateFitRepo(repoId)
    }

    const prune = <T,>(prev: Record<string, T>): Record<string, T> => {
      let changed = false
      const next = { ...prev }
      for (const repoId of Object.keys(next)) {
        const currentRev = nextRevisions[repoId]
        if (currentRev === undefined || staleRepos.has(repoId)) {
          delete next[repoId]
          changed = true
        }
      }
      return changed ? next : prev
    }

    setFitByRepo((prev) => {
      let changed = false
      const next = { ...prev }
      for (const [repoId, detail] of Object.entries(prev)) {
        const currentRev = nextRevisions[repoId]
        if (
          currentRev === undefined ||
          detail.revision !== currentRev ||
          detail.nodeId !== nodeIdRef.current ||
          detail.analysis.repository_id !== repoId ||
          detail.analysis.node_id !== nodeIdRef.current
        ) {
          delete next[repoId]
          changed = true
          if (!staleRepos.has(repoId)) {
            invalidateFitRepo(repoId)
          }
        }
      }
      return changed ? next : prev
    })
    setFitErrorByRepo(prune)
    setFitLoadingByRepo(prune)
    setExpandedFitRepos(prune)
  }, [items, invalidateFitRepo])

  // F5 / re-entry: restore Job IDs from sessionStorage via GET only (no POST).
  // Mark every draft-backed row restoring up front so sequential GET order cannot
  // leave later rows clickable for a duplicate Download.
  useEffect(() => {
    if (!nodeId || items == null) return
    // Do not restore against a stale previous-Node list still on screen.
    if (itemsBoundKey !== catalogContextKey) return
    const gen = ++downloadRestoreGenRef.current
    const controllers: AbortController[] = []
    let cancelled = false

    const pending: Array<{
      item: HfCatalogModel
      key: string
      requestedRevision: string | null
      jobId: string
    }> = []
    for (const item of items) {
      const requestedRevision = normalizeRequestedRevision(item.revision)
      const key = catalogDownloadTrackKey(
        nodeId,
        item.repository_id,
        requestedRevision,
      )
      if (downloadByKeyRef.current[key]) continue
      const draft = readCatalogDownloadDraft(storage, {
        nodeId,
        repositoryId: item.repository_id,
        requestedRevision,
      })
      if (!draft) continue
      pending.push({ item, key, requestedRevision, jobId: draft.jobId })
    }

    if (pending.length > 0) {
      setDownloadRestoringByKey((prev) => {
        const next = { ...prev }
        for (const { key } of pending) next[key] = true
        return next
      })
    }

    const clearRestoring = (key: string) => {
      if (gen !== downloadRestoreGenRef.current) return
      setDownloadRestoringByKey((prev) => {
        if (!prev[key]) return prev
        const next = { ...prev }
        delete next[key]
        return next
      })
    }

    const restoreOne = async (entry: (typeof pending)[number]) => {
      const { item, key, requestedRevision, jobId } = entry
      if (downloadByKeyRef.current[key]) {
        clearRestoring(key)
        return
      }
      const controller = new AbortController()
      controllers.push(controller)
      try {
        const job = await getHfDownload(jobId, controller.signal)
        if (
          cancelled ||
          gen !== downloadRestoreGenRef.current ||
          nodeIdRef.current !== nodeId ||
          controller.signal.aborted
        ) {
          return
        }
        if (
          !jobMatchesTrack(
            job,
            nodeId,
            item.repository_id,
            requestedRevision,
          )
        ) {
          clearCatalogDownloadDraft(storage, {
            nodeId,
            repositoryId: item.repository_id,
            requestedRevision,
          })
          setDownloadPollWarningByKey((prev) => ({
            ...prev,
            [key]: '저장된 다운로드 Job이 현재 모델/Node/revision과 맞지 않아 추적을 정리했습니다.',
          }))
          return
        }
        setDownloadByKey((prev) => ({
          ...prev,
          [key]: {
            job,
            nodeId,
            repositoryId: item.repository_id,
            requestedRevision,
            lastSuccessAt: new Date(),
          },
        }))
        setDownloadPollWarningByKey((prev) => {
          if (!(key in prev)) return prev
          const next = { ...prev }
          delete next[key]
          return next
        })
      } catch (err) {
        if (
          cancelled ||
          gen !== downloadRestoreGenRef.current ||
          controller.signal.aborted
        ) {
          return
        }
        if (err instanceof ApiError && err.status === 404) {
          clearCatalogDownloadDraft(storage, {
            nodeId,
            repositoryId: item.repository_id,
            requestedRevision,
          })
          setDownloadPollWarningByKey((prev) => ({
            ...prev,
            [key]: '저장된 다운로드 Job을 찾을 수 없어 추적을 정리했습니다.',
          }))
          return
        }
        // Transient failure: keep draft so Download stays locked until retry
        // succeeds or tracking is cleared (404 / mismatch).
        setDownloadPollWarningByKey((prev) => ({
          ...prev,
          [key]:
            err instanceof ApiError
              ? `다운로드 Job 복원 조회에 실패했습니다: ${err.message}`
              : '다운로드 Job 복원 조회에 실패했습니다. 다시 시도할 수 있습니다.',
        }))
      } finally {
        clearRestoring(key)
      }
    }

    void (async () => {
      for (const entry of pending) {
        if (cancelled || gen !== downloadRestoreGenRef.current) return
        await restoreOne(entry)
      }
    })()

    return () => {
      cancelled = true
      downloadRestoreGenRef.current += 1
      for (const c of controllers) c.abort()
    }
  }, [nodeId, items, storage, itemsBoundKey, catalogContextKey])

  const activeDownloadSignature = Object.values(downloadByKey)
    .filter((t) => isActiveDownloadStatus(t.job.status))
    .map((t) => t.job.job_id)
    .sort()
    .join('|')

  // Active Jobs: single-flight poll. Next GET is scheduled only after the prior
  // cycle finishes (no abort of in-flight GET on the 2s cadence). Cleanup on
  // unmount / Node change / terminal signature still aborts outstanding work.
  useEffect(() => {
    if (!activeDownloadSignature) return
    const gen = ++downloadPollGenRef.current
    let cancelled = false
    let timer: number | null = null
    let inFlight = false
    const controllers = new Map<string, AbortController>()

    const scheduleNext = () => {
      if (cancelled || gen !== downloadPollGenRef.current) return
      const stillActive = Object.values(downloadByKeyRef.current).some((t) =>
        isActiveDownloadStatus(t.job.status),
      )
      if (!stillActive) return
      timer = window.setTimeout(() => void pollOnce(), POLL_MS)
    }

    const pollOnce = async () => {
      if (cancelled || gen !== downloadPollGenRef.current) return
      if (inFlight) return
      inFlight = true
      try {
        const active = Object.entries(downloadByKeyRef.current).filter(([, t]) =>
          isActiveDownloadStatus(t.job.status),
        )
        for (const [key, tracked] of active) {
          if (cancelled || gen !== downloadPollGenRef.current) return
          const controller = new AbortController()
          controllers.set(key, controller)
          try {
            const updated = await getHfDownload(
              tracked.job.job_id,
              controller.signal,
            )
            if (
              cancelled ||
              gen !== downloadPollGenRef.current ||
              controller.signal.aborted
            ) {
              continue
            }
            if (updated.job_id !== tracked.job.job_id) continue
            if (
              !jobMatchesTrack(
                updated,
                tracked.nodeId,
                tracked.repositoryId,
                tracked.requestedRevision,
              )
            ) {
              continue
            }
            if (nodeIdRef.current !== tracked.nodeId) continue
            setDownloadByKey((prev) => {
              const cur = prev[key]
              if (!cur || cur.job.job_id !== updated.job_id) return prev
              return {
                ...prev,
                [key]: {
                  ...cur,
                  job: updated,
                  lastSuccessAt: new Date(),
                },
              }
            })
            setDownloadPollWarningByKey((prev) => {
              if (!(key in prev)) return prev
              const next = { ...prev }
              delete next[key]
              return next
            })
          } catch (err) {
            if (
              cancelled ||
              gen !== downloadPollGenRef.current ||
              controller.signal.aborted
            ) {
              continue
            }
            if (err instanceof DOMException && err.name === 'AbortError') {
              continue
            }
            if (err instanceof ApiError && err.status === 404) {
              clearCatalogDownloadDraft(storage, {
                nodeId: tracked.nodeId,
                repositoryId: tracked.repositoryId,
                requestedRevision: tracked.requestedRevision,
              })
              setDownloadByKey((prev) => {
                if (!(key in prev)) return prev
                const next = { ...prev }
                delete next[key]
                return next
              })
              setDownloadPollWarningByKey((prev) => ({
                ...prev,
                [key]: '다운로드 Job이 더 이상 존재하지 않아 추적을 정리했습니다.',
              }))
              continue
            }
            setDownloadPollWarningByKey((prev) => ({
              ...prev,
              [key]:
                err instanceof ApiError
                  ? `다운로드 상태 갱신에 실패했습니다: ${err.message}`
                  : '다운로드 상태 갱신에 실패했습니다. 마지막 정상 상태를 유지합니다.',
            }))
          } finally {
            if (controllers.get(key) === controller) {
              controllers.delete(key)
            }
          }
        }
      } finally {
        inFlight = false
        scheduleNext()
      }
    }

    void pollOnce()
    return () => {
      cancelled = true
      downloadPollGenRef.current += 1
      if (timer != null) window.clearTimeout(timer)
      for (const c of controllers.values()) c.abort()
      controllers.clear()
    }
  }, [activeDownloadSignature, storage])

  const retryDownloadStatus = async (
    trackKey: string,
    item?: HfCatalogModel,
  ) => {
    const tracked = downloadByKeyRef.current[trackKey]
    let jobId = tracked?.job.job_id
    let nodeIdForJob = tracked?.nodeId
    let repositoryId = tracked?.repositoryId
    let requestedRevision = tracked?.requestedRevision ?? null

    if (!jobId && item && nodeId) {
      const draft = readCatalogDownloadDraft(storage, {
        nodeId,
        repositoryId: item.repository_id,
        requestedRevision: normalizeRequestedRevision(item.revision),
      })
      if (!draft) return
      jobId = draft.jobId
      nodeIdForJob = draft.nodeId
      repositoryId = draft.repositoryId
      requestedRevision = draft.requestedRevision
      setDownloadRestoringByKey((prev) => ({ ...prev, [trackKey]: true }))
    }
    if (!jobId || !nodeIdForJob || !repositoryId) return

    try {
      const updated = await getHfDownload(jobId)
      if (
        !jobMatchesTrack(
          updated,
          nodeIdForJob,
          repositoryId,
          requestedRevision,
        )
      ) {
        clearCatalogDownloadDraft(storage, {
          nodeId: nodeIdForJob,
          repositoryId,
          requestedRevision,
        })
        setDownloadPollWarningByKey((prev) => ({
          ...prev,
          [trackKey]:
            '저장된 다운로드 Job이 현재 모델/Node/revision과 맞지 않아 추적을 정리했습니다.',
        }))
        return
      }
      if (nodeIdRef.current !== nodeIdForJob) return
      setDownloadByKey((prev) => ({
        ...prev,
        [trackKey]: {
          job: updated,
          nodeId: nodeIdForJob,
          repositoryId,
          requestedRevision,
          lastSuccessAt: new Date(),
        },
      }))
      setDownloadPollWarningByKey((prev) => {
        if (!(trackKey in prev)) return prev
        const next = { ...prev }
        delete next[trackKey]
        return next
      })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        clearCatalogDownloadDraft(storage, {
          nodeId: nodeIdForJob,
          repositoryId,
          requestedRevision,
        })
        setDownloadByKey((prev) => {
          if (!(trackKey in prev)) return prev
          const next = { ...prev }
          delete next[trackKey]
          return next
        })
        setDownloadPollWarningByKey((prev) => ({
          ...prev,
          [trackKey]: '다운로드 Job이 더 이상 존재하지 않아 추적을 정리했습니다.',
        }))
        return
      }
      setDownloadPollWarningByKey((prev) => ({
        ...prev,
        [trackKey]:
          err instanceof ApiError
            ? tracked
              ? `다운로드 상태 갱신에 실패했습니다: ${err.message}`
              : `다운로드 Job 복원 조회에 실패했습니다: ${err.message}`
            : tracked
              ? '다운로드 상태 갱신에 실패했습니다. 다시 시도할 수 있습니다.'
              : '다운로드 Job 복원 조회에 실패했습니다. 다시 시도할 수 있습니다.',
      }))
    } finally {
      setDownloadRestoringByKey((prev) => {
        if (!prev[trackKey]) return prev
        const next = { ...prev }
        delete next[trackKey]
        return next
      })
    }
  }

  const onSubmitSearch = (event: FormEvent) => {
    event.preventDefault()
    syncUrl({
      q: draftQ,
      modelType,
      page: 1,
      nodeId,
      fitOnly,
    })
  }

  const listMatchesContext = itemsBoundKey === catalogContextKey
  const catalogListPending =
    (loading && items === null) ||
    (items !== null && !listMatchesContext)

  const onAnalyze = async (item: HfCatalogModel) => {
    if (!nodeId) return
    // Previous Node/filter list may still be on screen while a new catalog
    // fetch is in flight — never Fit against a stale row as if it were current.
    if (itemsBoundKeyRef.current !== catalogContextKeyRef.current) return
    const repoId = item.repository_id
    const requestNodeId = nodeId
    const requestRevision = normalizeRevision(item.revision)
    const requestContextKey = catalogContextKeyRef.current
    const requestedAt = new Date()
    const gen = (fitGenByRepoRef.current[repoId] ?? 0) + 1
    fitGenByRepoRef.current[repoId] = gen

    fitAbortByRepoRef.current[repoId]?.abort()
    const controller = new AbortController()
    fitAbortByRepoRef.current[repoId] = controller

    setFitLoadingByRepo((prev) => ({
      ...prev,
      [repoId]: { revision: requestRevision },
    }))
    setFitErrorByRepo((prev) => {
      if (!(repoId in prev)) return prev
      const next = { ...prev }
      delete next[repoId]
      return next
    })

    const isCurrentAnalyzeTarget = (): boolean =>
      !controller.signal.aborted &&
      fitGenByRepoRef.current[repoId] === gen &&
      nodeIdRef.current === requestNodeId &&
      catalogContextKeyRef.current === requestContextKey &&
      itemsBoundKeyRef.current === requestContextKey &&
      (itemsRevisionByRepoRef.current[repoId] ?? null) === requestRevision

    try {
      const result = await analyzeHfResourceFit(
        {
          repositoryId: repoId,
          revision: requestRevision,
          nodeId: requestNodeId,
          modelType: item.model_type,
        },
        controller.signal,
      )
      if (!isCurrentAnalyzeTarget()) return
      if (result.repository_id !== repoId) return
      if (result.node_id !== requestNodeId) return
      if (
        result.revision != null &&
        result.revision !== requestRevision
      ) {
        return
      }
      setFitByRepo((prev) => ({
        ...prev,
        [repoId]: {
          analysis: result,
          requestedAt,
          nodeId: requestNodeId,
          revision: requestRevision,
        },
      }))
      pendingFitRevealRepoRef.current = repoId
      setExpandedFitRepos((prev) => ({ ...prev, [repoId]: true }))
    } catch (err) {
      if (!isCurrentAnalyzeTarget()) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      setFitErrorByRepo((prev) => ({
        ...prev,
        [repoId]: {
          message:
            err instanceof ApiError
              ? err.message
              : '자원 적합도 분석에 실패했습니다.',
          revision: requestRevision,
        },
      }))
    } finally {
      if (fitGenByRepoRef.current[repoId] === gen) {
        setFitLoadingByRepo((prev) => {
          if (!prev[repoId]) return prev
          const next = { ...prev }
          delete next[repoId]
          return next
        })
      }
    }
  }

  const toggleFitExpanded = (repoId: string) => {
    setExpandedFitRepos((prev) => ({
      ...prev,
      [repoId]: !prev[repoId],
    }))
  }

  const onDownload = async (item: HfCatalogModel) => {
    if (!nodeId) return
    if (itemsBoundKeyRef.current !== catalogContextKeyRef.current) return
    const requestedRevision = normalizeRequestedRevision(item.revision)
    const key = catalogDownloadTrackKey(
      nodeId,
      item.repository_id,
      requestedRevision,
    )
    const existing = downloadByKey[key]
    if (existing && isActiveDownloadStatus(existing.job.status)) return
    // READY already downloaded for this Node/repo/revision — no duplicate POST.
    if (isTerminalReadyDownload(existing?.job)) return
    if (downloadStartingByKey[key] || downloadRestoringByKey[key]) return
    // Draft without a restored Job means restore is pending or temporarily
    // failed — never POST a duplicate Download until tracking is cleared.
    if (
      !existing &&
      readCatalogDownloadDraft(storage, {
        nodeId,
        repositoryId: item.repository_id,
        requestedRevision,
      })
    ) {
      return
    }
    const modelType = resolveDownloadModelType(
      item,
      downloadTypeByRepo[item.repository_id],
    )
    if (!modelType) {
      setError(
        `${item.repository_id}: model type is unknown. Select LLM/VLM/EMBEDDING before Download.`,
      )
      return
    }

    setDownloadStartingByKey((prev) => ({ ...prev, [key]: true }))
    setError(null)
    setDownloadPollWarningByKey((prev) => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    try {
      const job = await startHfDownload({
        repositoryId: item.repository_id,
        revision: requestedRevision,
        nodeId,
        modelType,
      })
      if (
        !jobMatchesTrack(
          job,
          nodeId,
          item.repository_id,
          requestedRevision,
        )
      ) {
        setError('다운로드 응답이 요청한 Node/모델/revision과 일치하지 않습니다.')
        return
      }
      writeCatalogDownloadDraft(storage, {
        schema: 1,
        jobId: job.job_id,
        nodeId,
        repositoryId: item.repository_id,
        requestedRevision,
        savedAt: Date.now(),
      })
      setDownloadByKey((prev) => ({
        ...prev,
        [key]: {
          job,
          nodeId,
          repositoryId: item.repository_id,
          requestedRevision,
          lastSuccessAt: new Date(),
        },
      }))
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : '다운로드를 시작하지 못했습니다.',
      )
    } finally {
      setDownloadStartingByKey((prev) => {
        if (!prev[key]) return prev
        const next = { ...prev }
        delete next[key]
        return next
      })
    }
  }

  const renderDownloadState = (item: HfCatalogModel) => {
    if (!nodeId) return null
    const requestedRevision = normalizeRequestedRevision(item.revision)
    const key = catalogDownloadTrackKey(
      nodeId,
      item.repository_id,
      requestedRevision,
    )
    const tracked = downloadByKey[key]
    const restoring = Boolean(downloadRestoringByKey[key])
    const pollWarning = downloadPollWarningByKey[key]
    const starting = Boolean(downloadStartingByKey[key])

    // Active restore GET in flight — distinct from a failed restore query.
    if (restoring && !tracked) {
      return (
        <div
          className="secondary-text catalog-download-status"
          role="status"
        >
          저장된 다운로드 Job 복원 중…
        </div>
      )
    }

    if (!tracked && pollWarning) {
      const draftStillPresent = Boolean(
        readCatalogDownloadDraft(storage, {
          nodeId,
          repositoryId: item.repository_id,
          requestedRevision,
        }),
      )
      return (
        <div className="catalog-download-status">
          <p className="catalog-fit-error" role="status">
            {pollWarning}
          </p>
          <div className="catalog-row-actions catalog-row-actions--inline">
            {draftStillPresent ? (
              <button
                type="button"
                className="btn btn--ghost"
                disabled={restoring}
                onClick={() => void retryDownloadStatus(key, item)}
              >
                상태 다시 조회
              </button>
            ) : (
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  setDownloadPollWarningByKey((prev) => {
                    if (!(key in prev)) return prev
                    const next = { ...prev }
                    delete next[key]
                    return next
                  })
                }}
              >
                안내 닫기
              </button>
            )}
          </div>
        </div>
      )
    }

    // Draft still present while restore is queued (not yet failing).
    if (
      !tracked &&
      readCatalogDownloadDraft(storage, {
        nodeId,
        repositoryId: item.repository_id,
        requestedRevision,
      })
    ) {
      return (
        <div
          className="secondary-text catalog-download-status"
          role="status"
        >
          저장된 다운로드 Job 복원 중…
        </div>
      )
    }

    if (!tracked) return null

    const job = tracked.job
    const status = job.status.toUpperCase()
    const trackMatched = jobMatchesTrack(
      job,
      nodeId,
      item.repository_id,
      requestedRevision,
    )
    const bytesLine =
      job.bytes_downloaded != null || job.total_bytes != null
        ? `${job.bytes_downloaded != null ? formatBytes(job.bytes_downloaded) : '—'} / ${
            job.total_bytes != null ? formatBytes(job.total_bytes) : '—'
          }`
        : null
    const cacheReadyForDeploy =
      trackMatched && isDownloadCacheReadyForDeploy(job)

    return (
      <div className="secondary-text catalog-download-status">
        <StatusBadge status={job.status} />
        {isActiveDownloadStatus(job.status) ? (
          <div>
            {job.progress_percent != null
              ? `진행률 ${job.progress_percent}%`
              : '진행률 없음 (서버 미제공)'}
          </div>
        ) : null}
        {bytesLine ? <div>{bytesLine}</div> : null}
        {status === 'READY' ? (
          <div>
            {job.resolved_revision ? `rev ${job.resolved_revision}` : null}
            {job.size_bytes != null ? ` · ${formatBytes(job.size_bytes)}` : null}
          </div>
        ) : null}
        {job.local_path && status === 'READY' ? (
          <TruncatedValue
            className="mono"
            value={job.local_path}
            abbreviated={abbreviateMiddle(job.local_path, 28, 12)}
          />
        ) : null}
        {status === 'READY' && trackMatched ? (
          <div className="catalog-download-next">
            <p className="panel-hint">
              Downloaded ≠ Deployed ≠ Published. 다운로드 READY는 캐시 준비일 뿐
              모델 실행·Alias 게시 성공을 의미하지 않습니다.
            </p>
            {cacheReadyForDeploy && job.node_model_cache_id ? (
              <Link
                className="btn"
                to={`/models/cache/${encodeURIComponent(job.node_model_cache_id)}/deploy`}
              >
                배포 설정
              </Link>
            ) : (
              <>
                <p className="secondary-text">
                  Cache ID가 없거나 캐시가 아직 READY로 확인되지 않았습니다. Model
                  Cache에서 준비 상태를 확인하세요.
                </p>
                <Link
                  className="btn btn--ghost"
                  to={`/models/cache?node_id=${encodeURIComponent(nodeId)}`}
                >
                  Model Cache 확인
                </Link>
              </>
            )}
          </div>
        ) : null}
        {job.error_message && (status === 'FAILED' || status === 'CANCELED') ? (
          <div>{job.error_message}</div>
        ) : null}
        {tracked.lastSuccessAt ? (
          <div>
            마지막 성공 갱신:{' '}
            {formatApiDateTime(tracked.lastSuccessAt.toISOString())}
          </div>
        ) : null}
        {pollWarning ? (
          <p className="catalog-fit-error" role="status">
            {pollWarning}{' '}
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => void retryDownloadStatus(key)}
            >
              상태 다시 조회
            </button>
          </p>
        ) : null}
        {(status === 'FAILED' || status === 'CANCELED') && !starting ? (
          <button
            type="button"
            className="btn"
            disabled={!nodeId || starting || restoring || catalogListPending}
            onClick={() => void onDownload(item)}
          >
            Retry download
          </button>
        ) : null}
      </div>
    )
  }

  return (
    <AppShell
      title="Model Catalog"
      description="Hugging Face 모델을 검색하고 Node에 다운로드합니다. FIT는 자문용이며, 다운로드 READY는 배포·게시 성공을 의미하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← Models / Versions
        </Link>
        <Link className="btn btn--ghost" to="/models/cache">
          Model Cache
        </Link>
        <span className="secondary-text">
          FIT 결과는 자문용이며 배포 성공을 보장하지 않습니다.
        </span>
      </div>

      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="catalog-q">
          <span>검색</span>
          <input
            id="catalog-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="repository / keyword"
          />
        </label>
        <label className="toolbar__field" htmlFor="catalog-type">
          <span>Type</span>
          <select
            id="catalog-type"
            value={modelType ?? 'ALL'}
            onChange={(e) => {
              const next =
                e.target.value === 'ALL' ? null : e.target.value.toUpperCase()
              syncUrl({
                q,
                modelType: next && KNOWN_TYPES.has(next) ? next : null,
                page: 1,
                nodeId,
                fitOnly,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="LLM">LLM</option>
            <option value="VLM">VLM</option>
            <option value="EMBEDDING">EMBEDDING</option>
          </select>
        </label>
        <label className="toolbar__field" htmlFor="catalog-node">
          <span>Node</span>
          <select
            id="catalog-node"
            value={nodeId}
            onChange={(e) => {
              syncUrl({
                q,
                modelType,
                page: 1,
                nodeId: e.target.value,
                fitOnly: e.target.value ? fitOnly : false,
              })
              // Fit details clear via catalogContextKey effect; downloads are
              // node-scoped and must reset here. Drafts stay in sessionStorage
              // for F5 / re-select restore (GET only).
              setDownloadByKey({})
              setDownloadRestoringByKey({})
              setDownloadPollWarningByKey({})
              setDownloadStartingByKey({})
              downloadRestoreGenRef.current += 1
              downloadPollGenRef.current += 1
            }}
          >
            <option value="">선택…</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.status})
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="catalog-fit-only">
          <span>설치 가능만</span>
          <input
            id="catalog-fit-only"
            type="checkbox"
            checked={fitOnly}
            disabled={!nodeId}
            onChange={(e) =>
              syncUrl({
                q,
                modelType,
                page: 1,
                nodeId,
                fitOnly: e.target.checked,
              })
            }
          />
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
        </div>
      </form>

      {error ? (
        <SectionError title="카탈로그 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Hugging Face 카탈로그를 불러오는 중…" />
      ) : null}

      {catalogListPending && items !== null ? (
        <p className="catalog-list-pending" role="status">
          {refreshing || !listMatchesContext
            ? 'Node/필터 변경에 맞게 카탈로그를 다시 불러오는 중…'
            : '카탈로그를 불러오는 중…'}
        </p>
      ) : null}

      {items !== null && items.length === 0 && listMatchesContext ? (
        <p className="secondary-text">조건에 맞는 모델이 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div
            className={`table-wrap${catalogListPending ? ' catalog-table-wrap--pending' : ''}`}
            aria-busy={catalogListPending || undefined}
          >
            <table className="data-table catalog-table">
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Type</th>
                  <th scope="col">Size / VRAM est.</th>
                  <th scope="col">Fit</th>
                  <th scope="col">Reason</th>
                  <th scope="col">Next</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const repoId = item.repository_id
                  const fitDetail =
                    listMatchesContext &&
                    fitDetailMatchesCurrent(
                      fitByRepo[repoId],
                      item,
                      nodeId,
                    )
                      ? fitByRepo[repoId]
                      : undefined
                  const analysis = fitDetail?.analysis
                  const catalogFit =
                    listMatchesContext
                      ? item.resource_fit?.result || null
                      : null
                  const itemRevision = normalizeRevision(item.revision)
                  const analyzing =
                    listMatchesContext &&
                    fitLoadingByRepo[repoId]?.revision === itemRevision
                  const fitErrorEntry = fitErrorByRepo[repoId]
                  const fitError =
                    listMatchesContext &&
                    fitErrorEntry &&
                    fitErrorEntry.revision === itemRevision
                      ? fitErrorEntry.message
                      : undefined
                  const expanded = Boolean(
                    listMatchesContext &&
                      expandedFitRepos[repoId] &&
                      analysis,
                  )
                  const itemRequestedRevision = normalizeRequestedRevision(
                    item.revision,
                  )
                  const downloadTrackKey = nodeId
                    ? catalogDownloadTrackKey(
                        nodeId,
                        repoId,
                        itemRequestedRevision,
                      )
                    : ''
                  const trackedDownload =
                    listMatchesContext && downloadTrackKey
                      ? downloadByKey[downloadTrackKey]
                      : undefined
                  const job = trackedDownload?.job
                  const downloadActive =
                    job != null && isActiveDownloadStatus(job.status)
                  const downloadReady = isTerminalReadyDownload(job)
                  const downloadRestoring = Boolean(
                    listMatchesContext &&
                      downloadTrackKey &&
                      downloadRestoringByKey[downloadTrackKey],
                  )
                  const downloadStarting = Boolean(
                    listMatchesContext &&
                      downloadTrackKey &&
                      downloadStartingByKey[downloadTrackKey],
                  )
                  const downloadPollWarning =
                    listMatchesContext && downloadTrackKey
                      ? downloadPollWarningByKey[downloadTrackKey]
                      : undefined
                  const downloadDraftLocked =
                    listMatchesContext &&
                    Boolean(nodeId) &&
                    trackedDownload == null &&
                    Boolean(
                      readCatalogDownloadDraft(storage, {
                        nodeId,
                        repositoryId: repoId,
                        requestedRevision: itemRequestedRevision,
                      }),
                    )
                  const diskBlocked = isDiskInsufficientForDownload(analysis)
                  const resolvedType = resolveDownloadModelType(
                    item,
                    downloadTypeByRepo[repoId],
                  )
                  const typeMissing = resolvedType == null
                  const reasonText = listMatchesContext
                    ? catalogFitSummaryReason(item)
                    : '목록 갱신 중…'
                  const downloadTitle = catalogListPending
                    ? '카탈로그 목록을 갱신하는 동안 Fit/Download를 실행할 수 없습니다.'
                    : downloadReady
                      ? '이미 다운로드 READY입니다. 아래 링크로 Model Cache / 배포 설정을 확인하세요.'
                      : typeMissing
                        ? 'Select LLM/VLM/EMBEDDING before Download'
                        : downloadRestoring
                          ? '저장된 다운로드 Job 복원 중…'
                          : downloadDraftLocked && downloadPollWarning
                            ? '다운로드 Job 복원 조회에 실패했습니다. 상태 다시 조회를 사용하세요.'
                            : downloadDraftLocked
                              ? '저장된 다운로드 Job 복원 중…'
                              : downloadDisabledReason(nodeId, analysis)
                  const downloadDisabled =
                    catalogListPending ||
                    !nodeId ||
                    diskBlocked ||
                    typeMissing ||
                    downloadActive ||
                    downloadReady ||
                    downloadStarting ||
                    downloadRestoring ||
                    downloadDraftLocked
                  const fitDisabled =
                    catalogListPending || !nodeId || analyzing
                  const nodeLabel =
                    nodes.find((n) => n.id === (fitDetail?.nodeId || nodeId))
                      ?.name || fitDetail?.nodeId || nodeId || '—'

                  return (
                    <Fragment key={repoId}>
                      <tr
                        aria-busy={analyzing || catalogListPending || undefined}
                        data-fit-loading={analyzing ? 'true' : undefined}
                        data-catalog-pending={
                          catalogListPending ? 'true' : undefined
                        }
                      >
                        <td>
                          <div className="mono">{repoId}</div>
                          <div className="secondary-text">
                            {item.pipeline_tag || '—'}
                            {item.gated ? ' · gated' : ''}
                            {item.quantization_hint
                              ? ` · ${item.quantization_hint}`
                              : ''}
                          </div>
                        </td>
                        <td>
                          {item.model_type ? (
                            item.model_type
                          ) : (
                            <select
                              aria-label={`Type for ${repoId}`}
                              value={downloadTypeByRepo[repoId] ?? ''}
                              onChange={(e) =>
                                setDownloadTypeByRepo((prev) => ({
                                  ...prev,
                                  [repoId]: e.target.value,
                                }))
                              }
                            >
                              <option value="">Select…</option>
                              <option value="LLM">LLM</option>
                              <option value="VLM">VLM</option>
                              <option value="EMBEDDING">EMBEDDING</option>
                            </select>
                          )}
                        </td>
                        <td>
                          <div>
                            {formatBytes(item.estimated_download_size_bytes)}
                          </div>
                          <div className="secondary-text">
                            {item.estimated_required_vram_mb != null
                              ? `~${item.estimated_required_vram_mb} MiB VRAM`
                              : 'VRAM unknown'}
                          </div>
                        </td>
                        <td>
                          <div className="catalog-fit-summary">
                            <span className="secondary-text">목록 요약</span>
                            {catalogFit ? (
                              <StatusBadge status={catalogFit} />
                            ) : (
                              <span className="secondary-text">
                                {listMatchesContext ? '—' : '갱신 중'}
                              </span>
                            )}
                            {analyzing ? (
                              <div
                                className="catalog-fit-summary__detail"
                                data-testid={`fit-row-status-${repoId}`}
                              >
                                <span className="secondary-text">상태</span>
                                <span className="catalog-fit-pill catalog-fit-pill--loading">
                                  분석 중
                                </span>
                              </div>
                            ) : analysis ? (
                              <div
                                className="catalog-fit-summary__detail"
                                data-testid={`fit-row-status-${repoId}`}
                              >
                                <span className="secondary-text">분석 완료</span>
                                <StatusBadge status={analysis.result} />
                              </div>
                            ) : fitError ? (
                              <div
                                className="catalog-fit-summary__detail"
                                data-testid={`fit-row-status-${repoId}`}
                              >
                                <span className="secondary-text">상태</span>
                                <span className="catalog-fit-pill catalog-fit-pill--error">
                                  분석 실패
                                </span>
                              </div>
                            ) : null}
                          </div>
                        </td>
                        <td className="catalog-reason-cell">
                          <TruncatedValue
                            className="secondary-text"
                            value={reasonText}
                            abbreviated={abbreviateMiddle(reasonText, 48, 16)}
                          />
                        </td>
                        <td className="catalog-actions-cell">
                          <div className="catalog-row-actions">
                            <button
                              type="button"
                              className="btn"
                              disabled={fitDisabled}
                              aria-busy={analyzing || undefined}
                              onClick={() => void onAnalyze(item)}
                            >
                              {analyzing
                                ? '분석 중…'
                                : fitError
                                  ? '재시도'
                                  : analysis
                                    ? '재분석'
                                    : 'Fit 분석'}
                            </button>
                            {analysis ? (
                              <button
                                type="button"
                                className="btn btn--ghost"
                                onClick={() => toggleFitExpanded(repoId)}
                                aria-expanded={expanded}
                              >
                                {expanded ? '접기' : '상세 펼치기'}
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="btn"
                              disabled={downloadDisabled}
                              title={downloadTitle}
                              onClick={() => void onDownload(item)}
                            >
                              {downloadStarting
                                ? 'Starting…'
                                : downloadRestoring
                                  ? 'Restoring…'
                                  : downloadDraftLocked && downloadPollWarning
                                    ? '조회 실패'
                                    : downloadDraftLocked
                                      ? 'Restoring…'
                                      : downloadActive
                                        ? 'Downloading…'
                                        : downloadReady
                                          ? 'Downloaded'
                                          : 'Download'}
                            </button>
                          </div>
                          {analyzing ? (
                            <p
                              className="secondary-text catalog-fit-loading"
                              role="status"
                            >
                              Fit 분석 중… (advisory, 배포 실행 없음)
                            </p>
                          ) : null}
                          {fitError ? (
                            <p
                              className="catalog-fit-error"
                              role="alert"
                              data-testid={`fit-error-${repoId}`}
                            >
                              {fitError} — 재시도로 다시 분석할 수 있습니다.
                            </p>
                          ) : null}
                          {listMatchesContext
                            ? renderDownloadState(item)
                            : null}
                        </td>
                      </tr>
                      {expanded && analysis && fitDetail ? (
                        <tr className="catalog-fit-detail-row">
                          <td colSpan={6}>
                            <div
                              className="catalog-fit-detail"
                              data-testid={`fit-detail-${repoId}`}
                              tabIndex={-1}
                            >
                              <div className="catalog-fit-detail__head">
                                <h3 className="section-subheading" tabIndex={-1}>
                                  Fit 상세 분석 (advisory)
                                </h3>
                                <p className="panel-hint" role="note">
                                  현재 선택된 Node(<strong>{nodeLabel}</strong>)의
                                  전체 GPU를 비교한 advisory 결과입니다. Catalog에서
                                  GPU를 고르지 않으며, 실제 GPU 선택·Fresh Fit은
                                  Cache Deploy Wizard에서 수행합니다. GPU VRAM을
                                  합산하지 않고, Deployment를 실행하지 않습니다.
                                </p>
                              </div>
                              <dl className="config-kv-grid">
                                <div>
                                  <dt>종합 결과</dt>
                                  <dd>
                                    <StatusBadge status={analysis.result} />
                                    <div className="secondary-text">
                                      TP={analysis.tensor_parallel}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>분석 Node</dt>
                                  <dd>
                                    {nodeLabel}
                                    <div className="secondary-text mono">
                                      {fitDetail.nodeId}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>모델 / revision</dt>
                                  <dd className="mono">
                                    {analysis.repository_id}
                                    <div className="secondary-text">
                                      revision{' '}
                                      {analysis.revision ||
                                        fitDetail.revision ||
                                        '—'}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>분석 시각</dt>
                                  <dd>
                                    {formatApiDateTime(
                                      fitDetail.requestedAt.toISOString(),
                                    )}
                                    <div className="secondary-text">
                                      클라이언트 요청 시각 (서버 완료 시각 아님)
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>예상 필요 VRAM</dt>
                                  <dd>
                                    {formatMb(
                                      analysis.estimated_required_vram_mb,
                                    )}
                                  </dd>
                                </div>
                                <div>
                                  <dt>디스크 적합성</dt>
                                  <dd>
                                    <StatusBadge
                                      status={diskFitLabel(analysis.disk_ok)}
                                    />
                                    <div className="secondary-text">
                                      free {formatMb(analysis.disk_free_mb)}
                                      {analysis.estimated_download_size_bytes !=
                                      null
                                        ? ` · download est. ${formatBytes(analysis.estimated_download_size_bytes)}`
                                        : ''}
                                    </div>
                                  </dd>
                                </div>
                              </dl>

                              <h4 className="section-subheading">GPU별 판정</h4>
                              <p className="panel-hint">
                                GPU 0 / GPU 1을 각각 독립 카드로 표시합니다. free
                                VRAM을 합산하지 않습니다.
                              </p>
                              {analysis.gpu_results.length > 0 ? (
                                <div
                                  className="catalog-fit-gpu-cards"
                                  data-testid={`fit-gpu-cards-${repoId}`}
                                >
                                  {analysis.gpu_results.map((gpu) => {
                                    const suggested =
                                      analysis.suggested_gpu_device_ids?.includes(
                                        gpu.gpu_device_id,
                                      ) ?? false
                                    return (
                                      <article
                                        key={gpu.gpu_device_id}
                                        className={
                                          suggested
                                            ? 'catalog-fit-gpu-card catalog-fit-gpu-card--suggested'
                                            : 'catalog-fit-gpu-card'
                                        }
                                        data-testid={`fit-gpu-card-${repoId}-${gpu.gpu_index ?? gpu.gpu_device_id}`}
                                      >
                                        <header className="catalog-fit-gpu-card__head">
                                          <div>
                                            <strong>
                                              GPU {gpu.gpu_index ?? '—'}
                                            </strong>
                                            <div className="secondary-text">
                                              {gpu.name || '—'}
                                            </div>
                                          </div>
                                          <StatusBadge status={gpu.result} />
                                        </header>
                                        {suggested ? (
                                          <p className="catalog-fit-gpu-card__badge">
                                            추천 GPU (참고)
                                          </p>
                                        ) : null}
                                        <dl className="catalog-fit-gpu-card__metrics">
                                          <div>
                                            <dt>Free / Total VRAM</dt>
                                            <dd>
                                              {formatMb(gpu.vram_free_mb)} /{' '}
                                              {formatMb(gpu.vram_total_mb)}
                                            </dd>
                                          </div>
                                          <div>
                                            <dt>예상 필요 VRAM</dt>
                                            <dd>
                                              {formatMb(
                                                gpu.estimated_required_vram_mb,
                                              )}
                                            </dd>
                                          </div>
                                          <div>
                                            <dt>Safety margin</dt>
                                            <dd>
                                              {formatMb(gpu.safety_margin_mb)}
                                            </dd>
                                          </div>
                                        </dl>
                                        <div className="catalog-fit-gpu-card__reasons">
                                          <span className="secondary-text">
                                            판단 근거
                                          </span>
                                          {gpu.reasons?.length ? (
                                            <ul className="catalog-fit-list">
                                              {gpu.reasons.map((r) => (
                                                <li key={r}>{r}</li>
                                              ))}
                                            </ul>
                                          ) : (
                                            <p className="secondary-text">—</p>
                                          )}
                                        </div>
                                        {gpu.gpu_device_id ? (
                                          <TruncatedValue
                                            className="secondary-text mono catalog-clip-cell"
                                            value={gpu.gpu_device_id}
                                            abbreviated={abbreviateMiddle(
                                              gpu.gpu_device_id,
                                              10,
                                              8,
                                            )}
                                          />
                                        ) : null}
                                      </article>
                                    )
                                  })}
                                </div>
                              ) : (
                                <p className="secondary-text">
                                  GPU별 결과가 없습니다.
                                </p>
                              )}

                              {analysis.suggested_gpu_device_ids &&
                              analysis.suggested_gpu_device_ids.length > 0 ? (
                                <div className="catalog-fit-detail__block">
                                  <h4 className="section-subheading">
                                    추천 GPU 및 판단 근거
                                  </h4>
                                  <p className="panel-hint">
                                    Node 전체 GPU 비교의 참고 제안입니다. Catalog는
                                    GPU를 선택하지 않으며, Deploy Wizard에서 실제
                                    GPU를 고르고 Fresh Fit을 수행합니다.
                                  </p>
                                  <ul className="catalog-fit-list">
                                    {analysis.suggested_gpu_device_ids.map(
                                      (id) => {
                                        const gpu = analysis.gpu_results.find(
                                          (g) => g.gpu_device_id === id,
                                        )
                                        const label = gpu
                                          ? `GPU ${gpu.gpu_index ?? '—'} (${gpu.name || id})`
                                          : id
                                        return (
                                          <li key={id}>
                                            <strong>{label}</strong>
                                            <div className="secondary-text mono">
                                              {id}
                                            </div>
                                            {gpu?.reasons?.length ? (
                                              <div className="secondary-text">
                                                {gpu.reasons.join('; ')}
                                              </div>
                                            ) : null}
                                          </li>
                                        )
                                      },
                                    )}
                                  </ul>
                                </div>
                              ) : null}

                              <div className="catalog-fit-detail__lists">
                                <div>
                                  <h4 className="section-subheading">
                                    종합 Reasons
                                  </h4>
                                  <ul className="catalog-fit-list">
                                    {analysis.reasons.length > 0 ? (
                                      analysis.reasons.map((r) => (
                                        <li key={r}>
                                          <TruncatedValue
                                            value={r}
                                            abbreviated={abbreviateMiddle(
                                              r,
                                              72,
                                              24,
                                            )}
                                          />
                                        </li>
                                      ))
                                    ) : (
                                      <li className="secondary-text">—</li>
                                    )}
                                  </ul>
                                </div>
                                <div>
                                  <h4 className="section-subheading">Warnings</h4>
                                  <ul className="catalog-fit-list">
                                    {analysis.warnings.length > 0 ? (
                                      analysis.warnings.map((w) => (
                                        <li key={w}>
                                          <TruncatedValue
                                            value={w}
                                            abbreviated={abbreviateMiddle(
                                              w,
                                              72,
                                              24,
                                            )}
                                          />
                                        </li>
                                      ))
                                    ) : (
                                      <li className="secondary-text">—</li>
                                    )}
                                  </ul>
                                </div>
                                <div>
                                  <h4 className="section-subheading">
                                    Assumptions
                                  </h4>
                                  <ul className="catalog-fit-list">
                                    {analysis.assumptions.length > 0 ? (
                                      analysis.assumptions.map((a) => (
                                        <li key={a}>
                                          <TruncatedValue
                                            value={a}
                                            abbreviated={abbreviateMiddle(
                                              a,
                                              72,
                                              24,
                                            )}
                                          />
                                        </li>
                                      ))
                                    ) : (
                                      <li className="secondary-text">—</li>
                                    )}
                                  </ul>
                                </div>
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            hasMore={hasMore}
            onPageChange={(next) =>
              syncUrl({
                q,
                modelType,
                page: next,
                nodeId,
                fitOnly,
              })
            }
          />
        </>
      ) : null}
    </AppShell>
  )
}
