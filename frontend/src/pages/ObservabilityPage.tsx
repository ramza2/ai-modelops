import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getInvocationSummary,
  getRuntimeLatest,
} from '../api/observability'
import type {
  InvocationSummaryItem,
  InvocationSummaryResponse,
  RuntimeSnapshot,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parseBoundedInt } from '../utils/query'

const GROUP_BY = new Set(['client', 'alias', 'deployment'] as const)
type GroupBy = 'client' | 'alias' | 'deployment'

function parseGroupBy(raw: string | null): GroupBy {
  if (!raw) return 'client'
  const lower = raw.toLowerCase()
  if (GROUP_BY.has(lower as GroupBy)) return lower as GroupBy
  return 'client'
}

function formatNum(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(digits)
}

function tokenCoverage(item: InvocationSummaryItem): string {
  const req = item.request_count || 0
  const tok = item.tokenized_request_count || 0
  if (req <= 0) return '—'
  return `${((tok / req) * 100).toFixed(1)}% (${tok}/${req})`
}

function formatRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—'
  return `${ratio.toFixed(3)} (${(ratio * 100).toFixed(1)}%)`
}

export function ObservabilityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const hours = parseBoundedInt(searchParams.get('hours'), {
    min: 1,
    max: 720,
    defaultValue: 24,
  })
  const groupBy = parseGroupBy(searchParams.get('group_by'))
  const [draftHours, setDraftHours] = useState(String(hours))

  const [invocations, setInvocations] =
    useState<InvocationSummaryResponse | null>(null)
  const [invLoading, setInvLoading] = useState(true)
  const [invRefreshing, setInvRefreshing] = useState(false)
  const [invError, setInvError] = useState<string | null>(null)

  const [runtimeItems, setRuntimeItems] = useState<RuntimeSnapshot[] | null>(
    null,
  )
  const [rtLoading, setRtLoading] = useState(true)
  const [rtRefreshing, setRtRefreshing] = useState(false)
  const [rtError, setRtError] = useState<string | null>(null)

  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const invAbortRef = useRef<AbortController | null>(null)
  const rtAbortRef = useRef<AbortController | null>(null)
  const invGenRef = useRef(0)
  const rtGenRef = useRef(0)
  const invLoadedRef = useRef(false)
  const rtLoadedRef = useRef(false)

  useEffect(() => {
    setDraftHours(String(hours))
  }, [hours])

  const syncUrl = useCallback(
    (next: { hours: number; groupBy: GroupBy }) => {
      const params = new URLSearchParams()
      if (next.hours !== 24) params.set('hours', String(next.hours))
      if (next.groupBy !== 'client') params.set('group_by', next.groupBy)
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  const applyHours = useCallback(() => {
    const nextHours = parseBoundedInt(draftHours, {
      min: 1,
      max: 720,
      defaultValue: 24,
    })
    setDraftHours(String(nextHours))
    syncUrl({ hours: nextHours, groupBy })
  }, [draftHours, groupBy, syncUrl])

  useEffect(() => {
    const rawHours = searchParams.get('hours')
    const rawGroup = searchParams.get('group_by')
    let needsFix = false
    const fixed = new URLSearchParams()

    const parsedHours = parseBoundedInt(rawHours, {
      min: 1,
      max: 720,
      defaultValue: 24,
    })
    if (rawHours !== null && String(parsedHours) !== rawHours) needsFix = true
    if (parsedHours !== 24) fixed.set('hours', String(parsedHours))

    const parsedGroup = parseGroupBy(rawGroup)
    if (rawGroup !== null && rawGroup !== '') {
      if (rawGroup !== parsedGroup) needsFix = true
      if (parsedGroup !== 'client') fixed.set('group_by', parsedGroup)
      else if (rawGroup !== 'client') needsFix = true
    }

    if (needsFix) setSearchParams(fixed, { replace: true })
  }, [searchParams, setSearchParams])

  const loadInvocations = useCallback(
    async (mode: 'initial' | 'refresh') => {
      invAbortRef.current?.abort()
      const controller = new AbortController()
      invAbortRef.current = controller
      const gen = ++invGenRef.current
      if (mode === 'initial' && !invLoadedRef.current) setInvLoading(true)
      else setInvRefreshing(true)
      setInvError(null)
      try {
        const data = await getInvocationSummary({
          hours,
          groupBy,
          signal: controller.signal,
        })
        if (controller.signal.aborted || gen !== invGenRef.current) return
        setInvocations(data)
        setLastUpdated(new Date())
        invLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== invGenRef.current) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Invocation summary를 불러오지 못했습니다.'
        setInvError(message)
        if (!invLoadedRef.current) setInvocations(null)
      } finally {
        if (!controller.signal.aborted && gen === invGenRef.current) {
          setInvLoading(false)
          setInvRefreshing(false)
        }
      }
    },
    [hours, groupBy],
  )

  const loadRuntime = useCallback(async (mode: 'initial' | 'refresh') => {
    rtAbortRef.current?.abort()
    const controller = new AbortController()
    rtAbortRef.current = controller
    const gen = ++rtGenRef.current
    if (mode === 'initial' && !rtLoadedRef.current) setRtLoading(true)
    else setRtRefreshing(true)
    setRtError(null)
    try {
      const data = await getRuntimeLatest(null, controller.signal)
      if (controller.signal.aborted || gen !== rtGenRef.current) return
      setRuntimeItems(data.items)
      setLastUpdated(new Date())
      rtLoadedRef.current = true
    } catch (err) {
      if (controller.signal.aborted || gen !== rtGenRef.current) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Runtime latest를 불러오지 못했습니다.'
      setRtError(message)
      if (!rtLoadedRef.current) setRuntimeItems(null)
    } finally {
      if (!controller.signal.aborted && gen === rtGenRef.current) {
        setRtLoading(false)
        setRtRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    void loadInvocations(invLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      invAbortRef.current?.abort()
    }
  }, [loadInvocations])

  useEffect(() => {
    void loadRuntime(rtLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      rtAbortRef.current?.abort()
    }
  }, [loadRuntime])

  const refreshing = invRefreshing || rtRefreshing
  const items = invocations?.items ?? []

  return (
    <AppShell
      title="Observability"
      description="DB에 저장된 Invocation 집계와 Managed Runtime snapshot입니다. 실시간 scrape가 아니며, Capacity Profile/상세는 Deployment observability에서 확인합니다."
      onRefresh={() => {
        void loadInvocations('refresh')
        void loadRuntime('refresh')
      }}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <form
        className="toolbar toolbar--wrap"
        onSubmit={(e) => {
          e.preventDefault()
          applyHours()
        }}
      >
        <label className="toolbar__field" htmlFor="obs-hours">
          <span>Hours (1–720)</span>
          <input
            id="obs-hours"
            type="number"
            inputMode="numeric"
            min={1}
            max={720}
            step={1}
            value={draftHours}
            onChange={(e) => setDraftHours(e.target.value)}
            onBlur={applyHours}
          />
        </label>
        <label className="toolbar__field" htmlFor="obs-group-by">
          <span>Group by</span>
          <select
            id="obs-group-by"
            value={groupBy}
            onChange={(e) =>
              syncUrl({ hours, groupBy: e.target.value as GroupBy })
            }
          >
            <option value="client">client</option>
            <option value="alias">alias</option>
            <option value="deployment">deployment</option>
          </select>
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn">
            적용
          </button>
        </div>
        <p className="toolbar__hint">
          hours는 1..720의 임의 정수, group_by는 client|alias|deployment입니다.
          Token percentile는 tokenized row만 사용하며 전체 요청 대비 토큰
          커버리지를 함께 표시합니다.
        </p>
      </form>

      <section className="detail-panel" aria-labelledby="inv-summary-heading">
        <h2 id="inv-summary-heading">Invocation Summary</h2>
        <p className="panel-hint">
          `GET /api/v1/observability/invocations/summary` 결과입니다. 전역
          percentile를 재계산하지 않고 API가 반환한 그룹 지표를 표시합니다.
        </p>
        {invError ? (
          <SectionError
            title={
              invocations
                ? 'Invocation summary 새로고침 실패'
                : 'Invocation summary 오류'
            }
            message={
              invocations
                ? `기존 summary를 표시하고 있습니다. 새로고침 실패: ${invError}`
                : invError
            }
          />
        ) : null}
        {invLoading && !invocations ? (
          <LoadingBlock label="Invocation summary를 불러오는 중…" />
        ) : null}
        {invocations && items.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Group</th>
                  {groupBy === 'deployment' ? (
                    <th scope="col">Deployment</th>
                  ) : null}
                  <th scope="col">Requests</th>
                  <th scope="col">Success</th>
                  <th scope="col">Error</th>
                  <th scope="col">Token coverage</th>
                  <th scope="col">Input p95</th>
                  <th scope="col">Output avg</th>
                  <th scope="col">Latency p95</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={`${groupBy}:${item.group_key}`}>
                    <td className="mono">
                      {groupBy === 'deployment' ? (
                        <Link
                          className="table-link mono"
                          to={`/observability/deployments/${item.group_key}`}
                        >
                          {shortId(item.group_key, 12)}
                        </Link>
                      ) : (
                        item.group_key
                      )}
                    </td>
                    {groupBy === 'deployment' ? (
                      <td>{item.deployment_name || '—'}</td>
                    ) : null}
                    <td>{item.request_count}</td>
                    <td>{item.success_count}</td>
                    <td>{item.error_count}</td>
                    <td>{tokenCoverage(item)}</td>
                    <td>{formatNum(item.input_tokens_p95, 0)}</td>
                    <td>{formatNum(item.output_tokens_avg, 1)}</td>
                    <td>{formatNum(item.latency_ms_p95, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {invocations && items.length === 0 ? (
          <p className="empty-state" role="status">
            선택한 구간에 Invocation 집계가 없습니다.
          </p>
        ) : null}
      </section>

      <section className="detail-panel" aria-labelledby="runtime-latest-heading">
        <h2 id="runtime-latest-heading">Runtime Latest (persisted)</h2>
        <p className="panel-hint">
          DB에 저장된 최신 Runtime metric snapshot입니다. Node Agent live scrape가
          아니며, 상세/history/Capacity Profile은 Deployment observability로
          이동합니다.
        </p>
        {rtError ? (
          <SectionError
            title={
              runtimeItems
                ? 'Runtime latest 새로고침 실패'
                : 'Runtime latest 오류'
            }
            message={
              runtimeItems
                ? `기존 runtime snapshot을 표시하고 있습니다. 새로고침 실패: ${rtError}`
                : rtError
            }
          />
        ) : null}
        {rtLoading && runtimeItems === null ? (
          <LoadingBlock label="Runtime latest를 불러오는 중…" />
        ) : null}
        {runtimeItems && runtimeItems.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  <th scope="col">Sampled</th>
                  <th scope="col">Availability</th>
                  <th scope="col">KV ratio</th>
                  <th scope="col">Running</th>
                  <th scope="col">Waiting</th>
                  <th scope="col">Error</th>
                </tr>
              </thead>
              <tbody>
                {runtimeItems.map((row) => (
                  <tr key={row.deployment_id}>
                    <td>
                      <Link
                        className="table-link"
                        to={`/observability/deployments/${row.deployment_id}`}
                      >
                        {row.deployment_name || shortId(row.deployment_id, 12)}
                      </Link>
                    </td>
                    <td>{formatApiDateTime(row.sampled_at)}</td>
                    <td>
                      <StatusBadge status={row.availability} />
                    </td>
                    <td>{formatRatio(row.kv_cache_usage_ratio)}</td>
                    <td>{formatNum(row.num_requests_running, 0)}</td>
                    <td>{formatNum(row.num_requests_waiting, 0)}</td>
                    <td>
                      {row.error_code || row.error_message
                        ? [row.error_code, row.error_message]
                            .filter(Boolean)
                            .join(': ')
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {runtimeItems && runtimeItems.length === 0 ? (
          <p className="empty-state" role="status">
            저장된 Runtime snapshot이 없습니다.
          </p>
        ) : null}
      </section>
    </AppShell>
  )
}
