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
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import {
  CATALOG_FIELD_HELP,
  DOWNLOAD_STATUS_LABELS,
  FIT_RESULT_HELP,
  FIT_RESULT_LABELS,
  MODEL_TYPE_HELP,
} from '../utils/uiLabels'
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
  return '대상 서버를 선택하면 GPU 적합성(Fit) 분석으로 상세 결과를 확인할 수 있습니다.'
}

function formatMb(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '정보 없음'
  return `${value} MiB`
}

function displayOrUnknown(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim()
  return trimmed.length > 0 ? trimmed : '정보 없음'
}

function fitResultLabel(result: string | null | undefined): string | undefined {
  const code = (result || '').toUpperCase()
  if (code === 'FIT') return FIT_RESULT_LABELS.FIT
  if (code === 'TIGHT') return FIT_RESULT_LABELS.TIGHT
  if (code === 'INSUFFICIENT') return FIT_RESULT_LABELS.INSUFFICIENT
  if (code === 'UNKNOWN') return FIT_RESULT_LABELS.UNKNOWN
  return undefined
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
  if (!nodeId) return '먼저 대상 서버(Node)를 선택하세요.'
  if (isDiskInsufficientForDownload(detail)) {
    return '디스크 공간이 부족합니다(GPU 메모리 부족과 별개). 다운로드를 시작할 수 없습니다.'
  }
  return undefined
}

function fitResultHelp(result: string | null | undefined): string | undefined {
  const code = (result || '').toUpperCase()
  if (code === 'FIT') return FIT_RESULT_HELP.FIT
  if (code === 'TIGHT') return FIT_RESULT_HELP.TIGHT
  if (code === 'INSUFFICIENT') return FIT_RESULT_HELP.INSUFFICIENT
  if (code === 'UNKNOWN') return FIT_RESULT_HELP.UNKNOWN
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
        `${item.repository_id}: 모델 유형이 확정되지 않았습니다. LLM/VLM/EMBEDDING을 선택한 뒤 다운로드하세요.`,
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
          className="catalog-download-status"
          role="status"
          data-download-state="restoring"
        >
          <p className="catalog-download-status__headline">
            {DOWNLOAD_STATUS_LABELS.restoring}
          </p>
          <p className="secondary-text">저장된 다운로드 Job을 다시 읽는 중…</p>
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
        <div
          className="catalog-download-status"
          data-download-state="poll-failed"
        >
          <p className="catalog-download-status__headline catalog-download-status__headline--warn">
            {DOWNLOAD_STATUS_LABELS.pollFailed}
          </p>
          <p className="catalog-fit-error" role="status">
            <span className="secondary-text">
              다운로드 자체의 실패가 아닙니다. 상태만 다시 조회하세요.{' '}
            </span>
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
          className="catalog-download-status"
          role="status"
          data-download-state="restoring"
        >
          <p className="catalog-download-status__headline">
            {DOWNLOAD_STATUS_LABELS.restoring}
          </p>
          <p className="secondary-text">저장된 다운로드 Job 복원 중…</p>
        </div>
      )
    }

    if (!tracked) {
      if (starting) {
        return (
          <div
            className="catalog-download-status"
            role="status"
            data-download-state="requesting"
          >
            <p className="catalog-download-status__headline">
              {DOWNLOAD_STATUS_LABELS.requesting}
            </p>
          </div>
        )
      }
      return null
    }

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
        ? `${job.bytes_downloaded != null ? formatBytes(job.bytes_downloaded) : '정보 없음'} / ${
            job.total_bytes != null ? formatBytes(job.total_bytes) : '정보 없음'
          }`
        : null
    const cacheReadyForDeploy =
      trackMatched && isDownloadCacheReadyForDeploy(job)
    const active = isActiveDownloadStatus(job.status)
    const isFailed = status === 'FAILED'
    const isCanceled = status === 'CANCELED'
    const terminalRetryable = isFailed || isCanceled
    const headline = starting
      ? DOWNLOAD_STATUS_LABELS.requesting
      : active
        ? DOWNLOAD_STATUS_LABELS.inProgress
        : isFailed
          ? DOWNLOAD_STATUS_LABELS.failed
          : isCanceled
            ? DOWNLOAD_STATUS_LABELS.canceled
            : status === 'READY'
              ? DOWNLOAD_STATUS_LABELS.ready
              : null

    return (
      <div
        className={`catalog-download-status${
          status === 'READY' ? ' catalog-download-status--ready' : ''
        }`}
        data-download-state={
          starting
            ? 'requesting'
            : active
              ? 'in-progress'
              : isFailed
                ? 'failed'
                : isCanceled
                  ? 'canceled'
                  : status === 'READY'
                    ? 'ready'
                    : 'tracked'
        }
      >
        {headline ? (
          <p
            className={`catalog-download-status__headline${
              isFailed
                ? ' catalog-download-status__headline--error'
                : isCanceled
                  ? ' catalog-download-status__headline--warn'
                  : status === 'READY'
                    ? ' catalog-download-status__headline--ok'
                    : ''
            }`}
            role="status"
          >
            {headline}
          </p>
        ) : null}
        <StatusBadge status={job.status} />
        {active ? (
          <>
            <div>
              {job.progress_percent != null
                ? `진행률 ${job.progress_percent}%`
                : DOWNLOAD_STATUS_LABELS.progressMissing}
            </div>
            {bytesLine ? <div>받은 용량: {bytesLine}</div> : null}
            <p className="panel-hint">{CATALOG_FIELD_HELP.downloadPolling}</p>
          </>
        ) : null}
        {!active && bytesLine ? <div>{bytesLine}</div> : null}
        {status === 'READY' ? (
          <div>
            {job.resolved_revision ? `버전 ${job.resolved_revision}` : null}
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
          <div className="catalog-download-next catalog-download-next--ready">
            <p className="catalog-download-ready-note">
              {DOWNLOAD_STATUS_LABELS.readyNotRunning}
            </p>
            <p className="panel-hint">{CATALOG_FIELD_HELP.downloadReady}</p>
            {cacheReadyForDeploy && job.node_model_cache_id ? (
              <Link
                className="btn btn--primary"
                to={`/models/cache/${encodeURIComponent(job.node_model_cache_id)}/deploy`}
              >
                배포 설정
              </Link>
            ) : (
              <>
                <p className="secondary-text">
                  캐시 ID가 없거나 캐시가 아직 READY로 확인되지 않았습니다.
                  「다운로드한 모델 관리」에서 준비 상태를 확인하세요.
                </p>
                <Link
                  className="btn btn--ghost"
                  to={`/models/cache?node_id=${encodeURIComponent(nodeId)}`}
                >
                  캐시 목록 확인
                </Link>
              </>
            )}
          </div>
        ) : null}
        {job.error_message && isFailed ? (
          <div>
            <span className="secondary-text">다운로드 실패 원인: </span>
            {job.error_message}
          </div>
        ) : null}
        {tracked.lastSuccessAt ? (
          <div>
            마지막 정상 조회:{' '}
            {formatApiDateTime(tracked.lastSuccessAt.toISOString())}
          </div>
        ) : null}
        {pollWarning ? (
          <p className="catalog-fit-error" role="status">
            <span className="secondary-text">
              {DOWNLOAD_STATUS_LABELS.pollFailed}
              (다운로드 자체와 별개):{' '}
            </span>
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
        {terminalRetryable && !starting ? (
          <button
            type="button"
            className="btn"
            disabled={!nodeId || starting || restoring || catalogListPending}
            onClick={() => void onDownload(item)}
          >
            다시 다운로드
          </button>
        ) : null}
      </div>
    )
  }

  const selectedNode = nodes.find((n) => n.id === nodeId) ?? null
  const selectedNodeName = selectedNode?.name ?? null
  const boundNodeId = itemsBoundKey?.split('|')[0] ?? ''
  const serverChangePending = Boolean(
    catalogListPending &&
      items !== null &&
      nodeId &&
      boundNodeId !== nodeId,
  )
  const keywordPending = draftQ.trim() !== q.trim()

  return (
    <AppShell
      title="모델 검색 및 다운로드"
      description="Hugging Face에서 모델을 찾고, 대상 서버에 파일을 받습니다. GPU 적합성(Fit)은 참고용이며, 다운로드 READY는 배포·API 게시 완료가 아닙니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← 모델 관리
        </Link>
        <Link className="btn btn--ghost" to="/models/cache">
          다운로드한 모델 관리
        </Link>
      </div>

      <ol className="catalog-flow-steps" aria-label="사용 안내">
        <li className="catalog-flow-steps__item">
          <span className="catalog-flow-steps__num">1</span>
          모델 검색 및 대상 서버 선택
        </li>
        <li className="catalog-flow-steps__item">
          <span className="catalog-flow-steps__num">2</span>
          GPU 적합성 참고 분석
        </li>
        <li className="catalog-flow-steps__item">
          <span className="catalog-flow-steps__num">3</span>
          다운로드 후 배포 설정
        </li>
      </ol>
      <p className="panel-hint" role="note">
        {CATALOG_FIELD_HELP.steps}
      </p>

      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="사용 순서">
            {CATALOG_FIELD_HELP.overview} {CATALOG_FIELD_HELP.diskVsVram}
          </HelpHint>
          <HelpHint showTopic label="필터 적용 방식">
            {CATALOG_FIELD_HELP.filterApply}
          </HelpHint>
          <HelpHint showTopic label="모델 유형">
            {CATALOG_FIELD_HELP.modelType} {MODEL_TYPE_HELP.LLM}{' '}
            {MODEL_TYPE_HELP.VLM} {MODEL_TYPE_HELP.EMBEDDING}
          </HelpHint>
          <HelpHint showTopic label="GPU 적합성">
            {CATALOG_FIELD_HELP.fit}
          </HelpHint>
          <HelpHint showTopic label="Fit 판정 기준">
            {FIT_RESULT_HELP.FIT} {FIT_RESULT_HELP.TIGHT}{' '}
            {FIT_RESULT_HELP.INSUFFICIENT} {FIT_RESULT_HELP.UNKNOWN}
          </HelpHint>
          <HelpHint showTopic label="설치 가능만">
            {CATALOG_FIELD_HELP.fitOnly}
          </HelpHint>
          <HelpHint showTopic label="다운로드 상태">
            {CATALOG_FIELD_HELP.downloadPolling}{' '}
            {CATALOG_FIELD_HELP.downloadReady}
          </HelpHint>
        </div>
      </details>

      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="catalog-q">
          <span>검색어</span>
          <input
            id="catalog-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="모델 저장소 / 키워드"
          />
        </label>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="catalog-type">
              모델 유형 <span className="term-label__api">(Type)</span>
            </label>
            <HelpHint label="모델 유형">{CATALOG_FIELD_HELP.modelType}</HelpHint>
          </div>
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
            <option value="ALL">전체</option>
            <option value="LLM">LLM — 언어 모델</option>
            <option value="VLM">VLM — 비전·언어</option>
            <option value="EMBEDDING">EMBEDDING — 임베딩</option>
          </select>
          <span className="toolbar__field-note">변경 즉시 적용</span>
        </div>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="catalog-node">
              대상 서버 <span className="term-label__api">(Node)</span>
            </label>
            <HelpHint label="대상 서버">{CATALOG_FIELD_HELP.node}</HelpHint>
          </div>
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
          <span className="toolbar__field-note">변경 즉시 적용</span>
        </div>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="catalog-fit-only">설치 가능만</label>
            <HelpHint label="설치 가능만">{CATALOG_FIELD_HELP.fitOnly}</HelpHint>
          </div>
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
          <span className="toolbar__field-note">변경 즉시 적용 · 참고용</span>
        </div>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
        </div>
      </form>

      <div className="catalog-query-status" role="status">
        {serverChangePending ? (
          <p className="catalog-list-pending">서버 변경 적용 중…</p>
        ) : catalogListPending && items !== null ? (
          <p className="catalog-list-pending">
            {refreshing || !listMatchesContext
              ? '대상 서버/필터 변경에 맞게 카탈로그를 다시 불러오는 중…'
              : '카탈로그를 불러오는 중…'}
          </p>
        ) : null}
        {listMatchesContext && nodeId ? (
          <p className="catalog-query-status__current">
            현재 조회 서버:{' '}
            <strong>{selectedNodeName || nodeId}</strong>
            {selectedNode?.status ? ` (${selectedNode.status})` : null}
          </p>
        ) : null}
        {!nodeId ? (
          <p className="panel-hint">
            대상 서버를 선택하면 GPU 적합성 분석과 다운로드를 진행할 수 있습니다.
          </p>
        ) : null}
        {keywordPending ? (
          <p className="catalog-query-status__pending-q" role="note">
            검색 버튼을 누르면 입력한 검색어가 적용됩니다
          </p>
        ) : null}
        <p className="secondary-text catalog-query-status__filter-hint">
          {CATALOG_FIELD_HELP.filterApply}
        </p>
      </div>

      {error ? (
        <SectionError title="카탈로그 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Hugging Face 카탈로그를 불러오는 중…" />
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
                  <th scope="col">
                    <TermLabel
                      text="모델 저장소"
                      apiTerm="Repository"
                      help={CATALOG_FIELD_HELP.repository}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="유형"
                      apiTerm="Type"
                      help={CATALOG_FIELD_HELP.modelType}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="다운로드 크기 / 예상 GPU 메모리"
                      apiTerm="Size / VRAM"
                      help={CATALOG_FIELD_HELP.sizeVram}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="GPU 적합성"
                      apiTerm="Fit"
                      help={CATALOG_FIELD_HELP.fit}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="판정 근거"
                      apiTerm="Reason"
                      help={CATALOG_FIELD_HELP.reason}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="작업"
                      apiTerm="Next"
                      help={CATALOG_FIELD_HELP.next}
                    />
                  </th>
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
                    ? '카탈로그 목록을 갱신하는 동안 GPU 적합성 분석·다운로드를 실행할 수 없습니다.'
                    : downloadReady
                      ? '모델 파일 준비 완료입니다. 아래 「배포 설정」으로 이어가세요. 중복 다운로드는 시작하지 않습니다.'
                      : typeMissing
                        ? '모델 유형이 미확정입니다. LLM/VLM/EMBEDDING을 선택한 뒤 다운로드하세요.'
                        : downloadRestoring
                          ? DOWNLOAD_STATUS_LABELS.restoring
                          : downloadDraftLocked && downloadPollWarning
                            ? `${DOWNLOAD_STATUS_LABELS.pollFailed}. 「상태 다시 조회」를 사용하세요.(다운로드 실패와 별개)`
                            : downloadDraftLocked
                              ? `${DOWNLOAD_STATUS_LABELS.restoring}. 중복 요청을 막기 위해 버튼이 잠겨 있습니다.`
                              : downloadActive
                                ? DOWNLOAD_STATUS_LABELS.inProgress
                                : downloadStarting
                                  ? DOWNLOAD_STATUS_LABELS.requesting
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
                      ?.name || fitDetail?.nodeId || nodeId || '정보 없음'

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
                            <span>
                              {item.model_type}
                              {item.model_type === 'LLM'
                                ? ' · 언어'
                                : item.model_type === 'VLM'
                                  ? ' · 비전·언어'
                                  : item.model_type === 'EMBEDDING'
                                    ? ' · 임베딩'
                                    : ''}
                            </span>
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
                              <option value="">유형 선택…</option>
                              <option value="LLM">LLM — 언어 모델</option>
                              <option value="VLM">VLM — 비전·언어</option>
                              <option value="EMBEDDING">
                                EMBEDDING — 임베딩
                              </option>
                            </select>
                          )}
                        </td>
                        <td>
                          <div>
                            {formatBytes(item.estimated_download_size_bytes)}
                          </div>
                          <div className="secondary-text">
                            {item.estimated_required_vram_mb != null
                              ? `예상 GPU 메모리 ~${item.estimated_required_vram_mb} MiB`
                              : '예상 GPU 메모리 정보 없음'}
                          </div>
                        </td>
                        <td>
                          <div className="catalog-fit-summary">
                            <span className="secondary-text">목록 요약</span>
                            {catalogFit ? (
                              <StatusBadge
                                status={catalogFit}
                                label={fitResultLabel(catalogFit)}
                                title={
                                  fitResultHelp(catalogFit) ?? catalogFit
                                }
                              />
                            ) : (
                              <span className="secondary-text">
                                {listMatchesContext ? '정보 없음' : '갱신 중'}
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
                                <StatusBadge
                                  status={analysis.result}
                                  label={fitResultLabel(analysis.result)}
                                  title={
                                    fitResultHelp(analysis.result) ??
                                    analysis.result
                                  }
                                />
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
                                    : 'GPU 적합성 분석'}
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
                                ? '요청 중…'
                                : downloadRestoring
                                  ? '복원 중…'
                                  : downloadDraftLocked && downloadPollWarning
                                    ? '조회 실패'
                                    : downloadDraftLocked
                                      ? '복원 중…'
                                      : downloadActive
                                        ? '진행 중…'
                                        : downloadReady
                                          ? '파일 준비 완료'
                                          : '다운로드'}
                            </button>
                          </div>
                          {analyzing ? (
                            <p
                              className="secondary-text catalog-fit-loading"
                              role="status"
                            >
                              선택한 서버의 GPU 자원을 확인하는 중…
                            </p>
                          ) : null}
                          {analysis && !analyzing ? (
                            <p
                              className="catalog-fit-success"
                              role="status"
                              data-testid={`fit-success-${repoId}`}
                            >
                              GPU 적합성 분석 완료 —{' '}
                              {fitResultLabel(analysis.result) ?? analysis.result}
                              <span className="secondary-text">
                                {' '}
                                ({analysis.result}) · 참고용
                              </span>
                            </p>
                          ) : null}
                          {fitError ? (
                            <p
                              className="catalog-fit-error"
                              role="alert"
                              data-testid={`fit-error-${repoId}`}
                            >
                              {fitError} — 「재시도」로 다시 분석할 수 있습니다.
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
                                  GPU 적합성 상세 분석 (참고용)
                                </h3>
                                <p className="panel-hint" role="note">
                                  현재 선택된 대상 서버(
                                  <strong>{nodeLabel}</strong>)의 GPU를 각각
                                  비교한 참고 결과입니다. 이 단계에서는 GPU를
                                  직접 선택하지 않습니다. 실제 GPU 선택·최종
                                  Fresh Fit은 배포 설정(Cache Deploy Wizard)에서
                                  수행합니다. GPU 메모리(VRAM)를 여러 장
                                  합산하지 않습니다.
                                </p>
                                <details className="catalog-fit-detail__help">
                                  <summary>판정 코드·근거 도움말</summary>
                                  <p className="panel-hint">
                                    {fitResultHelp(analysis.result)}
                                  </p>
                                </details>
                              </div>
                              <dl className="config-kv-grid catalog-fit-summary-grid">
                                <div>
                                  <dt>종합 판정</dt>
                                  <dd>
                                    <StatusBadge
                                      status={analysis.result}
                                      label={fitResultLabel(analysis.result)}
                                      title={
                                        fitResultHelp(analysis.result) ??
                                        analysis.result
                                      }
                                    />
                                    <div className="secondary-text">
                                      TP=
                                      {analysis.tensor_parallel != null
                                        ? analysis.tensor_parallel
                                        : '정보 없음'}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>분석 대상 서버</dt>
                                  <dd>
                                    {displayOrUnknown(nodeLabel)}
                                    <div className="secondary-text mono">
                                      {fitDetail.nodeId}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>모델 / 모델 버전</dt>
                                  <dd className="mono">
                                    {analysis.repository_id}
                                    <div className="secondary-text">
                                      모델 버전{' '}
                                      {displayOrUnknown(
                                        analysis.revision ||
                                          fitDetail.revision,
                                      )}
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
                                  <dt>예상 필요 GPU 메모리</dt>
                                  <dd>
                                    {formatMb(
                                      analysis.estimated_required_vram_mb,
                                    )}
                                    <div className="secondary-text">
                                      예상값이며 실제 여유 VRAM·안전 여유와 다를 수
                                      있습니다.
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>디스크 적합성</dt>
                                  <dd>
                                    <StatusBadge
                                      status={diskFitLabel(analysis.disk_ok)}
                                      label={
                                        fitResultLabel(
                                          diskFitLabel(analysis.disk_ok),
                                        ) ?? undefined
                                      }
                                    />
                                    <div className="secondary-text">
                                      여유 디스크 {formatMb(analysis.disk_free_mb)}
                                      {analysis.estimated_download_size_bytes !=
                                      null
                                        ? ` · 다운로드 크기 추정 ${formatBytes(analysis.estimated_download_size_bytes)}`
                                        : ''}
                                    </div>
                                    <div className="secondary-text">
                                      {CATALOG_FIELD_HELP.diskVsVram}
                                    </div>
                                  </dd>
                                </div>
                              </dl>

                              <h4 className="section-subheading">GPU별 판정</h4>
                              <p className="panel-hint">
                                서버 GPU를 각각 독립 카드로 표시합니다. 여유 GPU
                                메모리(VRAM)를 합산하지 않으며, 여기서 GPU를
                                고르지 않습니다.
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
                                              GPU{' '}
                                              {gpu.gpu_index != null
                                                ? gpu.gpu_index
                                                : '정보 없음'}
                                            </strong>
                                            <div className="secondary-text">
                                              {displayOrUnknown(gpu.name)}
                                            </div>
                                          </div>
                                          <StatusBadge
                                            status={gpu.result}
                                            label={fitResultLabel(gpu.result)}
                                            title={
                                              fitResultHelp(gpu.result) ??
                                              gpu.result
                                            }
                                          />
                                        </header>
                                        {suggested ? (
                                          <p className="catalog-fit-gpu-card__badge">
                                            추천 GPU (참고 · 선택 아님)
                                          </p>
                                        ) : null}
                                        <dl className="catalog-fit-gpu-card__metrics">
                                          <div>
                                            <dt>현재 여유 GPU 메모리</dt>
                                            <dd>
                                              {formatMb(gpu.vram_free_mb)}
                                              <span className="secondary-text">
                                                {' '}
                                                / 전체{' '}
                                                {formatMb(gpu.vram_total_mb)}
                                              </span>
                                            </dd>
                                          </div>
                                          <div>
                                            <dt>예상 필요 GPU 메모리</dt>
                                            <dd>
                                              {formatMb(
                                                gpu.estimated_required_vram_mb,
                                              )}
                                            </dd>
                                          </div>
                                          <div>
                                            <dt>안전 여유</dt>
                                            <dd>
                                              {formatMb(gpu.safety_margin_mb)}
                                            </dd>
                                          </div>
                                          <div>
                                            <dt>판정</dt>
                                            <dd>
                                              {fitResultLabel(gpu.result) ??
                                                displayOrUnknown(gpu.result)}
                                              <span className="secondary-text">
                                                {' '}
                                                (
                                                {displayOrUnknown(gpu.result)})
                                              </span>
                                            </dd>
                                          </div>
                                        </dl>
                                        <div className="catalog-fit-gpu-card__reasons">
                                          <span className="secondary-text">
                                            판정 근거
                                          </span>
                                          {gpu.reasons?.length ? (
                                            <ul className="catalog-fit-list">
                                              {gpu.reasons.map((r) => (
                                                <li key={r}>{r}</li>
                                              ))}
                                            </ul>
                                          ) : (
                                            <p className="secondary-text">
                                              정보 없음
                                            </p>
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
                                    대상 서버 전체 GPU 비교의 참고 제안입니다. 이
                                    화면은 GPU를 선택하지 않으며, 배포
                                    설정(Cache Deploy Wizard)에서 실제 GPU를
                                    고르고 최종 Fit을 수행합니다.
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
                                    종합 판정 근거 (Reasons)
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
                                  <h4 className="section-subheading">
                                    경고 (Warnings)
                                  </h4>
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
                                    전제 (Assumptions)
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
