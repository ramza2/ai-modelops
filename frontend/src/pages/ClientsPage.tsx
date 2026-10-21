import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listClients } from '../api/clients'
import type { ClientApp } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'
import { CLIENT_FIELD_HELP, userFacingLoadError } from '../utils/uiLabels'

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
              : '목록을 불러오지 못했습니다.'
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
  const listError = error
    ? userFacingLoadError('AI 사용 시스템 목록', error, {
        keptPrevious: items !== null,
      })
    : null

  return (
    <AppShell
      title="AI 사용 시스템"
      description="여기의 ‘사용 시스템’은 직원 계정이 아니라, AI 모델을 호출하는 업무 서비스입니다. 조회 전용이며 추가·수정·정책 변경 UI는 제공하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {CLIENT_FIELD_HELP.readOnly} 비활성(INACTIVE)은 Gateway 추론을 직접
        막는 스위치가 아닙니다. 자세한 의미는 「등록 상태」 도움말을 보세요.
      </p>

      <form className="toolbar toolbar--wrap" onSubmit={onSearch}>
        <label className="toolbar__field" htmlFor="client-q">
          <span>검색</span>
          <input
            id="client-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="서비스 키 / 표시 이름"
          />
        </label>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="client-active">
              등록 상태{' '}
              <span className="term-label__api">(is_active)</span>
            </label>
            <HelpHint label="등록 상태">{CLIENT_FIELD_HELP.isActive}</HelpHint>
          </div>
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
            <option value="ALL">전체</option>
            <option value="ACTIVE">활성 (ACTIVE)</option>
            <option value="INACTIVE">비활성 (INACTIVE)</option>
          </select>
        </div>
        <div className="toolbar__actions">
          <button type="submit" className="btn">
            검색
          </button>
        </div>
      </form>

      {listError ? (
        <SectionError
          title={listError.title}
          message={listError.message}
          action={listError.action}
        />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="AI 사용 시스템 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="표시 이름" apiTerm="display_name" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="서비스 키"
                      apiTerm="client_key"
                      help={CLIENT_FIELD_HELP.clientKey}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="등록 상태"
                      apiTerm="is_active"
                      help={CLIENT_FIELD_HELP.isActive}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="수정 시각"
                      apiTerm="updated_at"
                      help={CLIENT_FIELD_HELP.updatedAt}
                    />
                  </th>
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
          등록된 AI 사용 시스템이 없습니다.
        </p>
      ) : null}
      {emptyFiltered ? (
        <p className="empty-state" role="status">
          검색·필터 조건에 해당하는 사용 시스템이 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
