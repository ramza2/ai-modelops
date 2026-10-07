import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listEndpoints } from '../api/endpoints'
import type { Endpoint } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20
const KNOWN_API_TYPES = new Set(['CHAT', 'EMBEDDING'])
const KNOWN_TRAFFIC = new Set(['SERVING', 'DRAINING', 'MAINTENANCE'])

function parseApiType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_API_TYPES.has(upper)) return null
  return upper
}

function parseTrafficState(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TRAFFIC.has(upper)) return null
  return upper
}

/** Strict enabled query: only lowercase true/false canonical. */
export function parseEnabledQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function enabledLabel(value: boolean | null): string {
  if (value === true) return 'ENABLED'
  if (value === false) return 'DISABLED'
  return 'ALL'
}

export function EndpointsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const apiType = parseApiType(searchParams.get('api_type'))
  const isEnabled = parseEnabledQuery(searchParams.get('is_enabled'))
  const trafficState = parseTrafficState(searchParams.get('traffic_state'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositivePage(searchParams.get('page'))

  const [draftQ, setDraftQ] = useState(q)
  const [items, setItems] = useState<Endpoint[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  useEffect(() => {
    setDraftQ(q)
  }, [q])

  const syncUrl = useCallback(
    (next: {
      q: string
      apiType: string | null
      isEnabled: boolean | null
      trafficState: string | null
      page: number
    }) => {
      const params = new URLSearchParams()
      const trimmedQ = next.q.trim()
      if (trimmedQ) params.set('q', trimmedQ)
      if (next.apiType) params.set('api_type', next.apiType)
      if (next.isEnabled === true) params.set('is_enabled', 'true')
      if (next.isEnabled === false) params.set('is_enabled', 'false')
      if (next.trafficState) params.set('traffic_state', next.trafficState)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawType = searchParams.get('api_type')
    const rawEnabled = searchParams.get('is_enabled')
    const rawTraffic = searchParams.get('traffic_state')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    if (rawType) {
      const parsed = parseApiType(rawType)
      if (rawType.toUpperCase() === 'ALL' || parsed === null) {
        needsFix = true
      } else {
        fixed.set('api_type', parsed)
        if (rawType !== parsed) needsFix = true
      }
    }

    if (rawEnabled !== null && rawEnabled !== '') {
      const parsed = parseEnabledQuery(rawEnabled)
      if (parsed === null) {
        needsFix = true
      } else {
        const canonical = parsed ? 'true' : 'false'
        if (rawEnabled !== canonical) needsFix = true
        fixed.set('is_enabled', canonical)
      }
    }

    if (rawTraffic) {
      const parsed = parseTrafficState(rawTraffic)
      if (rawTraffic.toUpperCase() === 'ALL' || parsed === null) {
        needsFix = true
      } else {
        fixed.set('traffic_state', parsed)
        if (rawTraffic !== parsed) needsFix = true
      }
    }

    if (searchParams.get('q')) fixed.set('q', searchParams.get('q')!)

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
        const result = await listEndpoints({
          q: q.trim() || null,
          apiType,
          isEnabled,
          trafficState,
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
          syncUrl({ q, apiType, isEnabled, trafficState, page: 1 })
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
              : 'Endpoint 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) setItems(null)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [q, apiType, isEnabled, trafficState, page, syncUrl],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onSubmitSearch = (event: FormEvent) => {
    event.preventDefault()
    syncUrl({ q: draftQ, apiType, isEnabled, trafficState, page: 1 })
  }

  const onClearSearch = () => {
    setDraftQ('')
    syncUrl({ q: '', apiType, isEnabled, trafficState, page: 1 })
  }

  const hasFilters =
    apiType !== null ||
    isEnabled !== null ||
    trafficState !== null ||
    q.trim() !== ''
  const emptyFiltered = items !== null && items.length === 0 && hasFilters
  const emptyDatabase = items !== null && items.length === 0 && !hasFilters

  return (
    <AppShell
      title="Endpoints"
      description="Endpoint Alias와 ACTIVE Route를 조회합니다. HOT/COLD Switch는 상세 페이지에서 Preflight preview 후 enqueue합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="endpoint-search-q">
          <span>검색</span>
          <input
            id="endpoint-search-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="alias / display name"
          />
        </label>
        <label className="toolbar__field" htmlFor="endpoint-api-type">
          <span>API Type</span>
          <select
            id="endpoint-api-type"
            value={apiType ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                q,
                apiType: v === 'ALL' ? null : v,
                isEnabled,
                trafficState,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="CHAT">CHAT</option>
            <option value="EMBEDDING">EMBEDDING</option>
          </select>
        </label>
        <label className="toolbar__field" htmlFor="endpoint-enabled">
          <span>Enabled</span>
          <select
            id="endpoint-enabled"
            value={enabledLabel(isEnabled)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'ENABLED' ? true : v === 'DISABLED' ? false : null
              syncUrl({
                q,
                apiType,
                isEnabled: next,
                trafficState,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="ENABLED">ENABLED</option>
            <option value="DISABLED">DISABLED</option>
          </select>
        </label>
        <label className="toolbar__field" htmlFor="endpoint-traffic">
          <span>Traffic</span>
          <select
            id="endpoint-traffic"
            value={trafficState ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                q,
                apiType,
                isEnabled,
                trafficState: v === 'ALL' ? null : v,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            {[...KNOWN_TRAFFIC].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
          <button type="button" className="btn" onClick={onClearSearch}>
            지우기
          </button>
        </div>
      </form>

      {error ? (
        <SectionError title="Endpoint 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Endpoint 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Endpoint</th>
                  <th scope="col">API Type</th>
                  <th scope="col">Enabled</th>
                  <th scope="col">Traffic</th>
                  <th scope="col">ACTIVE Deployment</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {items.map((ep) => {
                  const active = ep.active_route
                  const dep = active?.deployment
                  return (
                    <tr key={ep.id}>
                      <td>
                        <Link
                          className="table-link"
                          to={`/endpoints/${ep.id}`}
                        >
                          {ep.display_name || ep.alias}
                        </Link>
                        <div className="secondary-text mono">{ep.alias}</div>
                      </td>
                      <td>{ep.api_type || '—'}</td>
                      <td>
                        <ActiveBadge active={ep.is_enabled} />
                      </td>
                      <td>
                        <StatusBadge status={ep.traffic_state} />
                      </td>
                      <td>
                        {active ? (
                          <Link
                            className="table-link"
                            to={`/deployments/${active.deployment_id}`}
                          >
                            {dep?.name ?? shortId(active.deployment_id)}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td>{formatApiDateTime(ep.updated_at)}</td>
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
            onPageChange={(nextPage) =>
              syncUrl({
                q,
                apiType,
                isEnabled,
                trafficState,
                page: nextPage,
              })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 Endpoint가 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          검색/필터 조건에 해당하는 Endpoint가 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
