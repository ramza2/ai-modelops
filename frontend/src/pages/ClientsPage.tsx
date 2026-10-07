import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listClients } from '../api/clients'
import type { ClientApp } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20

/** Strict active query: only lowercase true/false canonical. */
export function parseClientActiveQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function activeLabel(value: boolean | null): string {
  if (value === true) return 'ACTIVE'
  if (value === false) return 'INACTIVE'
  return 'ALL'
}

export function ClientsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const q = searchParams.get('q') ?? ''
  const isActive = parseClientActiveQuery(searchParams.get('is_active'))
  const page = parsePositivePage(searchParams.get('page'))

  const [draftQ, setDraftQ] = useState(q)
  const [items, setItems] = useState<ClientApp[] | null>(null)
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
    (next: { q: string; isActive: boolean | null; page: number }) => {
      const params = new URLSearchParams()
      const trimmed = next.q.trim()
      if (trimmed) params.set('q', trimmed)
      if (next.isActive === true) params.set('is_active', 'true')
      else if (next.isActive === false) params.set('is_active', 'false')
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawActive = searchParams.get('is_active')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()
    const rawQ = searchParams.get('q')
    if (rawQ) fixed.set('q', rawQ)

    const parsedActive = parseClientActiveQuery(rawActive)
    if (rawActive !== null && rawActive !== '') {
      if (parsedActive === null) {
        needsFix = true
      } else {
        const canonical = parsedActive ? 'true' : 'false'
        if (rawActive !== canonical) needsFix = true
        fixed.set('is_active', canonical)
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
      if (mode === 'initial' && !hasLoadedRef.current) setLoading(true)
      else setRefreshing(true)
      setError(null)
      try {
        const result = await listClients({
          q,
          isActive,
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
          syncUrl({ q, isActive, page: 1 })
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
              : 'Client 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) setItems(null)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [q, isActive, page, syncUrl],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onSearch = (e: FormEvent) => {
    e.preventDefault()
    syncUrl({ q: draftQ, isActive, page: 1 })
  }

  const hasFilters = Boolean(q.trim()) || isActive !== null
  const emptyFiltered = items !== null && items.length === 0 && hasFilters
  const emptyDatabase = items !== null && items.length === 0 && !hasFilters

  return (
    <AppShell
      title="Clients"
      description="ClientApp registry와 Runtime Policy 조회 전용입니다. Client 생성/수정/Policy PUT는 현재 Backend가 Idempotency-Key를 소비하지 않아 C7 UI에서 제공하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <form className="toolbar toolbar--wrap" onSubmit={onSearch}>
        <label className="toolbar__field" htmlFor="client-q">
          <span>검색</span>
          <input
            id="client-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="client_key / display_name"
          />
        </label>
        <label className="toolbar__field" htmlFor="client-active">
          <span>Active</span>
          <select
            id="client-active"
            value={activeLabel(isActive)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'ACTIVE' ? true : v === 'INACTIVE' ? false : null
              syncUrl({ q, isActive: next, page: 1 })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="ACTIVE">ACTIVE</option>
            <option value="INACTIVE">INACTIVE</option>
          </select>
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn">
            검색
          </button>
        </div>
      </form>

      {error ? (
        <SectionError
          title={items ? 'Client 목록 새로고침 실패' : 'Client 목록 오류'}
          message={
            items
              ? `기존 Client 목록을 표시하고 있습니다. 새로고침 실패: ${error}`
              : error
          }
        />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Client 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Client</th>
                  <th scope="col">Key</th>
                  <th scope="col">Active</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {items.map((client) => (
                  <tr key={client.id}>
                    <td>
                      <Link
                        className="table-link"
                        to={`/clients/${client.id}`}
                      >
                        {client.display_name}
                      </Link>
                    </td>
                    <td className="mono">{client.client_key}</td>
                    <td>
                      <ActiveBadge active={client.is_active} />
                    </td>
                    <td>{formatApiDateTime(client.updated_at)}</td>
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
              syncUrl({ q, isActive, page: nextPage })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 Client가 없습니다.
        </p>
      ) : null}
      {emptyFiltered ? (
        <p className="empty-state" role="status">
          필터 조건에 해당하는 Client가 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
