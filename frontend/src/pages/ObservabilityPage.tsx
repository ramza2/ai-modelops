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
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parseBoundedInt } from '../utils/query'
import { OBSERVABILITY_FIELD_HELP } from '../utils/uiLabels'

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

/** Client-group cell: prefer registered display_name; keep group_key as service key. */
export function ClientGroupLabel({ item }: { item: InvocationSummaryItem }) {
  if (item.group_key === 'unknown') {
    return <span>미식별 시스템</span>
  }
  if (item.client_app_id && item.client_display_name) {
    return (
      <div>
        <Link className="table-link" to={`/clients/${item.client_app_id}`}>
          {item.client_display_name}
        </Link>
        <div className="secondary-text mono">{item.group_key}</div>
      </div>
    )
  }
  return (
    <div>
      <span>미등록 시스템</span>
      <div className="secondary-text mono">{item.group_key}</div>
    </div>
  )
}

function formatRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—'
  return `${ratio.toFixed(3)} (${(ratio * 100).toFixed(1)}%)`
}

/** Persisted metric availability is NOT the current lifecycle state. */
export const RUNTIME_STALE_MS = 120_000

export function displayedRuntimeAvailability(
  row: RuntimeSnapshot,
  now = Date.now(),
): string {
  if (row.retired_at) return 'RETIRED'
  if (row.runtime_status && row.runtime_status !== 'RUNNING') {
    return row.runtime_status
  }
  const sampled = row.sampled_at ? Date.parse(row.sampled_at) : NaN
  if (!Number.isFinite(sampled) || sampled > now + 30_000 || now - sampled > RUNTIME_STALE_MS) {
    return 'STALE'
  }
  return row.availability
}

export function ObservabilityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const hours = parseBoundedInt(searchParams.get('hours'), {
    min: 1,
    max: 720,
    defaultValue: 24,
  })
  const groupBy = parseGroupBy(searchParams.get('group_by'))
  const includeInactive = searchParams.get('include_inactive') === 'true'
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
    (next: { hours: number; groupBy: GroupBy; includeInactive: boolean }) => {
      const params = new URLSearchParams()
      if (next.hours !== 24) params.set('hours', String(next.hours))
      if (next.groupBy !== 'client') params.set('group_by', next.groupBy)
      if (next.includeInactive) params.set('include_inactive', 'true')
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
    syncUrl({ hours: nextHours, groupBy, includeInactive })
  }, [draftHours, groupBy, includeInactive, syncUrl])

  useEffect(() => {
    const rawHours = searchParams.get('hours')
    const rawGroup = searchParams.get('group_by')
    const rawInactive = searchParams.get('include_inactive')
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

    if (rawInactive === 'true') fixed.set('include_inactive', 'true')
    else if (rawInactive !== null) needsFix = true

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
      const data = await getRuntimeLatest(null, controller.signal, !includeInactive)
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
  }, [includeInactive])

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
      title="운영 모니터링"
      description="DB에 저장된 AI 요청 집계와 모델 실행 스냅샷입니다. 실시간 측정 화면이 아니며, Capacity Profile/상세는 Deployment 모니터링에서 확인합니다."
      onRefresh={() => {
        void loadInvocations('refresh')
        void loadRuntime('refresh')
      }}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {OBSERVABILITY_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="AI 요청 사용 현황">
            {OBSERVABILITY_FIELD_HELP.invocations}
          </HelpHint>
          <HelpHint showTopic label="토큰·지연시간">
            {OBSERVABILITY_FIELD_HELP.tokens} {OBSERVABILITY_FIELD_HELP.latency}
          </HelpHint>
          <HelpHint showTopic label="최근 모델 실행 상태">
            {OBSERVABILITY_FIELD_HELP.runtimeLatest}
          </HelpHint>
          <HelpHint showTopic label="측정값 없음 vs 0">
            {OBSERVABILITY_FIELD_HELP.missingVsZero}
          </HelpHint>
          <HelpHint showTopic label="GPU VRAM">
            {OBSERVABILITY_FIELD_HELP.gpuVram}
          </HelpHint>
        </div>
      </details>

      <form
        className="toolbar toolbar--wrap"
        onSubmit={(e) => {
          e.preventDefault()
          applyHours()
        }}
      >
        <label className="toolbar__field" htmlFor="obs-hours">
          <span>조회 기간 Hours (1–720)</span>
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
          <span>그룹 기준 (Group by)</span>
          <select
            id="obs-group-by"
            value={groupBy}
            onChange={(e) =>
              syncUrl({ hours, groupBy: e.target.value as GroupBy, includeInactive })
            }
          >
            <option value="client">client — 사용 시스템</option>
            <option value="alias">alias — API 별칭</option>
            <option value="deployment">deployment — 모델 배포</option>
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
          커버리지를 함께 표시합니다. 「데이터 갱신 시각」은 화면 새로고침
          시각이며 샘플 시각과 다릅니다.
        </p>
      </form>

      <section className="detail-panel" aria-labelledby="inv-summary-heading">
        <h2 id="inv-summary-heading">AI 요청 사용 현황 (Invocation Summary)</h2>
        <p className="panel-hint">
          {OBSERVABILITY_FIELD_HELP.invocations} 전역 percentile를 재계산하지
          않고 API가 반환한 그룹 지표를 표시합니다.
        </p>
        {invError ? (
          <SectionError
            title={
              invocations
                ? 'AI 요청 사용 현황 새로고침 실패'
                : 'AI 요청 사용 현황 오류'
            }
            message={
              invocations
                ? `기존 집계를 표시하고 있습니다. 원인: ${invError}`
                : `원인: ${invError}`
            }
            action={
              invocations
                ? '네트워크·권한을 확인한 뒤 새로고침을 다시 시도하세요.'
                : '잠시 후 새로고침을 다시 시도하세요.'
            }
          />
        ) : null}
        {invLoading && !invocations ? (
          <LoadingBlock label="AI 요청 사용 현황을 불러오는 중…" />
        ) : null}
        {invocations && items.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="그룹" apiTerm="Group" />
                  </th>
                  {groupBy === 'deployment' ? (
                    <th scope="col">
                      <TermLabel text="모델 배포" apiTerm="Deployment" />
                    </th>
                  ) : null}
                  <th scope="col">
                    <TermLabel text="요청 건수" apiTerm="Requests" />
                  </th>
                  <th scope="col">
                    <TermLabel text="성공" apiTerm="Success" />
                  </th>
                  <th scope="col">
                    <TermLabel text="오류" apiTerm="Error" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="토큰 집계 포함률"
                      apiTerm="Token coverage"
                      help={OBSERVABILITY_FIELD_HELP.invocations}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="입력 토큰 95백분위"
                      apiTerm="Input p95"
                      help={OBSERVABILITY_FIELD_HELP.tokens}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="평균 출력 토큰"
                      apiTerm="Output avg"
                      help={OBSERVABILITY_FIELD_HELP.tokens}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="응답시간 95백분위"
                      apiTerm="Latency p95"
                      help={OBSERVABILITY_FIELD_HELP.latency}
                    />
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={`${groupBy}:${item.group_key}`}>
                    <td className={groupBy === 'client' ? undefined : 'mono'}>
                      {groupBy === 'deployment' ? (
                        <Link
                          className="table-link mono"
                          to={`/observability/deployments/${item.group_key}`}
                        >
                          {shortId(item.group_key, 12)}
                        </Link>
                      ) : groupBy === 'client' ? (
                        <ClientGroupLabel item={item} />
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
        <h2 id="runtime-latest-heading">
          최근 모델 실행 상태 (Runtime Latest)
        </h2>
        <p className="panel-hint">
          {OBSERVABILITY_FIELD_HELP.runtimeLatest}{' '}
          {OBSERVABILITY_FIELD_HELP.runningWaiting} 기본 목록은 현재
          RUNNING·미종료 Deployment만 표시합니다. 과거 스냅샷과 상세/history는
          삭제하지 않습니다.
        </p>
        <label className="checkbox-label" htmlFor="obs-include-inactive">
          <input
            id="obs-include-inactive"
            type="checkbox"
            checked={includeInactive}
            onChange={(e) =>
              syncUrl({ hours, groupBy, includeInactive: e.target.checked })
            }
          />
          종료·중지된 Deployment 포함 (과거 데이터)
        </label>
        {rtError ? (
          <SectionError
            title={
              runtimeItems
                ? '최근 모델 실행 상태 새로고침 실패'
                : '최근 모델 실행 상태 오류'
            }
            message={
              runtimeItems
                ? `기존 runtime snapshot을 표시하고 있습니다. 원인: ${rtError}`
                : `원인: ${rtError}`
            }
            action={
              runtimeItems
                ? '네트워크·권한을 확인한 뒤 새로고침을 다시 시도하세요.'
                : '잠시 후 새로고침을 다시 시도하세요.'
            }
          />
        ) : null}
        {rtLoading && runtimeItems === null ? (
          <LoadingBlock label="최근 모델 실행 상태를 불러오는 중…" />
        ) : null}
        {runtimeItems && runtimeItems.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="모델 배포" apiTerm="Deployment" />
                  </th>
                  <th scope="col">
                    <TermLabel text="샘플 시각" apiTerm="Sampled" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="가용성"
                      apiTerm="Availability"
                      help={OBSERVABILITY_FIELD_HELP.runtimeLatest}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="KV 캐시 사용 비율"
                      apiTerm="KV ratio"
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="처리 중"
                      apiTerm="Running"
                      help={OBSERVABILITY_FIELD_HELP.runningWaiting}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="대기 중"
                      apiTerm="Waiting"
                      help={OBSERVABILITY_FIELD_HELP.runningWaiting}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="오류" apiTerm="Error" />
                  </th>
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
                      <StatusBadge status={displayedRuntimeAvailability(row)} />
                      {displayedRuntimeAvailability(row) !== row.availability ? (
                        <div className="secondary-text">
                          수집 당시: {row.availability}
                        </div>
                      ) : null}
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
            {includeInactive
              ? '저장된 Runtime snapshot이 없습니다.'
              : '현재 실행 중인 Deployment의 Runtime snapshot이 없습니다.'}
          </p>
        ) : null}
      </section>
    </AppShell>
  )
}
