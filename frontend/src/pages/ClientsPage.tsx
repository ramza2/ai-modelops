import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listClients } from '../api/clients'
import { getInvocationSummary } from '../api/observability'
import type { ClientApp, InvocationSummaryItem } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { ClientCreateDialog } from '../components/ClientCreateDialog'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import {
  CLIENT_USAGE_HOURS,
  indexClientUsageByKey,
  resolveClientUsage,
} from '../utils/clientUsage'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'
import {
  CLIENT_FIELD_HELP,
  OBSERVABILITY_FIELD_HELP,
  PAGE_INTRO,
  userFacingLoadError,
} from '../utils/uiLabels'

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
  const navigate = useNavigate()
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
  const [usageByKey, setUsageByKey] = useState<Map<
    string,
    InvocationSummaryItem
  > | null>(null)
  const [usageFailed, setUsageFailed] = useState(false)
  const [usageError, setUsageError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
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
        const [listOutcome, usageOutcome] = await Promise.allSettled([
          listClients({
            q,
            isActive,
            page,
            pageSize: PAGE_SIZE,
            signal: controller.signal,
          }),
          getInvocationSummary({
            hours: CLIENT_USAGE_HOURS,
            groupBy: 'client',
            signal: controller.signal,
          }),
        ])
        if (controller.signal.aborted || gen !== readGenRef.current) return

        if (usageOutcome.status === 'fulfilled') {
          setUsageByKey(indexClientUsageByKey(usageOutcome.value))
          setUsageFailed(false)
          setUsageError(null)
        } else {
          const err = usageOutcome.reason
          if (!(err instanceof DOMException && err.name === 'AbortError')) {
            setUsageByKey(null)
            setUsageFailed(true)
            setUsageError(
              err instanceof ApiError
                ? err.message
                : err instanceof Error
                  ? err.message
                  : '호출 집계를 불러오지 못했습니다.',
            )
          }
        }

        if (listOutcome.status === 'rejected') {
          const err = listOutcome.reason
          if (err instanceof DOMException && err.name === 'AbortError') return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : '목록을 불러오지 못했습니다.'
          setError(message)
          if (!hasLoadedRef.current) setItems(null)
          return
        }

        const result = listOutcome.value
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
      description={PAGE_INTRO.clients}
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <details className="help-glossary">
        <summary>자세히 알아보기</summary>
        <p className="panel-hint" role="note">
          {CLIENT_FIELD_HELP.registryEditable} {CLIENT_FIELD_HELP.policyEditable}{' '}
          비활성(INACTIVE)은 Gateway 추론을 직접 막는 스위치가 아닙니다.
        </p>
        <div className="help-glossary__items">
          <HelpHint showTopic label="등록 상태">
            {CLIENT_FIELD_HELP.isActive}
          </HelpHint>
          <HelpHint showTopic label="최근 호출 현황">
            최근 {CLIENT_USAGE_HOURS}시간 호출 집계를 재사용합니다.{' '}
            {OBSERVABILITY_FIELD_HELP.lastCalledAt}{' '}
            {OBSERVABILITY_FIELD_HELP.usageStatus}
          </HelpHint>
          <HelpHint showTopic label="서비스 키">
            {CLIENT_FIELD_HELP.clientKey}
          </HelpHint>
        </div>
      </details>

      <div className="toolbar toolbar--wrap">
        <div className="toolbar__actions">
          <button
            type="button"
            className="btn"
            onClick={() => setCreateOpen(true)}
          >
            신규 시스템 등록
          </button>
        </div>
      </div>

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
      {usageFailed && usageError ? (
        <SectionError
          title="AI 호출 집계 조회 실패"
          message={`등록 시스템 목록은 유지합니다. 사용량 열만 확인할 수 없습니다. 원인: ${usageError}`}
          action="네트워크·권한을 확인한 뒤 새로고침을 다시 시도하세요. 호출 없음과 집계 실패는 다릅니다."
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
                    <TermLabel
                      text="표시 이름"
                      apiTerm="display_name"
                      showApiTerm={false}
                      help={CLIENT_FIELD_HELP.displayName}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="서비스 키"
                      apiTerm="client_key"
                      showApiTerm={false}
                      help={CLIENT_FIELD_HELP.clientKey}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="등록 상태"
                      apiTerm="is_active"
                      showApiTerm={false}
                      help={CLIENT_FIELD_HELP.isActive}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text={`최근 ${CLIENT_USAGE_HOURS}시간 요청`}
                      apiTerm="request_count"
                      showApiTerm={false}
                      help="선택한 조회 기간의 Gateway 요청 건수입니다."
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="성공"
                      apiTerm="success_count"
                      showApiTerm={false}
                      help="HTTP 2xx–3xx로 분류된 성공 건수입니다."
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="오류"
                      apiTerm="error_count"
                      showApiTerm={false}
                      help="HTTP 2xx–3xx가 아닌 오류 건수입니다."
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text={`마지막 호출(${CLIENT_USAGE_HOURS}시간 내)`}
                      apiTerm="last_called_at"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.lastCalledAt}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="최근 호출 상태"
                      apiTerm="usage_status"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.usageStatus}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="수정 시각"
                      apiTerm="updated_at"
                      showApiTerm={false}
                      help={CLIENT_FIELD_HELP.updatedAt}
                    />
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((client) => {
                  const usage = resolveClientUsage(
                    client.client_key,
                    usageByKey,
                    usageFailed,
                  )
                  return (
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
                      <td>
                        {usage.requestCount === null
                          ? '조회 실패'
                          : usage.requestCount}
                      </td>
                      <td>
                        {usage.successCount === null
                          ? '조회 실패'
                          : usage.successCount}
                      </td>
                      <td>
                        {usage.errorCount === null
                          ? '조회 실패'
                          : usage.errorCount}
                      </td>
                      <td>
                        {usage.status === 'unavailable'
                          ? '조회 실패'
                          : formatApiDateTime(usage.lastCalledAt)}
                      </td>
                      <td data-testid={`usage-status-${client.client_key}`}>
                        {usage.statusLabel}
                      </td>
                      <td>{formatApiDateTime(client.updated_at)}</td>
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

      <ClientCreateDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false)
          navigate(`/clients/${created.id}`)
        }}
      />
    </AppShell>
  )
}
