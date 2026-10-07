import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listOperations } from '../api/operations'
import type { OperationSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20

const KNOWN_STATUSES = new Set([
  'QUEUED',
  'RUNNING',
  'ROLLING_BACK',
  'SUCCEEDED',
  'FAILED',
  'ROLLED_BACK',
  'CANCELLED',
  'MANUAL_INTERVENTION_REQUIRED',
])

const KNOWN_TYPES = new Set([
  'DEPLOY',
  'START',
  'STOP',
  'RESTART',
  'SWITCH',
  'ROLLBACK',
  'DELETE',
  'IMPORT',
])

function parseStatus(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_STATUSES.has(upper)) return null
  return upper
}

function parseOperationType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TYPES.has(upper)) return null
  return upper
}

/** Strict active query: only lowercase true/false canonical. */
export function parseActiveQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function activeLabel(value: boolean | null): string {
  if (value === true) return 'ACTIVE'
  if (value === false) return 'TERMINAL'
  return 'ALL'
}

export function OperationsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const status = parseStatus(searchParams.get('status'))
  const operationType = parseOperationType(searchParams.get('operation_type'))
  // status and active are mutually exclusive; prefer status when both present.
  const activeRaw = parseActiveQuery(searchParams.get('active'))
  const active = status !== null ? null : activeRaw
  const page = parsePositivePage(searchParams.get('page'))

  const [items, setItems] = useState<OperationSummary[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  const syncUrl = useCallback(
    (next: {
      status: string | null
      operationType: string | null
      active: boolean | null
      page: number
    }) => {
      const params = new URLSearchParams()
      // Mutual exclusion: status wins over active.
      if (next.status) {
        params.set('status', next.status)
      } else if (next.active === true) {
        params.set('active', 'true')
      } else if (next.active === false) {
        params.set('active', 'false')
      }
      if (next.operationType) params.set('operation_type', next.operationType)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawStatus = searchParams.get('status')
    const rawType = searchParams.get('operation_type')
    const rawActive = searchParams.get('active')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    const parsedStatus = parseStatus(rawStatus)
    if (rawStatus) {
      if (rawStatus.toUpperCase() === 'ALL' || parsedStatus === null) {
        needsFix = true
      } else {
        fixed.set('status', parsedStatus)
        if (rawStatus !== parsedStatus) needsFix = true
      }
    }

    const parsedType = parseOperationType(rawType)
    if (rawType) {
      if (rawType.toUpperCase() === 'ALL' || parsedType === null) {
        needsFix = true
      } else {
        fixed.set('operation_type', parsedType)
        if (rawType !== parsedType) needsFix = true
      }
    }

    const parsedActive = parseActiveQuery(rawActive)
    if (rawActive !== null && rawActive !== '') {
      if (parsedActive === null) {
        needsFix = true
      } else if (parsedStatus !== null) {
        // Both status and active → drop active (mutual exclusion).
        needsFix = true
      } else {
        const canonical = parsedActive ? 'true' : 'false'
        if (rawActive !== canonical) needsFix = true
        fixed.set('active', canonical)
      }
    }

    const parsedPage = parsePositivePage(rawPage)
    if (rawPage !== null && String(parsedPage) !== rawPage) needsFix = true
    if (parsedPage > 1) fixed.set('page', String(parsedPage))

    if (needsFix) setSearchParams(fixed, { replace: true })
  }, [searchParams, setSearchParams])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial' && !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const result = await listOperations({
          status,
          operationType,
          active,
          page,
          pageSize: PAGE_SIZE,
          signal: controller.signal,
        })
        if (controller.signal.aborted || gen !== readGenRef.current) return

        const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
        if (page > maxPage) {
          if (result.total === 0) {
            setItems([])
            setTotal(0)
            setLastUpdated(new Date())
            hasLoadedRef.current = true
          }
          syncUrl({ status, operationType, active, page: 1 })
          return
        }

        setItems(result.items)
        setTotal(result.total)
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Operation 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) setItems(null)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [status, operationType, active, page, syncUrl],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const hasFilters =
    status !== null || operationType !== null || active !== null
  const emptyFiltered = items !== null && items.length === 0 && hasFilters
  const emptyDatabase = items !== null && items.length === 0 && !hasFilters

  return (
    <AppShell
      title="Operations"
      description="Operation 목록과 진행 상태입니다. Switch Safe Cancel / Explicit Retry는 상세 페이지에서만 제공됩니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap">
        <label className="toolbar__field" htmlFor="op-status-filter">
          <span>Status</span>
          <select
            id="op-status-filter"
            value={status ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                status: v === 'ALL' ? null : v,
                operationType,
                active: null,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            {[...KNOWN_STATUSES].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="op-type-filter">
          <span>Type</span>
          <select
            id="op-type-filter"
            value={operationType ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                status,
                operationType: v === 'ALL' ? null : v,
                active,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            {[...KNOWN_TYPES].map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="op-active-filter">
          <span>Active</span>
          <select
            id="op-active-filter"
            value={activeLabel(active)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'ACTIVE' ? true : v === 'TERMINAL' ? false : null
              syncUrl({
                status: null,
                operationType,
                active: next,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="ACTIVE">ACTIVE</option>
            <option value="TERMINAL">TERMINAL</option>
          </select>
        </label>
        <p className="toolbar__hint">
          Status와 Active는 동시에 사용할 수 없습니다.
        </p>
      </div>

      {error ? (
        <SectionError title="Operation 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Operation 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Operation</th>
                  <th scope="col">Type</th>
                  <th scope="col">Status</th>
                  <th scope="col">Strategy</th>
                  <th scope="col">Endpoint</th>
                  <th scope="col">Source</th>
                  <th scope="col">Target</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {items.map((op) => (
                  <tr key={op.id}>
                    <td>
                      <Link
                        className="table-link mono"
                        to={`/operations/${op.id}`}
                      >
                        {shortId(op.id, 12)}
                      </Link>
                    </td>
                    <td>{op.operation_type || '—'}</td>
                    <td>
                      <StatusBadge status={op.status} />
                    </td>
                    <td>{op.switch_strategy || '—'}</td>
                    <td>
                      {op.endpoint_alias_id ? (
                        <Link
                          className="table-link mono"
                          to={`/endpoints/${op.endpoint_alias_id}`}
                        >
                          {shortId(op.endpoint_alias_id)}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      {op.source_deployment_id ? (
                        <Link
                          className="table-link mono"
                          to={`/deployments/${op.source_deployment_id}`}
                        >
                          {shortId(op.source_deployment_id)}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      {op.target_deployment_id ? (
                        <Link
                          className="table-link mono"
                          to={`/deployments/${op.target_deployment_id}`}
                        >
                          {shortId(op.target_deployment_id)}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>{formatApiDateTime(op.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(nextPage) =>
              syncUrl({
                status,
                operationType,
                active,
                page: nextPage,
              })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 Operation이 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          필터 조건에 해당하는 Operation이 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
