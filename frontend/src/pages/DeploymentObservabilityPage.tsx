import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getCapacityProfile,
  getRuntimeHistory,
  getRuntimeLatest,
} from '../api/observability'
import type {
  CapacityProfile,
  RuntimeAnalytics,
  RuntimeSnapshot,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parseBoundedInt } from '../utils/query'

const CAPACITY_SETTINGS = [
  'max_model_len',
  'max_num_seqs',
  'tensor_parallel_size',
  'gpu_memory_utilization',
  'dtype',
  'quantization',
  'scheduling_policy',
] as const

const HISTOGRAM_KEYS = [
  'ttft_seconds',
  'queue_time_seconds',
  'e2e_latency_seconds',
  'prefill_time_seconds',
  'decode_time_seconds',
  'inter_token_latency_seconds',
  'time_per_output_token_seconds',
] as const

function formatNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(digits)
}

function formatRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—'
  return `${ratio.toFixed(3)} (${(ratio * 100).toFixed(1)}%)`
}

function formatCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function settingHint(key: string): string | null {
  if (key === 'max_num_seqs') {
    return 'scheduler/runner sequence capacity (not guaranteed simultaneous users)'
  }
  return null
}

export function DeploymentObservabilityPage() {
  const { deploymentId = '' } = useParams<{ deploymentId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const hours = parseBoundedInt(searchParams.get('hours'), {
    min: 1,
    max: 168,
    defaultValue: 24,
  })
  const limit = parseBoundedInt(searchParams.get('limit'), {
    min: 1,
    max: 1000,
    defaultValue: 500,
  })

  const [latest, setLatest] = useState<RuntimeSnapshot | null>(null)
  const [latestEmpty, setLatestEmpty] = useState(false)
  const [latestLoading, setLatestLoading] = useState(true)
  const [latestError, setLatestError] = useState<string | null>(null)

  const [history, setHistory] = useState<RuntimeSnapshot[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)

  const [profile, setProfile] = useState<CapacityProfile | null>(null)
  const [profileLoading, setProfileLoading] = useState(true)
  const [profileError, setProfileError] = useState<string | null>(null)

  const [notFound, setNotFound] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [draftHours, setDraftHours] = useState(String(hours))
  const [draftLimit, setDraftLimit] = useState(String(limit))

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  /** Generations blocked after an authoritative 404 for that read wave. */
  const goneGenRef = useRef(0)
  const activeIdRef = useRef(deploymentId)
  activeIdRef.current = deploymentId

  useEffect(() => {
    setDraftHours(String(hours))
  }, [hours])
  useEffect(() => {
    setDraftLimit(String(limit))
  }, [limit])

  const syncUrl = useCallback(
    (next: { hours: number; limit: number }) => {
      const params = new URLSearchParams()
      if (next.hours !== 24) params.set('hours', String(next.hours))
      if (next.limit !== 500) params.set('limit', String(next.limit))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawHours = searchParams.get('hours')
    const rawLimit = searchParams.get('limit')
    let needsFix = false
    const fixed = new URLSearchParams()
    const parsedHours = parseBoundedInt(rawHours, {
      min: 1,
      max: 168,
      defaultValue: 24,
    })
    const parsedLimit = parseBoundedInt(rawLimit, {
      min: 1,
      max: 1000,
      defaultValue: 500,
    })
    if (rawHours !== null && String(parsedHours) !== rawHours) needsFix = true
    if (rawLimit !== null && String(parsedLimit) !== rawLimit) needsFix = true
    if (parsedHours !== 24) fixed.set('hours', String(parsedHours))
    if (parsedLimit !== 500) fixed.set('limit', String(parsedLimit))
    if (needsFix) setSearchParams(fixed, { replace: true })
  }, [searchParams, setSearchParams])

  const applyBounds = useCallback(() => {
    const nextHours = parseBoundedInt(draftHours, {
      min: 1,
      max: 168,
      defaultValue: 24,
    })
    const nextLimit = parseBoundedInt(draftLimit, {
      min: 1,
      max: 1000,
      defaultValue: 500,
    })
    setDraftHours(String(nextHours))
    setDraftLimit(String(nextLimit))
    syncUrl({ hours: nextHours, limit: nextLimit })
  }, [draftHours, draftLimit, syncUrl])

  const resetIdentity = useCallback(() => {
    setLatest(null)
    setLatestEmpty(false)
    setLatestError(null)
    setHistory(null)
    setHistoryError(null)
    setProfile(null)
    setProfileError(null)
    setNotFound(false)
    setLastUpdated(null)
  }, [])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const requestId = deploymentId
      if (!requestId) {
        resetIdentity()
        setNotFound(true)
        setLatestLoading(false)
        setHistoryLoading(false)
        setProfileLoading(false)
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current
      const isCurrent = () =>
        gen === readGenRef.current && activeIdRef.current === requestId
      const canApply = () => isCurrent() && goneGenRef.current !== gen

      if (mode === 'initial') {
        resetIdentity()
        setLatestLoading(true)
        setHistoryLoading(true)
        setProfileLoading(true)
      } else {
        setRefreshing(true)
        setNotFound(false)
      }

      const markAuthoritativeGone = () => {
        if (!isCurrent()) return
        goneGenRef.current = gen
        setLatest(null)
        setLatestEmpty(false)
        setLatestError(null)
        setHistory(null)
        setHistoryError(null)
        setProfile(null)
        setProfileError(null)
        setNotFound(true)
        setLastUpdated(null)
        setLatestLoading(false)
        setHistoryLoading(false)
        setProfileLoading(false)
        setRefreshing(false)
        // Stop sibling in-flight work; their success paths are also blocked by goneGen.
        controller.abort()
      }

      const latestPromise = getRuntimeLatest(requestId, controller.signal)
        .then((data) => {
          if (!canApply()) return
          const item = data.items[0] ?? null
          if (item && item.deployment_id !== requestId) return
          setLatest(item)
          setLatestEmpty(!item)
          setLatestError(null)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            markAuthoritativeGone()
            return
          }
          if (!canApply()) return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Runtime latest를 불러오지 못했습니다.'
          setLatestError(message)
        })
        .finally(() => {
          if (canApply()) setLatestLoading(false)
        })

      const historyPromise = getRuntimeHistory(requestId, {
        hours,
        limit,
        signal: controller.signal,
      })
        .then((data) => {
          if (!canApply()) return
          if (data.deployment_id !== requestId) return
          setHistory(data.items)
          setHistoryError(null)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            markAuthoritativeGone()
            return
          }
          if (!canApply()) return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Runtime history를 불러오지 못했습니다.'
          setHistoryError(message)
        })
        .finally(() => {
          if (canApply()) setHistoryLoading(false)
        })

      const profilePromise = getCapacityProfile(
        requestId,
        hours,
        controller.signal,
      )
        .then((data) => {
          if (!canApply()) return
          if (data.deployment.id !== requestId) return
          setProfile(data)
          setProfileError(null)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            markAuthoritativeGone()
            return
          }
          if (!canApply()) return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Capacity Profile을 불러오지 못했습니다.'
          setProfileError(message)
        })
        .finally(() => {
          if (canApply()) setProfileLoading(false)
        })

      await Promise.allSettled([
        latestPromise,
        historyPromise,
        profilePromise,
      ])
      if (canApply()) setRefreshing(false)
    },
    [deploymentId, hours, limit, resetIdentity],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const analytics: RuntimeAnalytics | null = profile?.runtime_analytics ?? null
  const displayLatest =
    latest && latest.deployment_id === deploymentId ? latest : null
  const displayProfile =
    profile && profile.deployment.id === deploymentId ? profile : null
  const displayHistory = history

  if (notFound) {
    return (
      <AppShell
        title="Deployment Observability"
        description="Runtime / Capacity Profile"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/observability">
          ← Observability
        </Link>
        <SectionError
          title="Deployment를 찾을 수 없습니다."
          message="요청한 Deployment의 runtime observability 데이터가 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  return (
    <AppShell
      title={
        displayProfile
          ? `Runtime ${displayProfile.deployment.name}`
          : displayLatest?.deployment_name
            ? `Runtime ${displayLatest.deployment_name}`
            : 'Deployment Observability'
      }
      description="Persisted DB snapshot 기반 Runtime latest/history와 Capacity Profile입니다. `/analytics`는 Capacity Profile의 embedded runtime_analytics로 재사용하며 별도 호출하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <Link className="back-link" to="/observability">
        ← Observability
      </Link>
      <p className="panel-hint">
        Deployment:{' '}
        <Link className="table-link mono" to={`/deployments/${deploymentId}`}>
          {shortId(deploymentId, 12)}
        </Link>
      </p>

      <form
        className="toolbar toolbar--wrap"
        onSubmit={(e) => {
          e.preventDefault()
          applyBounds()
        }}
      >
        <label className="toolbar__field" htmlFor="dep-obs-hours">
          <span>Hours (1–168)</span>
          <input
            id="dep-obs-hours"
            type="number"
            inputMode="numeric"
            min={1}
            max={168}
            step={1}
            value={draftHours}
            onChange={(e) => setDraftHours(e.target.value)}
            onBlur={applyBounds}
          />
        </label>
        <label className="toolbar__field" htmlFor="dep-obs-limit">
          <span>History limit (1–1000)</span>
          <input
            id="dep-obs-limit"
            type="number"
            inputMode="numeric"
            min={1}
            max={1000}
            step={1}
            value={draftLimit}
            onChange={(e) => setDraftLimit(e.target.value)}
            onBlur={applyBounds}
          />
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn">
            적용
          </button>
        </div>
        <p className="toolbar__hint">
          hours/limit는 Backend 계약 범위의 임의의 정수입니다. 잘못된 값은
          기본값(hours=24, limit=500)으로 정규화됩니다.
        </p>
      </form>

      <section className="detail-panel" aria-labelledby="latest-heading">
        <h2 id="latest-heading">Latest runtime snapshot</h2>
        <p className="panel-hint">
          Persisted snapshot only — live scrape가 아닙니다.
        </p>
        {latestError ? (
          <SectionError
            title={
              displayLatest
                ? 'Runtime latest 새로고침 실패'
                : 'Runtime latest 오류'
            }
            message={
              displayLatest
                ? `기존 latest snapshot을 표시하고 있습니다. 새로고침 실패: ${latestError}`
                : latestError
            }
          />
        ) : null}
        {latestLoading && !displayLatest && !latestEmpty ? (
          <LoadingBlock label="Runtime latest를 불러오는 중…" />
        ) : null}
        {displayLatest ? (
          <dl className="meta-grid">
            <div>
              <dt>Sampled</dt>
              <dd>{formatApiDateTime(displayLatest.sampled_at)}</dd>
            </div>
            <div>
              <dt>Availability</dt>
              <dd>
                <StatusBadge status={displayLatest.availability} />
              </dd>
            </div>
            <div>
              <dt>KV cache ratio</dt>
              <dd>{formatRatio(displayLatest.kv_cache_usage_ratio)}</dd>
            </div>
            <div>
              <dt>Running</dt>
              <dd>{formatNum(displayLatest.num_requests_running, 0)}</dd>
            </div>
            <div>
              <dt>Waiting</dt>
              <dd>{formatNum(displayLatest.num_requests_waiting, 0)}</dd>
            </div>
            <div>
              <dt>Prompt tokens (cumulative)</dt>
              <dd>{formatNum(displayLatest.prompt_tokens_total, 0)}</dd>
            </div>
            <div>
              <dt>Generation tokens (cumulative)</dt>
              <dd>{formatNum(displayLatest.generation_tokens_total, 0)}</dd>
            </div>
            <div>
              <dt>Runtime instance</dt>
              <dd className="mono">
                {displayLatest.runtime_instance
                  ? [
                      displayLatest.runtime_instance.container_id || '—',
                      displayLatest.runtime_instance.started_at || '—',
                      `restarts=${displayLatest.runtime_instance.restart_count ?? '—'}`,
                    ].join(' · ')
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>Missing metrics</dt>
              <dd>
                {displayLatest.missing_metrics?.length
                  ? displayLatest.missing_metrics.join(', ')
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>Error</dt>
              <dd>
                {displayLatest.error_code || displayLatest.error_message
                  ? [displayLatest.error_code, displayLatest.error_message]
                      .filter(Boolean)
                      .join(': ')
                  : '—'}
              </dd>
            </div>
          </dl>
        ) : null}
        {latestEmpty && !latestLoading ? (
          <p className="empty-state" role="status">
            이 Deployment의 persisted runtime snapshot이 아직 없습니다.
          </p>
        ) : null}
      </section>

      <section className="detail-panel" aria-labelledby="history-heading">
        <h2 id="history-heading">History (oldest → newest)</h2>
        {historyError ? (
          <SectionError
            title={
              displayHistory
                ? 'Runtime history 새로고침 실패'
                : 'Runtime history 오류'
            }
            message={
              displayHistory
                ? `기존 history를 표시하고 있습니다. 새로고침 실패: ${historyError}`
                : historyError
            }
          />
        ) : null}
        {historyLoading && !displayHistory ? (
          <LoadingBlock label="Runtime history를 불러오는 중…" />
        ) : null}
        {displayHistory && displayHistory.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Sampled</th>
                  <th scope="col">Availability</th>
                  <th scope="col">KV</th>
                  <th scope="col">Running</th>
                  <th scope="col">Waiting</th>
                  <th scope="col">Prompt Σ</th>
                  <th scope="col">Gen Σ</th>
                  <th scope="col">Instance</th>
                  <th scope="col">Error</th>
                </tr>
              </thead>
              <tbody>
                {displayHistory.map((row, idx) => (
                  <tr key={`${row.sampled_at ?? 'null'}-${idx}`}>
                    <td>{formatApiDateTime(row.sampled_at)}</td>
                    <td>
                      <StatusBadge status={row.availability} />
                    </td>
                    <td>{formatRatio(row.kv_cache_usage_ratio)}</td>
                    <td>{formatNum(row.num_requests_running, 0)}</td>
                    <td>{formatNum(row.num_requests_waiting, 0)}</td>
                    <td>{formatNum(row.prompt_tokens_total, 0)}</td>
                    <td>{formatNum(row.generation_tokens_total, 0)}</td>
                    <td className="mono">
                      {row.runtime_instance?.container_id
                        ? shortId(row.runtime_instance.container_id, 10)
                        : '—'}
                    </td>
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
        {displayHistory && displayHistory.length === 0 ? (
          <p className="empty-state" role="status">
            선택 구간에 history snapshot이 없습니다.
          </p>
        ) : null}
      </section>

      <section className="detail-panel" aria-labelledby="capacity-heading">
        <h2 id="capacity-heading">Capacity Profile</h2>
        <p className="panel-hint">
          requested ModelOps config vs observed_explicit Managed vLLM argv입니다.
          런타임 기본값을 합성하지 않으며, GPU VRAM은 GPU별로만 표시합니다.
        </p>
        {profileError ? (
          <SectionError
            title={
              displayProfile
                ? 'Capacity Profile 새로고침 실패'
                : 'Capacity Profile 오류'
            }
            message={
              displayProfile
                ? `기존 Capacity Profile을 표시하고 있습니다. 새로고침 실패: ${profileError}`
                : profileError
            }
          />
        ) : null}
        {profileLoading && !displayProfile ? (
          <LoadingBlock label="Capacity Profile을 불러오는 중…" />
        ) : null}
        {displayProfile ? (
          <>
            <dl className="meta-grid">
              <div>
                <dt>Deployment</dt>
                <dd>
                  <Link
                    className="table-link"
                    to={`/deployments/${displayProfile.deployment.id}`}
                  >
                    {displayProfile.deployment.name}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Type / Runtime / Health</dt>
                <dd>
                  {displayProfile.deployment.deployment_type} ·{' '}
                  <StatusBadge
                    status={displayProfile.deployment.runtime_status}
                  />{' '}
                  ·{' '}
                  <StatusBadge
                    status={displayProfile.deployment.health_status}
                  />
                </dd>
              </div>
              <div>
                <dt>Model Version</dt>
                <dd>
                  {displayProfile.model.model_name} /{' '}
                  {displayProfile.model.version_label} (
                  {displayProfile.model.runtime_type})
                </dd>
              </div>
              <div>
                <dt>Observation sampled</dt>
                <dd>
                  {formatApiDateTime(
                    displayProfile.configuration.runtime_observation_sampled_at,
                  )}
                </dd>
              </div>
            </dl>

            <h3 className="section-subheading">Capacity settings</h3>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Requested</th>
                    <th scope="col">Source</th>
                    <th scope="col">Observed explicit</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {CAPACITY_SETTINGS.map((key) => {
                    const row = displayProfile.configuration.settings[key]
                    return (
                      <tr key={key}>
                        <td>
                          <span className="mono">{key}</span>
                          {settingHint(key) ? (
                            <div className="toolbar__hint">{settingHint(key)}</div>
                          ) : null}
                        </td>
                        <td>{formatCell(row?.requested)}</td>
                        <td className="mono">
                          {row?.requested_source || '—'}
                        </td>
                        <td>{formatCell(row?.observed_explicit)}</td>
                        <td>
                          <StatusBadge status={row?.comparison_status} />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            <h3 className="section-subheading">GPU assignments (per GPU)</h3>
            <p className="panel-hint">
              VRAM은 GPU별로만 표시합니다. 합산 pooled capacity로 해석하지
              마세요.
            </p>
            {displayProfile.gpu_assignments.length > 0 ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Order</th>
                      <th scope="col">Index</th>
                      <th scope="col">Model</th>
                      <th scope="col">VRAM total (MB)</th>
                      <th scope="col">Safety margin (MB)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...displayProfile.gpu_assignments]
                      .sort((a, b) => a.device_order - b.device_order)
                      .map((gpu) => (
                        <tr key={`${gpu.device_order}-${gpu.device_index}`}>
                          <td>{gpu.device_order}</td>
                          <td>{formatCell(gpu.device_index)}</td>
                          <td>{gpu.model_name || '—'}</td>
                          <td>{formatCell(gpu.vram_total_mb)}</td>
                          <td>{formatCell(gpu.safety_margin_mb)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty-state" role="status">
                GPU assignment가 없습니다.
              </p>
            )}

            <h3 className="section-subheading">
              Invocation demand (same window)
            </h3>
            <dl className="meta-grid">
              <div>
                <dt>Requests / Success / Error</dt>
                <dd>
                  {displayProfile.invocations.request_count} /{' '}
                  {displayProfile.invocations.success_count} /{' '}
                  {displayProfile.invocations.error_count}
                </dd>
              </div>
              <div>
                <dt>Token coverage</dt>
                <dd>
                  {displayProfile.invocations.request_count > 0
                    ? `${(
                        (displayProfile.invocations.tokenized_request_count /
                          displayProfile.invocations.request_count) *
                        100
                      ).toFixed(1)}%`
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>Input p50 / p95 / max</dt>
                <dd>
                  {formatNum(displayProfile.invocations.input_tokens_p50, 0)} /{' '}
                  {formatNum(displayProfile.invocations.input_tokens_p95, 0)} /{' '}
                  {formatNum(displayProfile.invocations.input_tokens_max, 0)}
                </dd>
              </div>
              <div>
                <dt>Latency p50 / p95 / max (ms)</dt>
                <dd>
                  {formatNum(displayProfile.invocations.latency_ms_p50, 1)} /{' '}
                  {formatNum(displayProfile.invocations.latency_ms_p95, 1)} /{' '}
                  {formatNum(displayProfile.invocations.latency_ms_max, 1)}
                </dd>
              </div>
            </dl>

            <h3 className="section-subheading">
              Runtime analytics (embedded, classic histogram estimates)
            </h3>
            <p className="panel-hint">
              P50/P95는 classic histogram bucket estimate이며 exact request
              percentile가 아닙니다. reset/unknown-identity interval은 delta에서
              제외됩니다.
            </p>
            {analytics ? (
              <>
                <dl className="meta-grid">
                  <div>
                    <dt>Snapshots / intervals</dt>
                    <dd>
                      {analytics.snapshot_count} / {analytics.interval_count}
                    </dd>
                  </div>
                  <div>
                    <dt>Reset boundaries</dt>
                    <dd>{analytics.boundaries.reset_boundary_count}</dd>
                  </div>
                  <div>
                    <dt>Unknown-identity intervals</dt>
                    <dd>
                      {analytics.boundaries.identity_unknown_interval_count}
                    </dd>
                  </div>
                </dl>
                <h4 className="section-subheading">Gauges (avg / max)</h4>
                <dl className="meta-grid">
                  <div>
                    <dt>KV cache</dt>
                    <dd>
                      {analytics.gauges?.kv_cache_usage_ratio
                        ? `${formatRatio(analytics.gauges.kv_cache_usage_ratio.avg)} / ${formatRatio(analytics.gauges.kv_cache_usage_ratio.max)}`
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Running</dt>
                    <dd>
                      {analytics.gauges?.num_requests_running
                        ? `${formatNum(analytics.gauges.num_requests_running.avg, 2)} / ${formatNum(analytics.gauges.num_requests_running.max, 0)}`
                        : '—'}
                    </dd>
                  </div>
                  <div>
                    <dt>Waiting</dt>
                    <dd>
                      {analytics.gauges?.num_requests_waiting
                        ? `${formatNum(analytics.gauges.num_requests_waiting.avg, 2)} / ${formatNum(analytics.gauges.num_requests_waiting.max, 0)}`
                        : '—'}
                    </dd>
                  </div>
                </dl>
                <h4 className="section-subheading">Token deltas / rates</h4>
                <dl className="meta-grid">
                  {(['prompt_tokens', 'generation_tokens'] as const).map(
                    (key) => {
                      const tok = analytics.tokens?.[key]
                      return (
                        <div key={key}>
                          <dt>{key}</dt>
                          <dd>
                            {tok
                              ? `Δ ${formatNum(tok.delta, 0)} · ${formatNum(tok.observed_tokens_per_second, 2)} tok/s · regressions ${tok.counter_regression_interval_count}`
                              : '—'}
                          </dd>
                        </div>
                      )
                    },
                  )}
                </dl>
                <h4 className="section-subheading">
                  Histograms (mean / P50 / P95 bucket estimates)
                </h4>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th scope="col">Metric</th>
                        <th scope="col">Mean (s)</th>
                        <th scope="col">P50 est. (s)</th>
                        <th scope="col">P95 est. (s)</th>
                        <th scope="col">Obs</th>
                        <th scope="col">Regressions</th>
                        <th scope="col">Bucket schema Δ</th>
                      </tr>
                    </thead>
                    <tbody>
                      {HISTOGRAM_KEYS.map((key) => {
                        const h = analytics.histograms?.[key]
                        if (!h) return null
                        return (
                          <tr key={key}>
                            <td className="mono">{key}</td>
                            <td>{formatNum(h.mean_seconds, 4)}</td>
                            <td>{formatNum(h.p50_seconds, 4)}</td>
                            <td>{formatNum(h.p95_seconds, 4)}</td>
                            <td>{h.observation_count}</td>
                            <td>{h.histogram_regression_interval_count}</td>
                            <td>{h.bucket_schema_change_interval_count}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <p className="empty-state" role="status">
                Embedded runtime analytics가 없습니다.
              </p>
            )}
          </>
        ) : null}
      </section>
    </AppShell>
  )
}
