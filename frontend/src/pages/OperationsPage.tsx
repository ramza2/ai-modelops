import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listOperations } from '../api/operations'
import type { OperationSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parsePositivePage } from '../utils/query'
import {
  OPERATION_FIELD_HELP,
  OPERATION_TYPE_LABELS,
  formatOperationTypeDisplay,
  userFacingLoadError,
} from '../utils/uiLabels'

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
              : '작업 목록을 불러오지 못했습니다.'
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
  const listError = error
    ? userFacingLoadError('작업 목록', error, {
        keptPrevious: items !== null,
      })
    : null

  return (
    <AppShell
      title="작업 이력"
      description="배포·전환 등 비동기 작업의 진행과 결과를 추적합니다. Safe Cancel / Explicit Retry는 상세 페이지에서만 제공합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {OPERATION_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="진행 상태">
            {OPERATION_FIELD_HELP.status}
          </HelpHint>
          <HelpHint showTopic label="작업 유형">
            {OPERATION_FIELD_HELP.type}
          </HelpHint>
          <HelpHint showTopic label="전환 방식">
            {OPERATION_FIELD_HELP.strategy}
          </HelpHint>
          <HelpHint showTopic label="기존/변경 대상">
            {OPERATION_FIELD_HELP.sourceTarget}
          </HelpHint>
          <HelpHint showTopic label="안전 취소">
            {OPERATION_FIELD_HELP.safeCancel}
          </HelpHint>
          <HelpHint showTopic label="명시적 재시도">
            {OPERATION_FIELD_HELP.explicitRetry}
          </HelpHint>
        </div>
      </details>

      <div className="toolbar toolbar--wrap">
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="op-status-filter">진행 상태 (Status)</label>
            <HelpHint label="진행 상태">{OPERATION_FIELD_HELP.status}</HelpHint>
          </div>
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
            <option value="ALL">전체</option>
            {[...KNOWN_STATUSES].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <label className="toolbar__field" htmlFor="op-type-filter">
          <span>작업 유형 (Type)</span>
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
            <option value="ALL">전체</option>
            {[...KNOWN_TYPES].map((t) => (
              <option key={t} value={t}>
                {OPERATION_TYPE_LABELS[t]
                  ? `${OPERATION_TYPE_LABELS[t]} (${t})`
                  : t}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="op-active-filter">
          <span>진행 여부 (Active)</span>
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
            <option value="ALL">전체</option>
            <option value="ACTIVE">진행 중 (ACTIVE)</option>
            <option value="TERMINAL">종료됨 (TERMINAL)</option>
          </select>
        </label>
        <p className="toolbar__hint">
          Status와 Active는 동시에 사용할 수 없습니다. 필터 값(QUEUED, SWITCH
          등)은 API 원본 코드를 유지합니다.
        </p>
      </div>

      {listError ? (
        <SectionError
          title={listError.title}
          message={listError.message}
          action={listError.action}
        />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="작업 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="작업" apiTerm="Operation" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="작업 유형"
                      apiTerm="Type"
                      help={OPERATION_FIELD_HELP.type}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="진행 상태"
                      apiTerm="Status"
                      help={OPERATION_FIELD_HELP.status}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="전환 방식"
                      apiTerm="Strategy"
                      help={OPERATION_FIELD_HELP.strategy}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="API 별칭" apiTerm="Endpoint" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="기존 대상"
                      apiTerm="Source"
                      help={OPERATION_FIELD_HELP.sourceTarget}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="변경 대상"
                      apiTerm="Target"
                      help={OPERATION_FIELD_HELP.sourceTarget}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="생성 시각" apiTerm="Created" />
                  </th>
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
                    <td>{formatOperationTypeDisplay(op.operation_type)}</td>
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
