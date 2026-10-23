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
import { OBSERVABILITY_FIELD_HELP, PAGE_INTRO } from '../utils/uiLabels'

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
      description={PAGE_INTRO.observability}
      onRefresh={() => {
        void loadInvocations('refresh')
        void loadRuntime('refresh')
      }}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <details className="help-glossary">
        <summary>자세히 알아보기</summary>
        <p className="panel-hint" role="note">
          {OBSERVABILITY_FIELD_HELP.overview}
        </p>
        <div className="help-glossary__items">
          <HelpHint showTopic label="AI 요청 사용 현황">
            {OBSERVABILITY_FIELD_HELP.invocations}
          </HelpHint>
          <HelpHint showTopic label="토큰·응답시간">
            {OBSERVABILITY_FIELD_HELP.tokens} {OBSERVABILITY_FIELD_HELP.latency}
          </HelpHint>
          <HelpHint showTopic label="최근 모델 실행 상태">
            {OBSERVABILITY_FIELD_HELP.runtimeLatest}
          </HelpHint>
          <HelpHint showTopic label="측정값 없음과 0">
            {OBSERVABILITY_FIELD_HELP.missingVsZero}
          </HelpHint>
          <HelpHint showTopic label="GPU 메모리">
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
          <span>조회 기간(시간, 1–720)</span>
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
            aria-label="조회 기간 Hours"
          />
        </label>
        <label className="toolbar__field" htmlFor="obs-group-by">
          <span>집계 기준</span>
          <select
            id="obs-group-by"
            value={groupBy}
            onChange={(e) =>
              syncUrl({ hours, groupBy: e.target.value as GroupBy, includeInactive })
            }
          >
            <option value="client">사용 시스템별</option>
            <option value="alias">API 별칭별</option>
            <option value="deployment">모델 배포별</option>
          </select>
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn">
            적용
          </button>
        </div>
      </form>

      <section className="detail-panel" aria-labelledby="inv-summary-heading">
        <div className="detail-panel__heading">
          <h2 id="inv-summary-heading">AI 요청 사용 현황</h2>
          <HelpHint label="AI 요청 사용 현황">
            {OBSERVABILITY_FIELD_HELP.invocations} API가 반환한 그룹 지표를
            그대로 표시하며 전역 백분위를 다시 계산하지 않습니다.
          </HelpHint>
        </div>
        <p className="panel-hint">
          요청·성공·오류는 HTTP 상태 기준 집계입니다. 토큰 정보 수집률은
          토큰이 기록된 요청 비율입니다.
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
                    <TermLabel
                      text="그룹"
                      apiTerm="group_key"
                      showApiTerm={false}
                      help="사용 시스템은 표시 이름이 우선이며, 서비스 키는 보조로 확인할 수 있습니다. 미등록·미식별(unknown)은 오류로 취급하지 않습니다."
                    />
                  </th>
                  {groupBy === 'deployment' ? (
                    <th scope="col">
                      <TermLabel
                        text="모델 배포"
                        apiTerm="deployment_name"
                        showApiTerm={false}
                      />
                    </th>
                  ) : null}
                  <th scope="col">
                    <TermLabel
                      text="요청 건수"
                      apiTerm="request_count"
                      showApiTerm={false}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="성공"
                      apiTerm="success_count"
                      showApiTerm={false}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="오류"
                      apiTerm="error_count"
                      showApiTerm={false}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="토큰 정보 수집률"
                      apiTerm="Token coverage"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.invocations}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="입력 토큰 상위 5% 기준(P95)"
                      apiTerm="input_tokens_p95"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.tokens}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="평균 출력 토큰"
                      apiTerm="output_tokens_avg"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.tokens}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="응답시간 상위 5% 기준(P95)"
                      apiTerm="latency_ms_p95"
                      showApiTerm={false}
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
            선택한 구간에 AI 요청 집계가 없습니다.
          </p>
        ) : null}
      </section>

      <section className="detail-panel" aria-labelledby="runtime-latest-heading">
        <div className="detail-panel__heading">
          <h2 id="runtime-latest-heading">최근 모델 실행 상태</h2>
          <HelpHint label="최근 모델 실행 상태">
            {OBSERVABILITY_FIELD_HELP.runtimeLatest}{' '}
            {OBSERVABILITY_FIELD_HELP.runningWaiting}
          </HelpHint>
        </div>
        <p className="panel-hint">
          기본 목록은 현재 실행 중·미종료 배포만 표시합니다. 과거 스냅샷은
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
          종료·중지된 배포 포함 (과거 데이터)
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
                    <TermLabel
                      text="모델 배포"
                      apiTerm="deployment_name"
                      showApiTerm={false}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="샘플 시각"
                      apiTerm="sampled_at"
                      showApiTerm={false}
                      help="저장된 스냅샷 시각입니다. 화면 새로고침 시각과 다릅니다."
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="가용성"
                      apiTerm="availability"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.runtimeLatest}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="KV 캐시 사용 비율"
                      apiTerm="kv_cache_usage_ratio"
                      showApiTerm={false}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="처리 중"
                      apiTerm="num_requests_running"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.runningWaiting}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="대기 중"
                      apiTerm="num_requests_waiting"
                      showApiTerm={false}
                      help={OBSERVABILITY_FIELD_HELP.runningWaiting}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="오류" apiTerm="error" showApiTerm={false} />
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
