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
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime } from '../utils/date'
import { abbreviateMiddle } from '../utils/format'
import { parsePositivePage } from '../utils/query'
import { CACHE_FIELD_HELP, CATALOG_FIELD_HELP } from '../utils/uiLabels'

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
  const listMatchesContext = itemsBoundKey === listContextKey
  const cacheListPending =
    (loading && items === null) ||
    (items !== null && !listMatchesContext)

  itemsRef.current = items
  purgingRef.current = purging
  nodeIdRef.current = nodeId
  pageRef.current = page

  // Drop an open Purge dialog when Node/page context changes so operators
  // cannot confirm against a stale row still on screen.
  useEffect(() => {
    setPurgeTarget(null)
    setPurgeError(null)
  }, [listContextKey])

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
      title="다운로드한 모델 관리"
      description="대상 서버에 받아 둔 모델 파일(캐시)만 관리합니다. 캐시 READY ≠ 모델 실행 중 ≠ API 게시 완료입니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing || purging}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← 모델 관리
        </Link>
        <Link className="btn btn--ghost" to="/models/catalog">
          모델 검색 및 다운로드
        </Link>
      </div>

      <p className="panel-hint" role="note">
        {CACHE_FIELD_HELP.overview} {CACHE_FIELD_HELP.deploy}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="캐시 상태">
            {CACHE_FIELD_HELP.status}
          </HelpHint>
          <HelpHint showTopic label="다운로드 상태">
            {CACHE_FIELD_HELP.progress} {CATALOG_FIELD_HELP.downloadReady}
          </HelpHint>
          <HelpHint showTopic label="캐시 삭제 안내">
            {CACHE_FIELD_HELP.purge}
          </HelpHint>
          <HelpHint showTopic label="배포 가능 여부">
            {CACHE_FIELD_HELP.deploy}
          </HelpHint>
        </div>
      </details>

      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="cache-node-filter">
              대상 서버 <span className="term-label__api">(Node)</span>
            </label>
            <HelpHint label="대상 서버">{CATALOG_FIELD_HELP.node}</HelpHint>
          </div>
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
        </div>
      </div>

      {error ? (
        <SectionError
          title="캐시 목록 오류"
          message={error}
          onRetry={() => void load('refresh')}
        />
      ) : null}
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

      {cacheListPending && items !== null ? (
        <p className="catalog-list-pending" role="status">
          {refreshing || !listMatchesContext
            ? '대상 서버/페이지 변경에 맞게 캐시 목록을 다시 불러오는 중…'
            : '캐시 목록을 불러오는 중…'}
        </p>
      ) : null}

      {items !== null && items.length === 0 && listMatchesContext ? (
        <p className="secondary-text">등록된 캐시가 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div
            className={`table-wrap${cacheListPending ? ' catalog-table-wrap--pending' : ''}`}
            aria-busy={cacheListPending || undefined}
            data-testid="cache-table"
          >
            <table className="data-table">
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
                      text="모델 버전"
                      apiTerm="Revision"
                      help={CATALOG_FIELD_HELP.revision}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="대상 서버"
                      apiTerm="Node"
                      help={CATALOG_FIELD_HELP.node}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="크기" apiTerm="Size" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="캐시 상태"
                      apiTerm="Status"
                      help={CACHE_FIELD_HELP.status}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="로컬 경로" apiTerm="Path" />
                  </th>
                  <th scope="col">
                    <TermLabel text="수정 시각" apiTerm="Updated" />
                  </th>
                  <th scope="col">작업</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const tracked = jobByCacheId[row.id]
                  const job = tracked?.job
                  const showProgress =
                    listMatchesContext &&
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
                  const deployBlocked =
                    !listMatchesContext || row.status !== 'READY'
                  return (
                    <tr
                      key={row.id}
                      data-cache-stale={
                        listMatchesContext ? undefined : 'true'
                      }
                    >
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
                        {!listMatchesContext ? (
                          <div className="secondary-text">목록 갱신 중…</div>
                        ) : null}
                        {showProgress ? (
                          <div
                            className="secondary-text"
                            data-testid={`cache-download-progress-${row.id}`}
                          >
                            <StatusBadge status={job.status} />{' '}
                            {job.progress_percent != null
                              ? `진행률 ${job.progress_percent}%`
                              : '진행률 없음 (서버 미제공 · 임의 %를 만들지 않음)'}
                            {tracked?.lastSuccessAt ? (
                              <div>
                                마지막 상태 조회:{' '}
                                {formatApiDateTime(
                                  tracked.lastSuccessAt.toISOString(),
                                )}
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                        {listMatchesContext &&
                        row.status === 'FAILED' &&
                        row.error_message ? (
                          <div className="secondary-text">{row.error_message}</div>
                        ) : null}
                        {listMatchesContext &&
                        row.status === 'PREPARING' &&
                        !row.active_download_job_id ? (
                          <div className="secondary-text">
                            활성 다운로드 Job 없음 — READY로 추정하지 않습니다.
                          </div>
                        ) : null}
                        {listMatchesContext && row.status === 'READY' ? (
                          <div className="secondary-text">
                            캐시 준비 완료 · 배포 설정으로 이동할 수 있습니다
                          </div>
                        ) : null}
                        {listMatchesContext &&
                        row.status !== 'READY' &&
                        row.status !== 'PREPARING' &&
                        row.status !== 'FAILED' ? (
                          <div className="secondary-text">
                            아직 배포할 수 없는 캐시 상태입니다
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
                            aria-disabled={deployBlocked}
                            onClick={(e) => {
                              if (deployBlocked) e.preventDefault()
                            }}
                            title={
                              !listMatchesContext
                                ? '목록 갱신 중 — 배포 설정 불가'
                                : row.status === 'READY'
                                  ? '캐시 READY — 배포 설정으로 이동'
                                  : '배포하려면 캐시가 READY여야 합니다'
                            }
                            style={
                              deployBlocked
                                ? { pointerEvents: 'none', opacity: 0.5 }
                                : undefined
                            }
                          >
                            배포 설정
                          </Link>
                          <button
                            type="button"
                            className="btn btn--danger"
                            disabled={purging || !listMatchesContext}
                            title={
                              !listMatchesContext
                                ? '목록 갱신 중 — 로컬 캐시 삭제 불가'
                                : '로컬 캐시 파일만 삭제합니다(레지스트리·실행 중 모델과 별개)'
                            }
                            onClick={() => {
                              if (!listMatchesContext) return
                              setPurgeError(null)
                              setPurgeTarget(row)
                            }}
                          >
                            로컬 캐시 삭제
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
            <h2 id="purge-cache-title">로컬 캐시 삭제 확인</h2>
            <p>
              <strong>{purgeTarget.repository_id}</strong> (
              {purgeTarget.resolved_revision || '버전 미상'}) — 대상 서버{' '}
              {purgeTarget.node_name || purgeTarget.node_id}의{' '}
              <strong>로컬 모델 파일만</strong> 삭제합니다.
            </p>
            <p className="secondary-text">{CACHE_FIELD_HELP.purge}</p>
            <p className="secondary-text">
              캐시 파일 ≠ 실행 중 배포. 실행 중 Deployment가 이 버전을 쓰면 삭제가
              거부될 수 있습니다. 모델 레지스트리(Model/Version)는 삭제되지
              않습니다.
            </p>
            {purgeError ? (
              <SectionError title="로컬 캐시 삭제 실패" message={purgeError} />
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
                {purging ? '삭제 중…' : '삭제 확인'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
