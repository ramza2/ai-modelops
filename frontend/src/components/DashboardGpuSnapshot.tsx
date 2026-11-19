import { Link } from 'react-router-dom'
import type { DashboardGpuNode } from '../api/dashboard'
import { formatApiDateTime } from '../utils/date'
import {
  formatMemoryMb,
  formatUtilizationPct,
  usagePercent,
} from '../utils/number'
import { UsageBar } from './UsageBar'

type Props = {
  nodes: DashboardGpuNode[] | null
  truncated: boolean
}

/** Latest stored GPU measurements only; never issue a refresh/collection POST. */
export function DashboardGpuSnapshot({ nodes, truncated }: Props) {
  return (
    <section
      className="dashboard-v2__section"
      aria-labelledby="dashboard-gpu-heading"
      data-testid="dashboard-gpu-summary"
    >
      <div className="dashboard-v2__section-head">
        <div>
          <h2 id="dashboard-gpu-heading">GPU 자원 요약</h2>
          <p className="dashboard-v2__section-note">
            DB에 마지막으로 저장된 GPU 측정값입니다. 실시간 GPU 상태가 아니며
            자동 수집을 실행하지 않습니다.
          </p>
        </div>
        <Link className="dashboard-v2__link" to="/nodes">
          서버 및 GPU 상세
        </Link>
      </div>

      {nodes === null ? (
        <p className="empty-hint" role="status">
          GPU 스냅샷 확인 불가: 서버 목록 조회에 실패했습니다.
        </p>
      ) : nodes.length === 0 ? (
        <p className="empty-hint">등록된 서버가 없습니다.</p>
      ) : (
        <>
          {truncated ? (
            <p className="dashboard-v2__section-note" role="status">
              서버가 5대보다 많아 앞의 5대만 표시합니다. 전체 서버는 상세에서
              확인하세요.
            </p>
          ) : null}
          <div className="dashboard-v2__gpu-grid">
            {nodes.map((node) => (
              <article className="dashboard-v2__gpu-node" key={node.nodeId}>
                <h3>
                  <Link className="dashboard-v2__link" to={`/nodes/${node.nodeId}`}>
                    {node.nodeName}
                  </Link>
                </h3>
                {node.error ? (
                  <p className="empty-hint" role="status">
                    이 서버의 GPU 자원 확인 불가. 다른 서버 측정값은 유지합니다.
                  </p>
                ) : node.resources === null ? (
                  <p className="empty-hint">GPU 자원이 미수집입니다.</p>
                ) : node.resources.gpus.length === 0 ? (
                  <p className="empty-hint">등록된 GPU가 없습니다 (0대).</p>
                ) : (
                  <ul className="dashboard-v2__gpu-list">
                    {node.resources.gpus.map(({ gpu, snapshot }) => {
                      const sampled = snapshot
                        ? Date.parse(snapshot.sampled_at)
                        : NaN
                      const isOld =
                        Number.isFinite(sampled) &&
                        Date.now() - sampled > 10 * 60 * 1000
                      return (
                        <li className="dashboard-v2__gpu-item" key={gpu.id}>
                          <p className="dashboard-v2__gpu-name">
                            GPU {gpu.device_index} · {gpu.model_name}
                          </p>
                          {snapshot === null ? (
                            <p className="empty-hint">
                              측정값 미수집 (0%로 대체하지 않음)
                            </p>
                          ) : (
                            <>
                              <UsageBar
                                label="VRAM 사용 / 전체"
                                displayValue={`${formatMemoryMb(snapshot.vram_used_mb)} / ${formatMemoryMb(snapshot.vram_total_mb)}`}
                                percentage={usagePercent(
                                  snapshot.vram_used_mb,
                                  snapshot.vram_total_mb,
                                )}
                              />
                              <UsageBar
                                label="GPU 연산 사용률"
                                displayValue={
                                  snapshot.gpu_utilization_pct === null
                                    ? '미수집'
                                    : formatUtilizationPct(
                                        snapshot.gpu_utilization_pct,
                                      )
                                }
                                percentage={snapshot.gpu_utilization_pct}
                              />
                              <p className="dashboard-v2__gpu-time">
                                측정 시각: {formatApiDateTime(snapshot.sampled_at)}
                                {isOld ? ' · 오래된 저장값' : ' · 저장된 측정값'}
                              </p>
                            </>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  )
}
