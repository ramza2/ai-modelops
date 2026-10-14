import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getHfDownload,
  isActiveDownloadStatus,
  listModelCaches,
  purgeModelCache,
} from '../api/downloads'
import { listNodes } from '../api/nodes'
import type { HfDownloadJob, ModelCacheEntry, NodeSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime } from '../utils/date'
import { abbreviateMiddle } from '../utils/format'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20
/** P1-8A: Cache page live download sync interval (Catalog remains 2s). */
export const CACHE_DOWNLOAD_POLL_MS = 4000

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

type JobTrack = {
  job: HfDownloadJob
  cacheId: string
  lastSuccessAt: Date
}

function pollSignature(items: ModelCacheEntry[] | null): string {
  if (!items?.length) return ''
  return items
    .filter(
      (row) =>
        row.status === 'PREPARING' &&
        typeof row.active_download_job_id === 'string' &&
        row.active_download_job_id.length > 0,
    )
    .map((row) => `${row.id}:${row.active_download_job_id}`)
    .sort()
    .join('|')
}

function pruneJobTracks(
  items: ModelCacheEntry[],
  prev: Record<string, JobTrack>,
): Record<string, JobTrack> {
  const next: Record<string, JobTrack> = {}
  for (const row of items) {
    const tracked = prev[row.id]
    if (
      tracked &&
      row.status === 'PREPARING' &&
      row.active_download_job_id === tracked.job.job_id
    ) {
      next[row.id] = tracked
    }
  }
  return next
}

export function ModelCachePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const nodeId = searchParams.get('node_id') ?? ''
  const page = parsePositivePage(searchParams.get('page'))

  const [nodes, setNodes] = useState<NodeSummary[]>([])
  const [items, setItems] = useState<ModelCacheEntry[] | null>(null)
  const [itemsBoundKey, setItemsBoundKey] = useState('')
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pollWarning, setPollWarning] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [purgeTarget, setPurgeTarget] = useState<ModelCacheEntry | null>(null)
  const [purging, setPurging] = useState(false)
  const [purgeError, setPurgeError] = useState<string | null>(null)
  const [jobByCacheId, setJobByCacheId] = useState<Record<string, JobTrack>>({})
  /** Bumped after Purge ends so poll restarts even when signature is unchanged. */
  const [pollResumeEpoch, setPollResumeEpoch] = useState(0)

  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)
  const pollGenRef = useRef(0)
  /** Shared monotonic seq so slower list responses cannot overwrite newer ones. */
  const listSeqRef = useRef(0)
  const itemsRef = useRef<ModelCacheEntry[] | null>(null)
  const purgingRef = useRef(false)
  const nodeIdRef = useRef(nodeId)
  const pageRef = useRef(page)
  const stopPollRef = useRef<() => void>(() => {})
  const listContextKey = `${nodeId}|${page}`

  itemsRef.current = items
  purgingRef.current = purging
  nodeIdRef.current = nodeId
  pageRef.current = page

  const syncUrl = useCallback(
    (next: { nodeId: string; page: number }) => {
      const params = new URLSearchParams()
      if (next.nodeId) params.set('node_id', next.nodeId)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  const applyCacheList = useCallback(
    (
      cachePage: { items: ModelCacheEntry[]; total: number },
      bound: { nodeId: string; page: number },
      seq: number,
    ): boolean => {
      if (seq !== listSeqRef.current) return false
      if (
        nodeIdRef.current !== bound.nodeId ||
        pageRef.current !== bound.page
      ) {
        return false
      }
      setItems(cachePage.items)
      setItemsBoundKey(`${bound.nodeId}|${bound.page}`)
      setTotal(cachePage.total)
      setLastUpdated(new Date())
      setJobByCacheId((prev) => pruneJobTracks(cachePage.items, prev))
      return true
    },
    [],
  )

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current
      const listSeq = ++listSeqRef.current

      if (mode === 'initial' && !hasLoadedRef.current) setLoading(true)
      else setRefreshing(true)
      setError(null)

      try {
        const [nodePage, cachePage] = await Promise.all([
          listNodes({ page: 1, pageSize: 100, signal: controller.signal }),
          listModelCaches({
            nodeId: nodeId || null,
            page,
            pageSize: PAGE_SIZE,
            signal: controller.signal,
          }),
        ])
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (nodeIdRef.current !== nodeId || pageRef.current !== page) return

        const maxPage = Math.max(1, Math.ceil(cachePage.total / PAGE_SIZE) || 1)
        if (page > maxPage && cachePage.total > 0) {
          syncUrl({ nodeId, page: 1 })
          return
        }

        if (
          !applyCacheList(cachePage, { nodeId, page }, listSeq)
        ) {
          return
        }
        setNodes(nodePage.items)
        hasLoadedRef.current = true
        setPollWarning(null)
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('모델 캐시 목록을 불러오지 못했습니다.')
        }
        // Keep last good rows on refresh failure; clear only on first load.
        if (!hasLoadedRef.current && listSeq === listSeqRef.current) {
          setItems([])
          setItemsBoundKey(`${nodeId}|${page}`)
          setTotal(0)
        }
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [nodeId, page, syncUrl, applyCacheList],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const activePollSignature = useMemo(() => {
    // Do not poll rows bound to a previous Node/page while a fresh load is in flight.
    if (itemsBoundKey !== listContextKey) return ''
    return pollSignature(items)
  }, [items, itemsBoundKey, listContextKey])

  // P1-8A: single-flight poll — getHfDownload syncs Agent, then re-list caches.
  useEffect(() => {
    if (!activePollSignature) return
    if (purgingRef.current) return
    const gen = ++pollGenRef.current
    const boundNodeId = nodeId
    const boundPage = page
    const boundContext = `${boundNodeId}|${boundPage}`
    let cancelled = false
    let timer: number | null = null
    let inFlight = false
    const controllers = new Set<AbortController>()

    const trackController = (): AbortController => {
      const controller = new AbortController()
      controllers.add(controller)
      return controller
    }

    const releaseController = (controller: AbortController) => {
      controllers.delete(controller)
    }

    const stop = () => {
      cancelled = true
      pollGenRef.current += 1
      if (timer != null) {
        window.clearTimeout(timer)
        timer = null
      }
      for (const c of controllers) c.abort()
      controllers.clear()
    }

    stopPollRef.current = stop

    const scheduleNext = () => {
      if (cancelled || gen !== pollGenRef.current) return
      if (purgingRef.current) return
      if (`${nodeIdRef.current}|${pageRef.current}` !== boundContext) return
      const still = pollSignature(itemsRef.current)
      if (!still) return
      timer = window.setTimeout(() => void pollOnce(), CACHE_DOWNLOAD_POLL_MS)
    }

    const pollOnce = async () => {
      if (cancelled || gen !== pollGenRef.current) return
      if (inFlight) return
      if (purgingRef.current) return
      inFlight = true
      try {
        const targets = (itemsRef.current ?? []).filter(
          (row) =>
            row.status === 'PREPARING' &&
            typeof row.active_download_job_id === 'string' &&
            row.active_download_job_id.length > 0,
        )
        if (!targets.length) return

        let hadSuccess = false
        let warning: string | null = null

        for (const row of targets) {
          if (cancelled || gen !== pollGenRef.current) return
          if (purgingRef.current) return
          const jobId = row.active_download_job_id as string
          const controller = trackController()
          try {
            const job = await getHfDownload(jobId, controller.signal)
            if (
              cancelled ||
              gen !== pollGenRef.current ||
              controller.signal.aborted
            ) {
              return
            }
            if (
              nodeIdRef.current !== boundNodeId ||
              pageRef.current !== boundPage
            ) {
              return
            }
            if (job.job_id !== jobId) continue
            // Never invent READY from a missing/mismatched job alone.
            setJobByCacheId((prev) => ({
              ...prev,
              [row.id]: {
                job,
                cacheId: row.id,
                lastSuccessAt: new Date(),
              },
            }))
            hadSuccess = true
          } catch (err) {
            if (
              cancelled ||
              gen !== pollGenRef.current ||
              controller.signal.aborted
            ) {
              return
            }
            if (err instanceof DOMException && err.name === 'AbortError') {
              return
            }
            if (err instanceof ApiError && err.status === 404) {
              warning =
                'Active Download Job을 찾을 수 없습니다. 캐시 상태를 READY로 추정하지 않습니다.'
              setJobByCacheId((prev) => {
                if (!(row.id in prev)) return prev
                const next = { ...prev }
                delete next[row.id]
                return next
              })
              continue
            }
            warning =
              err instanceof ApiError
                ? `다운로드 상태 갱신에 실패했습니다: ${err.message}`
                : '다운로드 상태 갱신에 실패했습니다. 마지막 정상 상태를 유지합니다.'
          } finally {
            releaseController(controller)
          }
        }

        if (cancelled || gen !== pollGenRef.current || purgingRef.current) {
          return
        }
        if (
          nodeIdRef.current !== boundNodeId ||
          pageRef.current !== boundPage
        ) {
          return
        }

        // Re-list after job sync so PREPARING→READY/FAILED is authoritative.
        const listController = trackController()
        const listSeq = ++listSeqRef.current
        try {
          const cachePage = await listModelCaches({
            nodeId: boundNodeId || null,
            page: boundPage,
            pageSize: PAGE_SIZE,
            signal: listController.signal,
          })
          if (
            cancelled ||
            gen !== pollGenRef.current ||
            listController.signal.aborted
          ) {
            return
          }
          if (
            !applyCacheList(
              cachePage,
              { nodeId: boundNodeId, page: boundPage },
              listSeq,
            )
          ) {
            return
          }
          hadSuccess = true
        } catch (err) {
          if (
            cancelled ||
            gen !== pollGenRef.current ||
            listController.signal.aborted
          ) {
            return
          }
          if (!(err instanceof DOMException && err.name === 'AbortError')) {
            warning =
              err instanceof ApiError
                ? `캐시 목록 재조회에 실패했습니다: ${err.message}`
                : '캐시 목록 재조회에 실패했습니다. 마지막 정상 상태를 유지합니다.'
          }
        } finally {
          releaseController(listController)
        }

        if (warning) setPollWarning(warning)
        else if (hadSuccess) setPollWarning(null)
      } finally {
        inFlight = false
        scheduleNext()
      }
    }

    void pollOnce()
    return () => {
      stopPollRef.current = () => {}
      stop()
    }
  }, [activePollSignature, nodeId, page, pollResumeEpoch, applyCacheList])

  const retryPoll = async () => {
    setPollWarning(null)
    await load('refresh')
  }

  const onConfirmPurge = async () => {
    if (!purgeTarget) return
    setPurgeError(null)
    // Stop in-flight poll (abort + invalidate gen) before mutating cache state.
    stopPollRef.current()
    setPurging(true)
    purgingRef.current = true
    try {
      await purgeModelCache(purgeTarget.id)
      setPurgeTarget(null)
      setJobByCacheId((prev) => {
        if (!(purgeTarget.id in prev)) return prev
        const next = { ...prev }
        delete next[purgeTarget.id]
        return next
      })
      await load('refresh')
    } catch (err) {
      setPurgeError(
        err instanceof ApiError ? err.message : '캐시 삭제에 실패했습니다.',
      )
    } finally {
      setPurging(false)
      purgingRef.current = false
      // Resume poll even when activePollSignature is unchanged.
      setPollResumeEpoch((epoch) => epoch + 1)
    }
  }

  return (
    <AppShell
      title="Model Cache"
      description="Node에 다운로드된 Hugging Face 아티팩트 캐시입니다. Downloaded ≠ Deployed ≠ Published."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing || purging}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← Models / Versions
        </Link>
        <Link className="btn btn--ghost" to="/models/catalog">
          Model Catalog
        </Link>
        <span className="secondary-text">
          Purge는 Node Agent의 로컬 파일을 제거합니다. Registry Model/Version은
          유지됩니다.
        </span>
      </div>

      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <label className="toolbar__field" htmlFor="cache-node-filter">
          <span>Node</span>
          <select
            id="cache-node-filter"
            value={nodeId}
            onChange={(e) =>
              syncUrl({ nodeId: e.target.value, page: 1 })
            }
          >
            <option value="">전체</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.status})
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <SectionError title="캐시 목록 오류" message={error} /> : null}
      {pollWarning ? (
        <SectionError
          title="다운로드 상태 갱신"
          message={pollWarning}
          onRetry={() => void retryPoll()}
        />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="모델 캐시를 불러오는 중…" />
      ) : null}

      {items !== null && items.length === 0 ? (
        <p className="secondary-text">등록된 캐시가 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Revision</th>
                  <th scope="col">Node</th>
                  <th scope="col">Size</th>
                  <th scope="col">Status</th>
                  <th scope="col">Path</th>
                  <th scope="col">Updated</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const tracked = jobByCacheId[row.id]
                  const job = tracked?.job
                  const showProgress =
                    row.status === 'PREPARING' &&
                    job != null &&
                    isActiveDownloadStatus(job.status)
                  const bytesLine =
                    job &&
                    (job.bytes_downloaded != null || job.total_bytes != null)
                      ? `${job.bytes_downloaded != null ? formatBytes(job.bytes_downloaded) : '—'} / ${
                          job.total_bytes != null
                            ? formatBytes(job.total_bytes)
                            : '—'
                        }`
                      : null
                  return (
                    <tr key={row.id}>
                      <td className="mono">{row.repository_id || '—'}</td>
                      <td className="mono">{row.resolved_revision || '—'}</td>
                      <td>{row.node_name || row.node_id}</td>
                      <td>
                        {formatBytes(row.size_bytes ?? job?.size_bytes)}
                        {showProgress && bytesLine ? (
                          <div className="secondary-text">{bytesLine}</div>
                        ) : null}
                      </td>
                      <td>
                        <StatusBadge status={row.status} />
                        {showProgress ? (
                          <div
                            className="secondary-text"
                            data-testid={`cache-download-progress-${row.id}`}
                          >
                            <StatusBadge status={job.status} />{' '}
                            {job.progress_percent != null
                              ? `진행률 ${job.progress_percent}%`
                              : '진행률 없음 (서버 미제공)'}
                          </div>
                        ) : null}
                        {row.status === 'FAILED' && row.error_message ? (
                          <div className="secondary-text">{row.error_message}</div>
                        ) : null}
                        {row.status === 'PREPARING' &&
                        !row.active_download_job_id ? (
                          <div className="secondary-text">
                            Active Download Job 없음 — READY로 추정하지 않습니다.
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {row.local_path ? (
                          <TruncatedValue
                            className="mono"
                            value={row.local_path}
                            abbreviated={abbreviateMiddle(row.local_path, 28, 12)}
                          />
                        ) : (
                          <span className="secondary-text">—</span>
                        )}
                      </td>
                      <td className="secondary-text">
                        {formatApiDateTime(row.updated_at)}
                      </td>
                      <td>
                        <div className="toolbar__actions">
                          <Link
                            className="btn"
                            to={`/models/cache/${row.id}/deploy`}
                            aria-disabled={row.status !== 'READY'}
                            onClick={(e) => {
                              if (row.status !== 'READY') e.preventDefault()
                            }}
                            title={
                              row.status === 'READY'
                                ? 'Deploy READY cache'
                                : 'Deploy requires READY cache'
                            }
                            style={
                              row.status !== 'READY'
                                ? { pointerEvents: 'none', opacity: 0.5 }
                                : undefined
                            }
                          >
                            Deploy
                          </Link>
                          <button
                            type="button"
                            className="btn btn--danger"
                            disabled={purging}
                            onClick={() => {
                              setPurgeError(null)
                              setPurgeTarget(row)
                            }}
                          >
                            Purge
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(next) => syncUrl({ nodeId, page: next })}
          />
        </>
      ) : null}

      {purgeTarget ? (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="purge-cache-title"
        >
          <div className="modal-card">
            <h2 id="purge-cache-title">캐시 Purge 확인</h2>
            <p>
              <strong>{purgeTarget.repository_id}</strong> (
              {purgeTarget.resolved_revision || 'revision unknown'}) on{' '}
              {purgeTarget.node_name || purgeTarget.node_id}의 로컬 캐시를
              삭제합니다.
            </p>
            <p className="secondary-text">
              Downloaded cache ≠ Running deployment. 실행 중 Deployment가 이
              버전을 사용 중이면 Purge가 거부될 수 있습니다.
            </p>
            {purgeError ? (
              <SectionError title="Purge 실패" message={purgeError} />
            ) : null}
            <div className="toolbar__actions">
              <button
                type="button"
                className="btn"
                disabled={purging}
                onClick={() => {
                  setPurgeTarget(null)
                  setPurgeError(null)
                }}
              >
                취소
              </button>
              <button
                type="button"
                className="btn btn--danger"
                disabled={purging}
                onClick={() => void onConfirmPurge()}
              >
                {purging ? '삭제 중…' : 'Purge 확인'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
