import { useCallback, useEffect, useRef, useState } from 'react'
import {
  aggregateInvocations,
  fetchDashboard,
  type DashboardSnapshot,
} from '../api/dashboard'
import { AppShell } from '../components/AppShell'
import { HelpHint } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatCard } from '../components/StatCard'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, operationElapsed, shortId } from '../utils/date'
import { formatInt, formatMs, formatPercent } from '../utils/number'
import { DASHBOARD_FIELD_HELP, PAGE_INTRO } from '../utils/uiLabels'

const REFRESH_MS = 15_000

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

  const inv = aggregateInvocations(data?.invocations?.items)
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

  return (
    <AppShell
      title="대시보드"
      description={PAGE_INTRO.dashboard}
      onRefresh={refresh}
      refreshing={refreshing || loading}
      lastUpdated={data?.fetchedAt ?? null}
    >
      <details className="help-glossary">
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

      {fatal && !data ? (
        <SectionError
          title="Management API를 사용할 수 없습니다"
          message={fatal}
          onRetry={refresh}
        />
      ) : null}

      {refreshError && data ? (
        <SectionError
          title="최근 새로고침 실패"
          message={refreshError}
          onRetry={refresh}
        />
      ) : null}

      <section className="panel" aria-labelledby="cp-heading">
        <div className="panel__head">
          <h2 id="cp-heading">관리 시스템 상태</h2>
        </div>
        <p className="panel__intro">
          Health는 프로세스 생존, Ready는 의존성 준비입니다. 자세한 차이는
          「자세히 알아보기」를 보세요.
        </p>
        {loading && !data ? (
          <LoadingBlock rows={2} />
        ) : (
          <div className="stat-grid">
            <StatCard
              label="관리 시스템 Health"
              value={health.text}
              help={DASHBOARD_FIELD_HELP.healthVsReady}
              hint={data?.healthError || undefined}
            />
            <div className="stat-card">
              <h3 className="stat-card__label">Health 배지</h3>
              <StatusBadge status={health.status} label={health.text} />
            </div>
            <StatCard
              label="관리 시스템 Ready"
              value={ready.text}
              help={DASHBOARD_FIELD_HELP.healthVsReady}
              hint={data?.readyError || undefined}
            />
            <div className="stat-card">
              <h3 className="stat-card__label">Ready 배지</h3>
              <StatusBadge status={ready.status} label={ready.text} />
            </div>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="res-heading">
        <div className="panel__head">
          <h2 id="res-heading">서버·배포·API 연결 요약</h2>
        </div>
        <p className="panel__intro">
          등록·배포·실행·Healthy와 API 별칭 사용 설정·요청 처리(SERVING)는
          서로 다른 개념입니다. 상세는 「용어 및 상태 안내」를 보세요.
        </p>
        {loading && !data ? (
          <LoadingBlock />
        ) : (
          <>
            {data?.nodesError ? (
              <SectionError message={data.nodesError} onRetry={refresh} />
            ) : null}
            {data?.deploymentsError ? (
              <SectionError message={data.deploymentsError} onRetry={refresh} />
            ) : null}
            {data?.endpointsError ? (
              <SectionError message={data.endpointsError} onRetry={refresh} />
            ) : null}
            <div className="stat-grid">
              <StatCard
                label="전체 서버 (Node)"
                value={countDisplay(data?.nodesTotal, data?.nodesError)}
                unit="대"
              />
              <StatCard
                label="온라인 서버 (ONLINE Node)"
                value={countDisplay(data?.nodesOnline, data?.nodesError)}
                unit="대"
                help={DASHBOARD_FIELD_HELP.missingCounts}
              />
              <StatCard
                label="활성 모델 배포 (Active Deployments)"
                value={countDisplay(
                  data?.deploymentsActive,
                  data?.deploymentsError,
                )}
                unit="건"
                help={DASHBOARD_FIELD_HELP.deploymentStates}
              />
              <StatCard
                label="실행 중인 모델 (Running)"
                value={countDisplay(
                  data?.deploymentsRunning,
                  data?.deploymentsError,
                )}
                unit="건"
                help={DASHBOARD_FIELD_HELP.deploymentStates}
              />
              <StatCard
                label="정상 모델 (Healthy)"
                value={countDisplay(
                  data?.deploymentsHealthy,
                  data?.deploymentsError,
                )}
                unit="건"
                help={DASHBOARD_FIELD_HELP.deploymentStates}
              />
              <StatCard
                label="전체 API 연결 (Endpoint)"
                value={countDisplay(data?.endpointsTotal, data?.endpointsError)}
                unit="건"
              />
              <StatCard
                label="사용 설정 (Enabled)"
                value={countDisplay(
                  data?.endpointsEnabled,
                  data?.endpointsError,
                )}
                unit="건"
                help={DASHBOARD_FIELD_HELP.endpointTraffic}
              />
              <StatCard
                label="요청 처리 중 (Serving)"
                value={countDisplay(
                  data?.endpointsServing,
                  data?.endpointsError,
                )}
                unit="건"
                help={DASHBOARD_FIELD_HELP.endpointTraffic}
              />
              <StatCard
                label="진행 중인 작업 (Active Operations)"
                value={countDisplay(
                  data?.operationsActive,
                  data?.operationsError,
                )}
                unit="건"
              />
            </div>
            {!loading && data && data.nodesTotal === 0 && !data.nodesError ? (
              <p className="empty-hint">등록된 Node가 없습니다.</p>
            ) : null}
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="inv-heading">
        <div className="panel__head">
          <h2 id="inv-heading">최근 24시간 호출</h2>
        </div>
        <p className="panel__intro">
          최근 24시간 Gateway 호출 집계입니다. 요청 성공률은 모델 실행
          정상(HEALTHY)과 다릅니다. 상위 5% 기준(P95)은 배포 단위입니다.
        </p>
        {loading && !data ? (
          <LoadingBlock />
        ) : data?.invocationsError ? (
          <SectionError message={data.invocationsError} onRetry={refresh} />
        ) : (
          <>
            <div className="stat-grid">
              <StatCard
                label="최근 24시간 요청"
                value={formatInt(inv.requestCount)}
                unit="건"
              />
              <StatCard
                label="최근 24시간 오류"
                value={formatInt(inv.errorCount)}
                unit="건"
              />
              <StatCard
                label="요청 성공률"
                value={rateDisplay(inv.successRate, null)}
                help={DASHBOARD_FIELD_HELP.invocations}
              />
              <StatCard
                label="토큰 정보 수집률"
                value={rateDisplay(inv.tokenCoverage, null)}
                help={DASHBOARD_FIELD_HELP.invocations}
              />
            </div>
            {inv.requestCount === 0 ? (
              <p className="empty-hint">최근 24시간 호출 기록이 없습니다.</p>
            ) : (
              <div className="table-wrap">
                <table>
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
                    {inv.topDeployments.map((row) => (
                      <tr key={row.group_key}>
                        <td
                          className="table-cell-clip"
                          title={row.group_key}
                        >
                          {row.deployment_name || shortId(row.group_key, 12)}
                        </td>
                        <td>{formatInt(row.request_count)}</td>
                        <td>{formatInt(row.error_count)}</td>
                        <td>{metricCell(row.latency_ms_p95 ?? null, 'ms')}</td>
                        <td>
                          {metricCell(row.input_tokens_p95 ?? null, 'int')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="ops-heading">
        <div className="panel__head">
          <h2 id="ops-heading">최근 작업</h2>
        </div>
        {loading && !data ? (
          <LoadingBlock />
        ) : data?.operationsError ? (
          <SectionError message={data.operationsError} onRetry={refresh} />
        ) : !data?.operationsRecent?.length ? (
          <p className="empty-hint">표시할 Operation이 없습니다.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="sr-only">최근 Operation 목록</caption>
              <thead>
                <tr>
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
                      <td>{op.operation_type}</td>
                      <td>
                        <StatusBadge status={op.status} />
                      </td>
                      <td
                        className="table-cell-clip mono"
                        title={target || undefined}
                      >
                        {shortId(target)}
                      </td>
                      <td>{formatApiDateTime(op.started_at || op.created_at)}</td>
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
    </AppShell>
  )
}
