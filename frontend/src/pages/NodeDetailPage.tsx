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
import { HelpHint, TermLabel } from '../components/HelpHint'
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
import { NODE_FIELD_HELP } from '../utils/uiLabels'

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
  const readLockRef = useRef(false)

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!nodeId) {
        setNodeNotFound(true)
        setNodeLoading(false)
        setResourceLoading(false)
        setNodeError('서버를 찾을 수 없습니다.')
        return
      }
      // DB reads and Node Agent resource mutation are mutually exclusive.
      if (mutateLockRef.current) return

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      readLockRef.current = true

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

      try {
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
              setNodeError('서버를 찾을 수 없습니다.')
              return
            }
            const message =
              err instanceof ApiError
                ? err.message
                : err instanceof Error
                  ? err.message
                  : '서버 메타데이터를 불러오지 못했습니다.'
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
      } finally {
        // Only the active read generation clears the lock (aborted loads keep it).
        if (abortRef.current === controller) {
          readLockRef.current = false
        }
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

  const resourceActionDisabled =
    mutating ||
    nodeLoading ||
    resourceLoading ||
    refreshing ||
    !nodeId

  const onResourceRefresh = async () => {
    if (
      mutateLockRef.current ||
      readLockRef.current ||
      nodeLoading ||
      resourceLoading ||
      refreshing ||
      !nodeId
    ) {
      return
    }
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
              : '서버 메타데이터를 다시 불러오지 못했습니다.'
        setMetadataWarning(
          `자원 수집은 성공했지만 서버 메타데이터를 다시 읽지 못했습니다: ${message}`,
        )
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : '리소스 갱신(수집)에 실패했습니다.'
      setRefreshError(message)
    } finally {
      mutateLockRef.current = false
      setMutating(false)
    }
  }

  if (nodeNotFound && !node) {
    return (
      <AppShell
        title="서버 및 GPU"
        description="등록된 GPU 서버 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/nodes">
          ← 서버 및 GPU
        </Link>
        <SectionError
          title="서버를 찾을 수 없습니다."
          message="요청한 서버(Node)가 없거나 삭제되었습니다."
        />
        <p>
          <Link className="table-link" to="/nodes">
            서버 목록으로 돌아가기
          </Link>
        </p>
      </AppShell>
    )
  }

  const host = resources?.host ?? null
  const gpuItems = resources?.gpus ?? []

  return (
    <AppShell
      title={node?.name ?? '서버 상세'}
      description="서버/GPU 스냅샷은 DB에 저장된 최근 수집 값입니다. 실시간 수집은 「리소스 갱신」으로만 실행합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
      refreshDisabled={mutating}
    >
      <Link className="back-link" to="/nodes">
        ← 서버 및 GPU
      </Link>

      <p className="panel-hint" role="note">
        {NODE_FIELD_HELP.refreshVsCollect} 화면 상단 「마지막 갱신」은 DB 재조회
        시각이고, 아래 「자원 측정 시각」은 스냅샷 수집 시각입니다.
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="서버 상태">
            {NODE_FIELD_HELP.status}
          </HelpHint>
          <HelpHint showTopic label="Heartbeat vs 자원 측정">
            {NODE_FIELD_HELP.heartbeat}
          </HelpHint>
          <HelpHint showTopic label="RAM vs VRAM">
            {NODE_FIELD_HELP.ramVsVram}
          </HelpHint>
          <HelpHint showTopic label="GPU 사용률 지표">
            {NODE_FIELD_HELP.gpuUtilVsVram}
          </HelpHint>
          <HelpHint showTopic label="VRAM Free / Safety Margin">
            {NODE_FIELD_HELP.vramFree} {NODE_FIELD_HELP.safetyMargin}
          </HelpHint>
          <HelpHint showTopic label="GPU별 VRAM">
            {NODE_FIELD_HELP.noPoolVram}
          </HelpHint>
          <HelpHint showTopic label="배포 Fit">
            {NODE_FIELD_HELP.fitReminder}
          </HelpHint>
          <HelpHint showTopic label="미수집">
            {NODE_FIELD_HELP.uncollected}
          </HelpHint>
        </div>
      </details>

      {nodeLoading && !node ? (
        <LoadingBlock label="서버 메타데이터를 불러오는 중…" />
      ) : null}

      {nodeError && !nodeNotFound ? (
        <SectionError title="서버 메타데이터 오류" message={nodeError} />
      ) : null}

      {node ? (
        <section className="detail-panel" aria-labelledby="node-meta-heading">
          <div className="detail-panel__header">
            <h2 id="node-meta-heading">{node.name}</h2>
            <StatusBadge status={node.status} title={NODE_FIELD_HELP.status} />
          </div>
          <dl className="meta-grid">
            <div>
              <dt>
                <TermLabel text="서버 호스트명" apiTerm="Hostname" />
              </dt>
              <dd>{node.hostname || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="운영 환경" apiTerm="Environment" />
              </dt>
              <dd>{node.environment || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="위치/리전" apiTerm="Region" />
              </dt>
              <dd>{node.region || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="마지막 연결 확인"
                  apiTerm="Last Heartbeat"
                  help={NODE_FIELD_HELP.heartbeat}
                />
              </dt>
              <dd>{formatApiDateTime(node.last_heartbeat_at)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="CPU 모델" apiTerm="CPU Model" />
              </dt>
              <dd>{node.cpu_model || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="시스템 메모리 전체"
                  apiTerm="RAM Total"
                  help={NODE_FIELD_HELP.ramVsVram}
                />
              </dt>
              <dd>{formatMemoryMb(node.ram_total_mb)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="디스크 전체" apiTerm="Disk Total" />
              </dt>
              <dd>{formatMemoryMb(node.disk_total_mb)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="Agent 주소" apiTerm="Agent Base URL" />
              </dt>
              <dd className="mono">{node.agent_base_url || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="생성 시각" apiTerm="Created" />
              </dt>
              <dd>{formatApiDateTime(node.created_at)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="수정 시각" apiTerm="Updated" />
              </dt>
              <dd>{formatApiDateTime(node.updated_at)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      <section className="detail-panel" aria-labelledby="resource-heading">
        <div className="detail-panel__header">
          <h2 id="resource-heading">서버 및 GPU 자원</h2>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void onResourceRefresh()}
            disabled={resourceActionDisabled}
            aria-busy={mutating}
            title={
              mutating
                ? '수집 진행 중 — 중복 요청이 차단됩니다.'
                : NODE_FIELD_HELP.refreshVsCollect
            }
          >
            {mutating ? '수집 중…' : '리소스 갱신'}
          </button>
        </div>
        <p className="panel-hint" role="note">
          <strong>새로고침</strong>(상단)은 Management API DB 재조회입니다.{' '}
          <strong>리소스 갱신</strong>은 Node Agent로 새 Host/GPU 스냅샷을
          수집합니다. 수집 중에는 버튼이 비활성화되어 중복 요청을 막습니다.
          수집 성공과 메타데이터 재조회 실패는 따로 안내됩니다.
        </p>

        {metadataWarning ? (
          <SectionError
            title="메타데이터 재조회 실패 (수집은 성공)"
            message={metadataWarning}
          />
        ) : null}
        {refreshError ? (
          <SectionError title="리소스 수집 실패" message={refreshError} />
        ) : null}
        {resourceError ? (
          <SectionError
            title="자원 스냅샷 조회 오류"
            message={`DB에 저장된 스냅샷을 읽지 못했습니다(수집 실패와 다를 수 있음): ${resourceError}`}
          />
        ) : null}

        {resourceLoading && !resources ? (
          <LoadingBlock label="자원 스냅샷을 불러오는 중…" />
        ) : null}

        {resources && !host ? (
          <p className="empty-state" role="status">
            Host 자원이 미수집입니다. 값을 0으로 표시하지 않습니다. 「리소스
            갱신」으로 Node Agent에서 최신 상태를 수집하세요.
          </p>
        ) : null}

        {host ? (
          <div className="host-metrics">
            <p className="sampled-at">
              자원 측정 시각 (Snapshot Sampled At){' '}
              {formatApiDateTime(host.sampled_at)}
            </p>
            <UsageBar
              label="CPU 사용률 (CPU Utilization)"
              displayValue={formatUtilizationPct(host.cpu_utilization_pct)}
              percentage={
                host.cpu_utilization_pct !== null &&
                Number.isFinite(host.cpu_utilization_pct)
                  ? Math.min(100, Math.max(0, host.cpu_utilization_pct))
                  : null
              }
            />
            <UsageBar
              label="시스템 메모리 사용량 / 전체 (RAM Used / Total)"
              displayValue={memoryPair(host.ram_used_mb, host.ram_total_mb)}
              percentage={usagePercent(host.ram_used_mb, host.ram_total_mb)}
            />
            <p className="metric-line">
              남은 시스템 메모리 (RAM Free):{' '}
              {formatMemoryMb(host.ram_free_mb)}
            </p>
            <UsageBar
              label="디스크 사용량 / 전체 (Disk Used / Total)"
              displayValue={memoryPair(host.disk_used_mb, host.disk_total_mb)}
              percentage={usagePercent(host.disk_used_mb, host.disk_total_mb)}
            />
            <p className="metric-line">
              남은 디스크 공간 (Disk Free):{' '}
              {formatMemoryMb(host.disk_free_mb)}
            </p>
          </div>
        ) : null}

        <h3 className="section-subheading">GPU</h3>
        <p className="panel-hint" role="note">
          {NODE_FIELD_HELP.gpuUtilVsVram} {NODE_FIELD_HELP.noPoolVram}{' '}
          {NODE_FIELD_HELP.fitReminder}
        </p>
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
                  <dt>
                    <TermLabel text="GPU UUID" apiTerm="GPU UUID" />
                  </dt>
                  <dd className="mono">{gpu.gpu_uuid}</dd>
                </div>
                <div>
                  <dt>
                    <TermLabel
                      text="GPU 연산 지원 버전"
                      apiTerm="Compute Capability"
                    />
                  </dt>
                  <dd>{gpu.compute_capability || '—'}</dd>
                </div>
                <div>
                  <dt>
                    <TermLabel text="마지막 확인" apiTerm="Last Seen" />
                  </dt>
                  <dd>{formatApiDateTime(gpu.last_seen_at)}</dd>
                </div>
                <div>
                  <dt>
                    <TermLabel
                      text="GPU 안전 여유"
                      apiTerm="Safety Margin"
                      help={NODE_FIELD_HELP.safetyMargin}
                    />
                  </dt>
                  <dd>{formatMemoryMb(gpu.safety_margin_mb)}</dd>
                </div>
              </dl>

              {!snapshot ? (
                <p className="empty-state" role="status">
                  GPU 자원이 미수집입니다. 0으로 대체하지 않습니다. 「리소스
                  갱신」으로 수집하세요.
                </p>
              ) : (
                <>
                  <UsageBar
                    label="GPU 메모리 사용량 / 전체 (VRAM Used / Total)"
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
                    남은 GPU 메모리 (VRAM Free):{' '}
                    {formatMemoryMb(snapshot.vram_free_mb)}
                  </p>
                  <p className="metric-line">
                    GPU 연산 사용률 (GPU Utilization):{' '}
                    {formatUtilizationPct(snapshot.gpu_utilization_pct)}
                  </p>
                  <p className="metric-line">
                    GPU 메모리 컨트롤러 사용률 (Memory Utilization):{' '}
                    {formatUtilizationPct(snapshot.memory_utilization_pct)}
                  </p>
                  <p className="metric-line">
                    GPU 온도 (Temperature):{' '}
                    {formatTemperatureC(snapshot.temperature_c)}
                  </p>
                  <p className="metric-line">
                    GPU 소비 전력 (Power): {formatPowerW(snapshot.power_w)}
                  </p>
                  <p className="sampled-at">
                    자원 측정 시각 (Snapshot Sampled At){' '}
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
