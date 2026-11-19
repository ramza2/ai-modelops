import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  aggregateInvocations,
  fetchDashboard,
  type DashboardSnapshot,
} from '../api/dashboard'
import { AppShell } from '../components/AppShell'
import { DashboardGpuSnapshot } from '../components/DashboardGpuSnapshot'
import { HelpHint } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import {
  formatApiDateTime,
  formatClock,
  operationElapsed,
  shortId,
} from '../utils/date'
import { formatInt, formatMs, formatPercent } from '../utils/number'
import {
  DASHBOARD_FIELD_HELP,
  PAGE_INTRO,
  formatOperationTypeDisplay,
} from '../utils/uiLabels'

const REFRESH_MS = 15_000

const FAILED_OP_STATUSES = new Set([
  'FAILED',
  'MANUAL_INTERVENTION_REQUIRED',
])

/** Deployment PK shape only — arbitrary group_key strings are not linkable. */
const DEPLOYMENT_UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function isDeploymentId(raw: string | null | undefined): boolean {
  if (!raw) return false
  if (raw.toLowerCase() === 'unknown') return false
  return DEPLOYMENT_UUID_RE.test(raw)
}

type AttentionItem = {
  id: string
  title: string
  detail: string
  to: string
  linkLabel: string
}

function controlPlaneLabel(
  kind: 'health' | 'ready',
  value: { status?: string } | null,
  error: string | null,
): { text: string; status: string } {
  if (error) return { text: '확인 불가', status: 'UNKNOWN' }
  if (!value) return { text: '미수집', status: 'UNKNOWN' }
  const raw = String(value.status || '').toUpperCase()
  if (kind === 'health') {
    if (raw === 'OK' || raw === 'HEALTHY' || raw === 'UP') {
      return { text: '정상', status: 'HEALTHY' }
    }
    return { text: raw || '오류', status: 'FAILED' }
  }
  if (raw === 'READY' || raw === 'OK') {
    return { text: '준비됨', status: 'HEALTHY' }
  }
  return { text: raw || '오류', status: 'FAILED' }
}

/** Distinguish 0 / 미수집 / 확인 불가 without changing aggregation. */
function countDisplay(
  value: number | null | undefined,
  sectionError: string | null | undefined,
): string {
  if (value === null || value === undefined) {
    return sectionError ? '확인 불가' : '미수집'
  }
  return formatInt(value)
}

function rateDisplay(
  value: number | null | undefined,
  sectionError: string | null | undefined,
): string {
  if (sectionError) return '확인 불가'
  if (value === null || value === undefined) return '미수집'
  return formatPercent(value)
}

function metricCell(
  value: number | null | undefined,
  kind: 'int' | 'ms',
): string {
  if (value === null || value === undefined) return '미수집'
  return kind === 'ms' ? formatMs(value) : formatInt(value)
}

function ratioLabel(
  numerator: number | null | undefined,
  denominator: number | null | undefined,
  sectionError: string | null | undefined,
): string {
  if (sectionError) return '확인 불가'
  if (numerator == null || denominator == null) return '미수집'
  return `${formatInt(numerator)} / ${formatInt(denominator)}`
}

function buildAttentionItems(
  data: DashboardSnapshot,
  inv: ReturnType<typeof aggregateInvocations>,
): AttentionItem[] {
  const items: AttentionItem[] = []

  if (data.nodesError) {
    items.push({
      id: 'nodes-error',
      title: '서버 목록 조회 실패',
      detail: data.nodesError,
      to: '/nodes',
      linkLabel: '서버 및 GPU 보기',
    })
  }
  if (data.deploymentsError) {
    items.push({
      id: 'deployments-error',
      title: '모델 배포 목록 조회 실패',
      detail: data.deploymentsError,
      to: '/deployments',
      linkLabel: '모델 배포 관리 보기',
    })
  }
  if (data.endpointsError) {
    items.push({
      id: 'endpoints-error',
      title: 'API 연결 목록 조회 실패',
      detail: data.endpointsError,
      to: '/endpoints',
      linkLabel: 'API 연결 관리 보기',
    })
  }
  if (data.operationsError) {
    items.push({
      id: 'operations-error',
      title: '작업 이력 조회 실패',
      detail: data.operationsError,
      to: '/operations',
      linkLabel: '작업 이력 보기',
    })
  }
  if (data.invocationsError) {
    items.push({
      id: 'invocations-error',
      title: '24시간 호출 집계 조회 실패',
      detail: data.invocationsError,
      to: '/observability',
      linkLabel: '운영 모니터링 보기',
    })
  }

  if (!data.invocationsError && inv.requestCount > 0 && inv.errorCount > 0) {
    items.push({
      id: 'inv-errors',
      title: '최근 24시간 오류 호출',
      detail: `오류 ${formatInt(inv.errorCount)}건 / 요청 ${formatInt(inv.requestCount)}건 (집계 기준)`,
      to: '/observability',
      linkLabel: '호출 집계 확인',
    })
  }

  for (const op of data.operationsRecent ?? []) {
    const status = String(op.status || '').toUpperCase()
    if (!FAILED_OP_STATUSES.has(status)) continue
    items.push({
      id: `op-${op.id}`,
      title: `최근 실패 작업 · ${formatOperationTypeDisplay(op.operation_type)}`,
      detail: op.error_message || op.error_code || status,
      to: `/operations/${op.id}`,
      linkLabel: '작업 상세 보기',
    })
  }

  return items
}

function KpiTile(props: {
  label: string
  value: string
  hint?: string
  to: string
  linkLabel: string
  help?: string
}) {
  return (
    <article className="dashboard-v2__kpi">
      <div className="dashboard-v2__kpi-head">
        <h3 className="dashboard-v2__kpi-label">{props.label}</h3>
        {props.help ? (
          <HelpHint label={props.label}>{props.help}</HelpHint>
        ) : null}
      </div>
      <p className="dashboard-v2__kpi-value">{props.value}</p>
      {props.hint ? (
        <p className="dashboard-v2__kpi-hint">{props.hint}</p>
      ) : null}
      <Link className="dashboard-v2__link" to={props.to}>
        {props.linkLabel}
      </Link>
    </article>
  )
}

export function DashboardPage() {
  const [data, setData] = useState<DashboardSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [fatal, setFatal] = useState<string | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const hasDataRef = useRef(false)

  const load = useCallback(async (mode: 'initial' | 'refresh') => {
    if (inFlight.current) return
    inFlight.current = true
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    if (mode === 'initial') setLoading(true)
    else setRefreshing(true)
    try {
      const snap = await fetchDashboard(ac.signal)
      if (!ac.signal.aborted) {
        hasDataRef.current = true
        setData(snap)
        setFatal(null)
        setRefreshError(null)
      }
    } catch (err) {
      if (ac.signal.aborted) return
      const message =
        err instanceof Error ? err.message : '대시보드를 불러오지 못했습니다.'
      if (!hasDataRef.current) {
        setFatal(message)
      } else {
        // Preserve prior data and last successful fetchedAt.
        setRefreshError(message)
      }
    } finally {
      inFlight.current = false
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void load('initial')
    const timer = window.setInterval(() => {
      void load('refresh')
    }, REFRESH_MS)
    return () => {
      window.clearInterval(timer)
      abortRef.current?.abort()
    }
  }, [load])

  const refresh = () => {
    void load('refresh')
  }

  const inv = useMemo(
    () => aggregateInvocations(data?.invocations?.items),
    [data?.invocations?.items],
  )
  const health = controlPlaneLabel(
    'health',
    data?.health ?? null,
    data?.healthError ?? null,
  )
  const ready = controlPlaneLabel(
    'ready',
    data?.ready ?? null,
    data?.readyError ?? null,
  )
  const attention = useMemo(
    () => (data ? buildAttentionItems(data, inv) : []),
    [data, inv],
  )

  return (
    <AppShell
      title="대시보드"
      description={PAGE_INTRO.dashboard}
      onRefresh={refresh}
      refreshing={refreshing || loading}
      lastUpdated={data?.fetchedAt ?? null}
    >
      <div className="dashboard-v2">
        {fatal && !data ? (
          <SectionError
            title="Management API를 사용할 수 없습니다"
            message={fatal}
            onRetry={refresh}
          />
        ) : null}

        {/* Total outage: error-only surface — do not claim "no attention items". */}
        {!(fatal && !data) ? (
          <>
        {refreshError && data ? (
          <div
            className="dashboard-v2__stale"
            role="status"
            data-testid="dashboard-stale-banner"
          >
            <SectionError
              title="최근 새로고침 실패"
              message={refreshError}
              onRetry={refresh}
            />
            <p className="dashboard-v2__stale-note">
              아래는 <strong>이전 수집값</strong>입니다. 마지막 성공 수집{' '}
              {formatClock(data.fetchedAt)}.
            </p>
          </div>
        ) : null}

        <section
          className="dashboard-v2__section"
          aria-labelledby="ops-summary-heading"
        >
          <div className="dashboard-v2__section-head">
            <h2 id="ops-summary-heading">운영 요약</h2>
            <p className="dashboard-v2__section-note">
              15초 주기 조회 기준입니다. Health/Ready와 집계 분모는 서로 다른
              조회입니다.
            </p>
          </div>

          {loading && !data ? (
            <LoadingBlock rows={2} />
          ) : (
            <>
              <div className="dashboard-v2__cp" role="group" aria-label="관리 시스템">
                <div className="dashboard-v2__cp-item">
                  <span className="dashboard-v2__cp-label">관리 시스템 Health</span>
                  <StatusBadge status={health.status} label={health.text} />
                  {data?.healthError ? (
                    <span className="dashboard-v2__cp-error">{data.healthError}</span>
                  ) : null}
                </div>
                <div className="dashboard-v2__cp-item">
                  <span className="dashboard-v2__cp-label">관리 시스템 Ready</span>
                  <StatusBadge status={ready.status} label={ready.text} />
                  {data?.readyError ? (
                    <span className="dashboard-v2__cp-error">{data.readyError}</span>
                  ) : null}
                </div>
                <p className="dashboard-v2__cp-help">
                  {DASHBOARD_FIELD_HELP.healthVsReady}
                </p>
              </div>

              <div className="dashboard-v2__kpi-grid">
                <KpiTile
                  label="서버 ONLINE / 전체"
                  value={ratioLabel(
                    data?.nodesOnline,
                    data?.nodesTotal,
                    data?.nodesError,
                  )}
                  hint="ONLINE ≠ 모델 실행 정상"
                  to="/nodes"
                  linkLabel="서버 및 GPU"
                  help={DASHBOARD_FIELD_HELP.missingCounts}
                />
                <KpiTile
                  label="모델 HEALTHY / RUNNING"
                  value={ratioLabel(
                    data?.deploymentsHealthy,
                    data?.deploymentsRunning,
                    data?.deploymentsError,
                  )}
                  hint={`활성 배포 ${countDisplay(data?.deploymentsActive, data?.deploymentsError)} · RUNNING과 HEALTHY는 별도 집계`}
                  to="/deployments"
                  linkLabel="모델 배포 관리"
                  help={DASHBOARD_FIELD_HELP.deploymentStates}
                />
                <KpiTile
                  label="API Serving / Enabled"
                  value={ratioLabel(
                    data?.endpointsServing,
                    data?.endpointsEnabled,
                    data?.endpointsError,
                  )}
                  hint={`전체 별칭 ${countDisplay(data?.endpointsTotal, data?.endpointsError)} · Serving과 Enabled는 독립 집계`}
                  to="/endpoints"
                  linkLabel="API 연결 관리"
                  help={DASHBOARD_FIELD_HELP.endpointTraffic}
                />
                <KpiTile
                  label="24시간 성공률 · 오류"
                  value={
                    data?.invocationsError
                      ? '확인 불가'
                      : inv.requestCount === 0
                        ? '데이터 없음'
                        : `${rateDisplay(inv.successRate, null)} · 오류 ${formatInt(inv.errorCount)}`
                  }
                  hint={
                    data?.invocationsError
                      ? undefined
                      : inv.requestCount === 0
                        ? '요청 0건은 100% 성공이 아닙니다'
                        : `요청 ${formatInt(inv.requestCount)}건 (최근 24시간)`
                  }
                  to="/observability"
                  linkLabel="운영 모니터링"
                  help={DASHBOARD_FIELD_HELP.invocations}
                />
                <KpiTile
                  label="진행 중인 작업"
                  value={countDisplay(
                    data?.operationsActive,
                    data?.operationsError,
                  )}
                  to="/operations"
                  linkLabel="작업 이력"
                />
              </div>
            </>
          )}
        </section>

        <section
          className="dashboard-v2__section"
          aria-labelledby="attention-heading"
        >
          <div className="dashboard-v2__section-head">
            <h2 id="attention-heading">확인이 필요한 항목</h2>
          </div>
          {loading && !data ? (
            <LoadingBlock rows={2} />
          ) : !data ? (
            <p className="dashboard-v2__quiet" role="status">
              주의 항목을 판단할 집계가 아직 없습니다. 확인 불가 상태를
              문제없음으로 보지 마세요.
            </p>
          ) : attention.length === 0 ? (
            <p className="dashboard-v2__quiet" role="status">
              현재 목록·집계에서 확인된 주의 항목이 없습니다. 미조회 구간이나
              다른 화면의 문제는 포함되지 않을 수 있습니다.
            </p>
          ) : (
            <ul className="dashboard-v2__attention">
              {attention.map((item) => (
                <li key={item.id} className="dashboard-v2__attention-item">
                  <div>
                    <p className="dashboard-v2__attention-title">{item.title}</p>
                    <p className="dashboard-v2__attention-detail">{item.detail}</p>
                  </div>
                  <Link className="dashboard-v2__link" to={item.to}>
                    {item.linkLabel}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section
          className="dashboard-v2__section"
          aria-labelledby="fleet-heading"
        >
          <div className="dashboard-v2__section-head">
            <h2 id="fleet-heading">서버 · 모델 · API 연결</h2>
          </div>
          {loading && !data ? (
            <LoadingBlock />
          ) : (
            <div className="dashboard-v2__fleet">
              <article className="dashboard-v2__fleet-card">
                <h3>서버 (Node)</h3>
                {data?.nodesError ? (
                  <SectionError message={data.nodesError} onRetry={refresh} />
                ) : (
                  <dl className="dashboard-v2__dl">
                    <div>
                      <dt>전체</dt>
                      <dd>{countDisplay(data?.nodesTotal, data?.nodesError)}</dd>
                    </div>
                    <div>
                      <dt>ONLINE</dt>
                      <dd>{countDisplay(data?.nodesOnline, data?.nodesError)}</dd>
                    </div>
                  </dl>
                )}
                {!loading && data && data.nodesTotal === 0 && !data.nodesError ? (
                  <p className="empty-hint">등록된 Node가 없습니다.</p>
                ) : null}
                <Link className="dashboard-v2__link" to="/nodes">
                  서버 및 GPU 상세
                </Link>
              </article>

              <article className="dashboard-v2__fleet-card">
                <h3>모델 배포 (Deployment)</h3>
                {data?.deploymentsError ? (
                  <SectionError
                    message={data.deploymentsError}
                    onRetry={refresh}
                  />
                ) : (
                  <dl className="dashboard-v2__dl">
                    <div>
                      <dt>활성</dt>
                      <dd>
                        {countDisplay(
                          data?.deploymentsActive,
                          data?.deploymentsError,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>RUNNING</dt>
                      <dd>
                        {countDisplay(
                          data?.deploymentsRunning,
                          data?.deploymentsError,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>HEALTHY</dt>
                      <dd>
                        {countDisplay(
                          data?.deploymentsHealthy,
                          data?.deploymentsError,
                        )}
                      </dd>
                    </div>
                  </dl>
                )}
                <p className="dashboard-v2__kpi-hint">
                  RUNNING과 HEALTHY는 별도 조건 집계이며 합/차로 장애 수를 만들지
                  않습니다.
                </p>
                <Link className="dashboard-v2__link" to="/deployments">
                  모델 배포 상세
                </Link>
              </article>

              <article className="dashboard-v2__fleet-card">
                <h3>API 연결 (Endpoint)</h3>
                {data?.endpointsError ? (
                  <SectionError
                    message={data.endpointsError}
                    onRetry={refresh}
                  />
                ) : (
                  <dl className="dashboard-v2__dl">
                    <div>
                      <dt>전체</dt>
                      <dd>
                        {countDisplay(
                          data?.endpointsTotal,
                          data?.endpointsError,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Enabled</dt>
                      <dd>
                        {countDisplay(
                          data?.endpointsEnabled,
                          data?.endpointsError,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>SERVING</dt>
                      <dd>
                        {countDisplay(
                          data?.endpointsServing,
                          data?.endpointsError,
                        )}
                      </dd>
                    </div>
                  </dl>
                )}
                <p className="dashboard-v2__kpi-hint">
                  Enabled와 SERVING은 독립 집계입니다.
                </p>
                <Link className="dashboard-v2__link" to="/endpoints">
                  API 연결 상세
                </Link>
              </article>
            </div>
          )}
        </section>

        {data ? (
          <DashboardGpuSnapshot
            nodes={data.gpuNodes}
            truncated={data.gpuTruncated}
          />
        ) : null}

        <section
          className="dashboard-v2__section"
          aria-labelledby="inv-heading"
        >
          <div className="dashboard-v2__section-head">
            <div>
              <h2 id="inv-heading">최근 24시간 호출</h2>
              <p className="dashboard-v2__section-note">
                Gateway 호출 집계입니다. 요청 성공률 ≠ Runtime HEALTHY. P95는
                배포 단위입니다.
              </p>
            </div>
            <Link className="dashboard-v2__link" to="/observability">
              운영 모니터링 상세
            </Link>
          </div>
          {loading && !data ? (
            <LoadingBlock />
          ) : data?.invocationsError ? (
            <SectionError message={data.invocationsError} onRetry={refresh} />
          ) : (
            <>
              <div className="dashboard-v2__kpi-grid dashboard-v2__kpi-grid--compact">
                <article className="dashboard-v2__kpi">
                  <h3 className="dashboard-v2__kpi-label">최근 24시간 요청</h3>
                  <p className="dashboard-v2__kpi-value">
                    {formatInt(inv.requestCount)}
                    <span className="dashboard-v2__kpi-unit">건</span>
                  </p>
                </article>
                <article className="dashboard-v2__kpi">
                  <h3 className="dashboard-v2__kpi-label">최근 24시간 오류</h3>
                  <p className="dashboard-v2__kpi-value">
                    {formatInt(inv.errorCount)}
                    <span className="dashboard-v2__kpi-unit">건</span>
                  </p>
                </article>
                <article className="dashboard-v2__kpi">
                  <div className="dashboard-v2__kpi-head">
                    <h3 className="dashboard-v2__kpi-label">요청 성공률</h3>
                    <HelpHint label="요청 성공률">
                      {DASHBOARD_FIELD_HELP.invocations}
                    </HelpHint>
                  </div>
                  <p className="dashboard-v2__kpi-value">
                    {rateDisplay(inv.successRate, null)}
                  </p>
                </article>
                <article className="dashboard-v2__kpi">
                  <div className="dashboard-v2__kpi-head">
                    <h3 className="dashboard-v2__kpi-label">토큰 정보 수집률</h3>
                    <HelpHint label="토큰 정보 수집률">
                      {DASHBOARD_FIELD_HELP.invocations}
                    </HelpHint>
                  </div>
                  <p className="dashboard-v2__kpi-value">
                    {rateDisplay(inv.tokenCoverage, null)}
                  </p>
                </article>
              </div>
              {inv.requestCount === 0 ? (
                <p className="empty-hint">최근 24시간 호출 기록이 없습니다.</p>
              ) : (
                <div className="table-wrap">
                  <table className="dashboard-v2__table">
                    <caption className="sr-only">
                      모델 배포별 호출 상위 5건(요청 수 기준). 상위 5% 기준(P95)은
                      배포 단위이며 전역·최대값이 아닙니다.
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">모델 배포</th>
                        <th scope="col">요청 건수</th>
                        <th scope="col">오류</th>
                        <th scope="col">응답시간 상위 5% 기준(P95)</th>
                        <th scope="col">입력 토큰 상위 5% 기준(P95)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {inv.topDeployments.map((row) => {
                        const label =
                          row.deployment_name || shortId(row.group_key, 12)
                        const linkable = isDeploymentId(row.group_key)
                        return (
                          <tr key={row.group_key}>
                            <td
                              className="table-cell-clip"
                              title={row.group_key}
                            >
                              {linkable ? (
                                <Link
                                  className="dashboard-v2__link"
                                  to={`/deployments/${row.group_key}`}
                                >
                                  {label}
                                </Link>
                              ) : (
                                <span>{label}</span>
                              )}
                            </td>
                            <td>{formatInt(row.request_count)}</td>
                            <td>{formatInt(row.error_count)}</td>
                            <td>
                              {metricCell(row.latency_ms_p95 ?? null, 'ms')}
                            </td>
                            <td>
                              {metricCell(row.input_tokens_p95 ?? null, 'int')}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>

        <section
          className="dashboard-v2__section"
          aria-labelledby="ops-heading"
        >
          <div className="dashboard-v2__section-head">
            <h2 id="ops-heading">최근 작업</h2>
            <Link className="dashboard-v2__link" to="/operations">
              작업 이력 전체
            </Link>
          </div>
          {loading && !data ? (
            <LoadingBlock />
          ) : data?.operationsError ? (
            <SectionError message={data.operationsError} onRetry={refresh} />
          ) : !data?.operationsRecent?.length ? (
            <p className="empty-hint">표시할 Operation이 없습니다.</p>
          ) : (
            <div className="table-wrap">
              <table className="dashboard-v2__table">
                <caption className="sr-only">최근 Operation 목록</caption>
                <thead>
                  <tr>
                    <th scope="col">작업</th>
                    <th scope="col">작업 유형 (Type)</th>
                    <th scope="col">상태 (Status)</th>
                    <th scope="col">대상 (Target / Endpoint)</th>
                    <th scope="col">시작 시각 (Started)</th>
                    <th scope="col">경과 (Duration)</th>
                    <th scope="col">오류 (Error)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.operationsRecent.map((op) => {
                    const target =
                      op.target_deployment_id ||
                      op.endpoint_alias_id ||
                      op.source_deployment_id
                    return (
                      <tr key={op.id}>
                        <td>
                          <Link
                            className="dashboard-v2__link mono"
                            to={`/operations/${op.id}`}
                          >
                            {shortId(op.id, 10)}
                          </Link>
                        </td>
                        <td>{formatOperationTypeDisplay(op.operation_type)}</td>
                        <td>
                          <StatusBadge status={op.status} />
                        </td>
                        <td
                          className="table-cell-clip mono"
                          title={target || undefined}
                        >
                          {shortId(target)}
                        </td>
                        <td>
                          {formatApiDateTime(op.started_at || op.created_at)}
                        </td>
                        <td>{operationElapsed(op)}</td>
                        <td
                          className="table-cell-clip"
                          title={op.error_message || undefined}
                        >
                          {op.error_code || '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <details className="help-glossary dashboard-v2__glossary">
          <summary>자세히 알아보기</summary>
          <p className="panel-hint" role="note">
            {DASHBOARD_FIELD_HELP.overview}
          </p>
          <div className="help-glossary__items">
            <HelpHint showTopic label="Health와 Ready">
              {DASHBOARD_FIELD_HELP.healthVsReady}
            </HelpHint>
            <HelpHint showTopic label="배포·실행·정상">
              {DASHBOARD_FIELD_HELP.deploymentStates}
            </HelpHint>
            <HelpHint showTopic label="API 연결·Traffic">
              {DASHBOARD_FIELD_HELP.endpointTraffic}
            </HelpHint>
            <HelpHint showTopic label="24시간 호출 집계">
              {DASHBOARD_FIELD_HELP.invocations}
            </HelpHint>
            <HelpHint showTopic label="상위 5% 기준(P95)">
              {DASHBOARD_FIELD_HELP.p95}
            </HelpHint>
            <HelpHint showTopic label="0 / 미수집 / 확인 불가">
              {DASHBOARD_FIELD_HELP.missingCounts}
            </HelpHint>
          </div>
        </details>
          </>
        ) : null}
      </div>
    </AppShell>
  )
}
