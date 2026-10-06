import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getNode,
  getNodeResources,
  refreshNodeResources,
} from '../api/nodes'
import type { NodeDetail, NodeResourcesLatest } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { UsageBar } from '../components/UsageBar'
import { formatApiDateTime } from '../utils/date'
import {
  formatMemoryMb,
  formatPowerW,
  formatTemperatureC,
  formatUtilizationPct,
  usagePercent,
} from '../utils/number'

function memoryPair(
  used: number | null | undefined,
  total: number | null | undefined,
): string {
  return `${formatMemoryMb(used)} / ${formatMemoryMb(total)}`
}

export function NodeDetailPage() {
  const { nodeId = '' } = useParams<{ nodeId: string }>()

  const [node, setNode] = useState<NodeDetail | null>(null)
  const [resources, setResources] = useState<NodeResourcesLatest | null>(null)
  const [nodeLoading, setNodeLoading] = useState(true)
  const [resourceLoading, setResourceLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [mutating, setMutating] = useState(false)
  const [nodeError, setNodeError] = useState<string | null>(null)
  const [nodeNotFound, setNodeNotFound] = useState(false)
  const [resourceError, setResourceError] = useState<string | null>(null)
  const [metadataWarning, setMetadataWarning] = useState<string | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const mutateLockRef = useRef(false)

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!nodeId) {
        setNodeNotFound(true)
        setNodeLoading(false)
        setResourceLoading(false)
        setNodeError('Node를 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      const isInitial = mode === 'initial'
      if (isInitial) {
        setNodeLoading(true)
        setResourceLoading(true)
      } else {
        setRefreshing(true)
      }
      setNodeError(null)
      setResourceError(null)
      setMetadataWarning(null)
      setRefreshError(null)
      setNodeNotFound(false)

      const nodePromise = getNode(nodeId, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return
          setNode(data)
          setNodeNotFound(false)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            setNodeNotFound(true)
            setNodeError('Node를 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Node 메타데이터를 불러오지 못했습니다.'
          setNodeError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted) setNodeLoading(false)
        })

      const resourcePromise = getNodeResources(nodeId, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return
          setResources(data)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            // Node 404 is owned by metadata section.
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : '자원 스냅샷을 불러오지 못했습니다.'
          setResourceError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted) setResourceLoading(false)
        })

      await Promise.all([nodePromise, resourcePromise])
      if (!controller.signal.aborted) {
        setRefreshing(false)
      }
    },
    [nodeId],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onResourceRefresh = async () => {
    if (mutateLockRef.current || !nodeId) return
    mutateLockRef.current = true
    setMutating(true)
    setRefreshError(null)
    setMetadataWarning(null)

    try {
      const latest = await refreshNodeResources(nodeId)
      setResources(latest)
      setLastUpdated(new Date())
      setResourceError(null)

      try {
        const meta = await getNode(nodeId)
        setNode(meta)
        setNodeNotFound(false)
        setNodeError(null)
        setLastUpdated(new Date())
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Node 메타데이터를 다시 불러오지 못했습니다.'
        setMetadataWarning(
          `자원 수집은 성공했지만 Node 메타데이터를 다시 읽지 못했습니다: ${message}`,
        )
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : '리소스 갱신에 실패했습니다.'
      setRefreshError(message)
    } finally {
      mutateLockRef.current = false
      setMutating(false)
    }
  }

  if (nodeNotFound && !node) {
    return (
      <AppShell
        title="Nodes / GPUs"
        description="등록된 serving host 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/nodes">
          ← Nodes / GPUs
        </Link>
        <SectionError
          title="Node를 찾을 수 없습니다."
          message="요청한 Node가 없거나 삭제되었습니다."
        />
        <p>
          <Link className="table-link" to="/nodes">
            Node 목록으로 돌아가기
          </Link>
        </p>
      </AppShell>
    )
  }

  const host = resources?.host ?? null
  const gpuItems = resources?.gpus ?? []

  return (
    <AppShell
      title={node?.name ?? 'Node 상세'}
      description="Host/GPU 스냅샷은 DB에 저장된 최근 수집 값입니다. 실시간 수집은 ‘리소스 갱신’으로만 실행합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={mutating}
    >
      <Link className="back-link" to="/nodes">
        ← Nodes / GPUs
      </Link>

      {nodeLoading && !node ? (
        <LoadingBlock label="Node 메타데이터를 불러오는 중…" />
      ) : null}

      {nodeError && !nodeNotFound ? (
        <SectionError title="Node 메타데이터 오류" message={nodeError} />
      ) : null}

      {node ? (
        <section className="detail-panel" aria-labelledby="node-meta-heading">
          <div className="detail-panel__header">
            <h2 id="node-meta-heading">{node.name}</h2>
            <StatusBadge status={node.status} />
          </div>
          <dl className="meta-grid">
            <div>
              <dt>Hostname</dt>
              <dd>{node.hostname || '—'}</dd>
            </div>
            <div>
              <dt>Environment</dt>
              <dd>{node.environment || '—'}</dd>
            </div>
            <div>
              <dt>Region</dt>
              <dd>{node.region || '—'}</dd>
            </div>
            <div>
              <dt>Last Heartbeat</dt>
              <dd>{formatApiDateTime(node.last_heartbeat_at)}</dd>
            </div>
            <div>
              <dt>CPU Model</dt>
              <dd>{node.cpu_model || '—'}</dd>
            </div>
            <div>
              <dt>RAM Total</dt>
              <dd>{formatMemoryMb(node.ram_total_mb)}</dd>
            </div>
            <div>
              <dt>Disk Total</dt>
              <dd>{formatMemoryMb(node.disk_total_mb)}</dd>
            </div>
            <div>
              <dt>Agent Base URL</dt>
              <dd className="mono">{node.agent_base_url || '—'}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatApiDateTime(node.created_at)}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{formatApiDateTime(node.updated_at)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      <section className="detail-panel" aria-labelledby="resource-heading">
        <div className="detail-panel__header">
          <h2 id="resource-heading">Host / GPU 자원</h2>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void onResourceRefresh()}
            disabled={mutating || !nodeId}
            aria-busy={mutating}
          >
            {mutating ? '갱신 중…' : '리소스 갱신'}
          </button>
        </div>
        <p className="panel-hint">
          <strong>새로고침</strong>은 DB 재조회입니다.{' '}
          <strong>리소스 갱신</strong>은 Management API를 통해 Node Agent에서
          Host/GPU 스냅샷을 수집합니다. GPU별 free VRAM은 합산하지 않습니다.
        </p>

        {metadataWarning ? (
          <SectionError title="메타데이터 경고" message={metadataWarning} />
        ) : null}
        {refreshError ? (
          <SectionError title="리소스 갱신 실패" message={refreshError} />
        ) : null}
        {resourceError ? (
          <SectionError title="자원 스냅샷 오류" message={resourceError} />
        ) : null}

        {resourceLoading && !resources ? (
          <LoadingBlock label="자원 스냅샷을 불러오는 중…" />
        ) : null}

        {resources && !host ? (
          <p className="empty-state" role="status">
            아직 수집된 Host 자원 정보가 없습니다. 리소스 갱신을 실행하면 Node
            Agent에서 최신 상태를 수집합니다.
          </p>
        ) : null}

        {host ? (
          <div className="host-metrics">
            <p className="sampled-at">
              최근 수집 시각 {formatApiDateTime(host.sampled_at)}
            </p>
            <UsageBar
              label="CPU Utilization"
              displayValue={formatUtilizationPct(host.cpu_utilization_pct)}
              percentage={
                host.cpu_utilization_pct !== null &&
                Number.isFinite(host.cpu_utilization_pct)
                  ? Math.min(100, Math.max(0, host.cpu_utilization_pct))
                  : null
              }
            />
            <UsageBar
              label="RAM Used / Total"
              displayValue={memoryPair(host.ram_used_mb, host.ram_total_mb)}
              percentage={usagePercent(host.ram_used_mb, host.ram_total_mb)}
            />
            <p className="metric-line">
              RAM Free: {formatMemoryMb(host.ram_free_mb)}
            </p>
            <UsageBar
              label="Disk Used / Total"
              displayValue={memoryPair(host.disk_used_mb, host.disk_total_mb)}
              percentage={usagePercent(host.disk_used_mb, host.disk_total_mb)}
            />
            <p className="metric-line">
              Disk Free: {formatMemoryMb(host.disk_free_mb)}
            </p>
          </div>
        ) : null}

        <h3 className="section-subheading">GPUs</h3>
        {resources && gpuItems.length === 0 ? (
          <p className="empty-state" role="status">
            등록된 GPU가 없습니다.
          </p>
        ) : null}

        <div className="gpu-grid">
          {gpuItems.map(({ gpu, snapshot }) => (
            <article
              key={gpu.id}
              className="gpu-card"
              aria-label={`GPU ${gpu.device_index}`}
            >
              <header className="gpu-card__header">
                <h4>
                  GPU {gpu.device_index} · {gpu.model_name}
                </h4>
                <StatusBadge status={gpu.status} />
              </header>
              <dl className="meta-grid meta-grid--compact">
                <div>
                  <dt>GPU UUID</dt>
                  <dd className="mono">{gpu.gpu_uuid}</dd>
                </div>
                <div>
                  <dt>Compute capability</dt>
                  <dd>{gpu.compute_capability || '—'}</dd>
                </div>
                <div>
                  <dt>Last seen</dt>
                  <dd>{formatApiDateTime(gpu.last_seen_at)}</dd>
                </div>
                <div>
                  <dt>Safety Margin</dt>
                  <dd>{formatMemoryMb(gpu.safety_margin_mb)}</dd>
                </div>
              </dl>

              {!snapshot ? (
                <p className="empty-state" role="status">
                  아직 수집된 GPU 자원 정보가 없습니다.
                </p>
              ) : (
                <>
                  <UsageBar
                    label="VRAM Used / Total"
                    displayValue={memoryPair(
                      snapshot.vram_used_mb,
                      snapshot.vram_total_mb,
                    )}
                    percentage={usagePercent(
                      snapshot.vram_used_mb,
                      snapshot.vram_total_mb,
                    )}
                  />
                  <p className="metric-line">
                    VRAM Free: {formatMemoryMb(snapshot.vram_free_mb)}
                  </p>
                  <p className="metric-line">
                    GPU Utilization:{' '}
                    {formatUtilizationPct(snapshot.gpu_utilization_pct)}
                  </p>
                  <p className="metric-line">
                    Memory Utilization:{' '}
                    {formatUtilizationPct(snapshot.memory_utilization_pct)}
                  </p>
                  <p className="metric-line">
                    Temperature: {formatTemperatureC(snapshot.temperature_c)}
                  </p>
                  <p className="metric-line">
                    Power: {formatPowerW(snapshot.power_w)}
                  </p>
                  <p className="sampled-at">
                    Snapshot Sampled At{' '}
                    {formatApiDateTime(snapshot.sampled_at)}
                  </p>
                </>
              )}
            </article>
          ))}
        </div>
      </section>
    </AppShell>
  )
}
