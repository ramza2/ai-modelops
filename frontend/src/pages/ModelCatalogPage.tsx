import {
  Fragment,
  useCallback,
  useEffect,
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
import { abbreviateMiddle } from '../utils/format'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'

/** Per-repo detailed Fit analysis (button click), distinct from catalog list summary. */
type FitDetailState = {
  analysis: ResourceFitAnalysis
  /** Client wall-clock when the analyze request was started. */
  requestedAt: Date
  nodeId: string
  revision: string | null
}

const PAGE_SIZE = 20
const POLL_MS = 2000
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
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [fitByRepo, setFitByRepo] = useState<Record<string, FitDetailState>>({})
  const [fitErrorByRepo, setFitErrorByRepo] = useState<Record<string, string>>(
    {},
  )
  const [fitLoadingByRepo, setFitLoadingByRepo] = useState<
    Record<string, boolean>
  >({})
  const [expandedFitRepos, setExpandedFitRepos] = useState<
    Record<string, boolean>
  >({})
  const [downloadByRepo, setDownloadByRepo] = useState<
    Record<string, HfDownloadJob>
  >({})
  const [downloadTypeByRepo, setDownloadTypeByRepo] = useState<
    Record<string, string>
  >({})
  const [downloadStarting, setDownloadStarting] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)
  const fitAbortByRepoRef = useRef<Record<string, AbortController>>({})
  const fitGenByRepoRef = useRef<Record<string, number>>({})
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
    setFitByRepo({})
    setFitErrorByRepo({})
    setFitLoadingByRepo({})
    setExpandedFitRepos({})
  }, [catalogContextKey])

  useEffect(() => {
    return () => {
      for (const controller of Object.values(fitAbortByRepoRef.current)) {
        controller.abort()
      }
      fitAbortByRepoRef.current = {}
    }
  }, [])

  useEffect(() => {
    const next: Record<string, string | null> = {}
    for (const item of items ?? []) {
      next[item.repository_id] = item.revision ?? null
    }
    itemsRevisionByRepoRef.current = next
  }, [items])

  useEffect(() => {
    const active = Object.values(downloadByRepo).filter((j) =>
      isActiveDownloadStatus(j.status),
    )
    if (active.length === 0) return

    let cancelled = false
    const poll = async () => {
      for (const job of active) {
        try {
          const updated = await getHfDownload(job.job_id)
          if (cancelled) return
          setDownloadByRepo((prev) => ({
            ...prev,
            [job.repository_id]: updated,
          }))
        } catch {
          /* keep last known job; user can refresh */
        }
      }
    }

    const timer = window.setInterval(() => void poll(), POLL_MS)
    void poll()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [downloadByRepo])

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

  const onAnalyze = async (item: HfCatalogModel) => {
    if (!nodeId) return
    const repoId = item.repository_id
    const requestNodeId = nodeId
    const requestRevision = item.revision ?? null
    const requestContextKey = catalogContextKeyRef.current
    const requestedAt = new Date()
    const gen = (fitGenByRepoRef.current[repoId] ?? 0) + 1
    fitGenByRepoRef.current[repoId] = gen

    fitAbortByRepoRef.current[repoId]?.abort()
    const controller = new AbortController()
    fitAbortByRepoRef.current[repoId] = controller

    setFitLoadingByRepo((prev) => ({ ...prev, [repoId]: true }))
    setFitErrorByRepo((prev) => {
      if (!(repoId in prev)) return prev
      const next = { ...prev }
      delete next[repoId]
      return next
    })

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
      if (
        controller.signal.aborted ||
        fitGenByRepoRef.current[repoId] !== gen ||
        nodeIdRef.current !== requestNodeId ||
        catalogContextKeyRef.current !== requestContextKey ||
        (itemsRevisionByRepoRef.current[repoId] ?? null) !== requestRevision ||
        result.repository_id !== repoId
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
      setExpandedFitRepos((prev) => ({ ...prev, [repoId]: true }))
    } catch (err) {
      if (
        controller.signal.aborted ||
        fitGenByRepoRef.current[repoId] !== gen ||
        nodeIdRef.current !== requestNodeId ||
        catalogContextKeyRef.current !== requestContextKey
      ) {
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') return
      setFitErrorByRepo((prev) => ({
        ...prev,
        [repoId]:
          err instanceof ApiError
            ? err.message
            : '자원 적합도 분석에 실패했습니다.',
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
    const existing = downloadByRepo[item.repository_id]
    if (existing && isActiveDownloadStatus(existing.status)) return
    if (downloadStarting === item.repository_id) return
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

    setDownloadStarting(item.repository_id)
    setError(null)
    try {
      const job = await startHfDownload({
        repositoryId: item.repository_id,
        revision: item.revision,
        nodeId,
        modelType,
      })
      setDownloadByRepo((prev) => ({ ...prev, [item.repository_id]: job }))
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : '다운로드를 시작하지 못했습니다.',
      )
    } finally {
      setDownloadStarting(null)
    }
  }

  const renderDownloadState = (item: HfCatalogModel) => {
    const job = downloadByRepo[item.repository_id]
    if (!job) return null

    const status = job.status.toUpperCase()
    if (status === 'READY') {
      return (
        <div className="secondary-text" style={{ marginTop: '0.35rem' }}>
          <StatusBadge status={job.status} />
          <div>
            {job.resolved_revision ? `rev ${job.resolved_revision}` : null}
            {job.size_bytes != null ? ` · ${formatBytes(job.size_bytes)}` : null}
          </div>
          {job.local_path ? (
            <TruncatedValue
              className="mono"
              value={job.local_path}
              abbreviated={abbreviateMiddle(job.local_path, 28, 12)}
            />
          ) : null}
        </div>
      )
    }

    if (status === 'FAILED') {
      return (
        <div style={{ marginTop: '0.35rem' }}>
          <StatusBadge status={job.status} />
          {job.error_message ? (
            <div className="secondary-text">{job.error_message}</div>
          ) : null}
          <button
            type="button"
            className="btn"
            style={{ marginTop: '0.25rem' }}
            disabled={!nodeId || downloadStarting === item.repository_id}
            onClick={() => void onDownload(item)}
          >
            Retry download
          </button>
        </div>
      )
    }

    if (isActiveDownloadStatus(job.status)) {
      const pct =
        job.progress_percent != null ? `${job.progress_percent}%` : '…'
      return (
        <div className="secondary-text" style={{ marginTop: '0.35rem' }}>
          <StatusBadge status={job.status} />
          <div>Progress {pct}</div>
        </div>
      )
    }

    return (
      <div style={{ marginTop: '0.35rem' }}>
        <StatusBadge status={job.status} />
      </div>
    )
  }

  return (
    <AppShell
      title="Model Catalog"
      description="Hugging Face 모델을 검색하고 Node에 다운로드합니다. FIT는 자문용이며 Deploy는 M7-C에서 제공됩니다."
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
              // node-scoped and must reset here.
              setDownloadByRepo({})
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

      {items !== null && items.length === 0 ? (
        <p className="secondary-text">조건에 맞는 모델이 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
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
                  const fitDetail = fitByRepo[repoId]
                  const analysis = fitDetail?.analysis
                  const catalogFit = item.resource_fit?.result || null
                  const analyzing = Boolean(fitLoadingByRepo[repoId])
                  const fitError = fitErrorByRepo[repoId]
                  const expanded = Boolean(expandedFitRepos[repoId] && analysis)
                  const job = downloadByRepo[repoId]
                  const downloadActive =
                    job != null && isActiveDownloadStatus(job.status)
                  const diskBlocked = isDiskInsufficientForDownload(analysis)
                  const resolvedType = resolveDownloadModelType(
                    item,
                    downloadTypeByRepo[repoId],
                  )
                  const typeMissing = resolvedType == null
                  const downloadTitle = typeMissing
                    ? 'Select LLM/VLM/EMBEDDING before Download'
                    : downloadDisabledReason(nodeId, analysis)
                  const downloadDisabled =
                    !nodeId ||
                    diskBlocked ||
                    typeMissing ||
                    downloadActive ||
                    downloadStarting === repoId
                  const nodeLabel =
                    nodes.find((n) => n.id === (fitDetail?.nodeId || nodeId))
                      ?.name || fitDetail?.nodeId || nodeId || '—'

                  return (
                    <Fragment key={repoId}>
                      <tr
                        aria-busy={analyzing || undefined}
                        data-fit-loading={analyzing ? 'true' : undefined}
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
                              <span className="secondary-text">—</span>
                            )}
                            {analysis ? (
                              <div className="catalog-fit-summary__detail">
                                <span className="secondary-text">상세 분석</span>
                                <StatusBadge status={analysis.result} />
                              </div>
                            ) : null}
                          </div>
                        </td>
                        <td className="secondary-text">
                          {catalogFitSummaryReason(item)}
                        </td>
                        <td>
                          <div className="toolbar__actions">
                            <button
                              type="button"
                              className="btn"
                              disabled={!nodeId || analyzing}
                              aria-busy={analyzing || undefined}
                              onClick={() => void onAnalyze(item)}
                            >
                              {analyzing
                                ? '분석 중…'
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
                              {downloadStarting === repoId
                                ? 'Starting…'
                                : downloadActive
                                  ? 'Downloading…'
                                  : 'Download'}
                            </button>
                            <button
                              type="button"
                              className="btn"
                              disabled
                              title="M7-C"
                            >
                              Deploy
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
                              {fitError}
                            </p>
                          ) : null}
                          {renderDownloadState(item)}
                        </td>
                      </tr>
                      {expanded && analysis && fitDetail ? (
                        <tr className="catalog-fit-detail-row">
                          <td colSpan={6}>
                            <div
                              className="catalog-fit-detail"
                              data-testid={`fit-detail-${repoId}`}
                            >
                              <div className="catalog-fit-detail__head">
                                <h3 className="section-subheading">
                                  Fit 상세 분석 (advisory)
                                </h3>
                                <p className="panel-hint">
                                  서버 응답을 그대로 표시합니다. GPU VRAM을
                                  합산하지 않으며, 실제 Deployment를 실행하지
                                  않습니다. 목록의 resource_fit 요약과 별개입니다.
                                </p>
                              </div>
                              <dl className="config-kv-grid">
                                <div>
                                  <dt>종합 결과</dt>
                                  <dd>
                                    <StatusBadge status={analysis.result} />
                                  </dd>
                                </div>
                                <div>
                                  <dt>분석 대상 Node</dt>
                                  <dd>
                                    {nodeLabel}
                                    <div className="secondary-text mono">
                                      {fitDetail.nodeId}
                                    </div>
                                  </dd>
                                </div>
                                <div>
                                  <dt>분석 대상 모델</dt>
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
                                  <dt>클라이언트 요청 시각</dt>
                                  <dd>
                                    {formatApiDateTime(
                                      fitDetail.requestedAt.toISOString(),
                                    )}
                                    <div className="secondary-text">
                                      서버 완료 시각이 아닙니다.
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
                                GPU마다 독립적으로 표시합니다. free VRAM을
                                합산하지 않습니다.
                              </p>
                              {analysis.gpu_results.length > 0 ? (
                                <div className="table-wrap">
                                  <table className="data-table catalog-fit-gpu-table">
                                    <thead>
                                      <tr>
                                        <th scope="col">GPU</th>
                                        <th scope="col">Free / Total</th>
                                        <th scope="col">Safety margin</th>
                                        <th scope="col">예상 필요 VRAM</th>
                                        <th scope="col">판정</th>
                                        <th scope="col">Reasons</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {analysis.gpu_results.map((gpu) => (
                                        <tr key={gpu.gpu_device_id}>
                                          <td>
                                            index{' '}
                                            {gpu.gpu_index ?? '—'}
                                            <div className="secondary-text">
                                              {gpu.name || '—'}
                                            </div>
                                          </td>
                                          <td>
                                            {formatMb(gpu.vram_free_mb)} /{' '}
                                            {formatMb(gpu.vram_total_mb)}
                                          </td>
                                          <td>
                                            {formatMb(gpu.safety_margin_mb)}
                                          </td>
                                          <td>
                                            {formatMb(
                                              gpu.estimated_required_vram_mb,
                                            )}
                                          </td>
                                          <td>
                                            <StatusBadge status={gpu.result} />
                                          </td>
                                          <td className="secondary-text">
                                            {gpu.reasons?.length
                                              ? gpu.reasons.join('; ')
                                              : '—'}
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
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
                                    Suggested GPU (참고)
                                  </h4>
                                  <p className="panel-hint">
                                    Node 기준 분석의 참고 제안입니다. Catalog에서
                                    GPU를 선택하지 않으며, 실제 배정은 Cache Deploy
                                    Wizard에서 합니다.
                                  </p>
                                  <ul className="catalog-fit-list mono">
                                    {analysis.suggested_gpu_device_ids.map(
                                      (id) => (
                                        <li key={id}>{id}</li>
                                      ),
                                    )}
                                  </ul>
                                </div>
                              ) : null}

                              <div className="catalog-fit-detail__lists">
                                <div>
                                  <h4 className="section-subheading">Reasons</h4>
                                  <ul className="catalog-fit-list">
                                    {analysis.reasons.length > 0 ? (
                                      analysis.reasons.map((r) => (
                                        <li key={r}>{r}</li>
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
                                        <li key={w}>{w}</li>
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
                                        <li key={a}>{a}</li>
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
