import { useCallback, useEffect, useRef, useState } from 'react'
import {
  aggregateInvocations,
  fetchDashboard,
  type DashboardSnapshot,
} from '../api/dashboard'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatCard } from '../components/StatCard'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, operationElapsed, shortId } from '../utils/date'
import { formatInt, formatMs, formatPercent } from '../utils/number'

const REFRESH_MS = 15_000

function controlPlaneLabel(
  kind: 'health' | 'ready',
  value: { status?: string } | null,
  error: string | null,
): { text: string; status: string } {
  if (error) return { text: '확인 불가', status: 'UNKNOWN' }
  if (!value) return { text: '—', status: 'UNKNOWN' }
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
      title="Dashboard"
      description="Control Plane, 자원, Endpoint, Operation, 최근 24시간 호출 현황을 한눈에 확인합니다. (읽기 전용)"
      onRefresh={refresh}
      refreshing={refreshing || loading}
      lastUpdated={data?.fetchedAt ?? null}
    >
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
          <h2 id="cp-heading">Control Plane</h2>
        </div>
        {loading && !data ? (
          <LoadingBlock rows={2} />
        ) : (
          <div className="stat-grid">
            <StatCard
              label="Backend Health"
              value={health.text}
              hint={data?.healthError || undefined}
            />
            <div className="stat-card">
              <h3 className="stat-card__label">상태 배지</h3>
              <StatusBadge status={health.status} label={health.text} />
            </div>
            <StatCard
              label="Backend Readiness"
              value={ready.text}
              hint={data?.readyError || undefined}
            />
            <div className="stat-card">
              <h3 className="stat-card__label">준비 상태</h3>
              <StatusBadge status={ready.status} label={ready.text} />
            </div>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="res-heading">
        <div className="panel__head">
          <h2 id="res-heading">자원 요약</h2>
        </div>
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
              <StatCard label="전체 Node" value={formatInt(data?.nodesTotal)} />
              <StatCard label="ONLINE Node" value={formatInt(data?.nodesOnline)} />
              <StatCard
                label="Active Deployments"
                value={formatInt(data?.deploymentsActive)}
              />
              <StatCard
                label="Running"
                value={formatInt(data?.deploymentsRunning)}
              />
              <StatCard
                label="Healthy"
                value={formatInt(data?.deploymentsHealthy)}
              />
              <StatCard
                label="전체 Endpoint"
                value={formatInt(data?.endpointsTotal)}
              />
              <StatCard
                label="Enabled"
                value={formatInt(data?.endpointsEnabled)}
              />
              <StatCard
                label="Serving"
                value={formatInt(data?.endpointsServing)}
              />
              <StatCard
                label="Active Operations"
                value={formatInt(data?.operationsActive)}
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
          <h2 id="inv-heading">24시간 호출</h2>
        </div>
        {loading && !data ? (
          <LoadingBlock />
        ) : data?.invocationsError ? (
          <SectionError message={data.invocationsError} onRetry={refresh} />
        ) : (
          <>
            <div className="stat-grid">
              <StatCard
                label="24h Requests"
                value={formatInt(inv.requestCount)}
              />
              <StatCard label="24h Errors" value={formatInt(inv.errorCount)} />
              <StatCard
                label="24h Success Rate"
                value={formatPercent(inv.successRate)}
              />
              <StatCard
                label="Token Usage Coverage"
                value={formatPercent(inv.tokenCoverage)}
              />
            </div>
            {inv.requestCount === 0 ? (
              <p className="empty-hint">최근 24시간 호출 기록이 없습니다.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <caption className="sr-only">
                    Deployment별 호출 상위 5건 (요청 수 기준). P95는
                    Deployment 단위이며 전역 P95가 아닙니다.
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Deployment</th>
                      <th scope="col">Requests</th>
                      <th scope="col">Errors</th>
                      <th scope="col">P95 Latency</th>
                      <th scope="col">P95 Input Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inv.topDeployments.map((row) => (
                      <tr key={row.group_key}>
                        <td title={row.group_key}>
                          {row.deployment_name || shortId(row.group_key, 12)}
                        </td>
                        <td>{formatInt(row.request_count)}</td>
                        <td>{formatInt(row.error_count)}</td>
                        <td>{formatMs(row.latency_ms_p95 ?? null)}</td>
                        <td>{formatInt(row.input_tokens_p95 ?? null)}</td>
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
                  <th scope="col">Type</th>
                  <th scope="col">Status</th>
                  <th scope="col">Target / Endpoint</th>
                  <th scope="col">Started</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Error</th>
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
                      <td title={target || undefined}>{shortId(target)}</td>
                      <td>{formatApiDateTime(op.started_at || op.created_at)}</td>
                      <td>{operationElapsed(op)}</td>
                      <td title={op.error_message || undefined}>
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
