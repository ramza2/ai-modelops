import { StatusBadge } from './StatusBadge'
import {
  ENDPOINT_FIELD_HELP,
  TRAFFIC_STATE_LABELS,
} from '../utils/uiLabels'

type EndpointTrafficDisplayProps = {
  isEnabled: boolean
  trafficState: string | null | undefined
  hasActiveRoute: boolean
}

/**
 * Title for live (enabled + ACTIVE Route) Traffic StatusBadge.
 * Reuses ENDPOINT_FIELD_HELP.traffic wording; SERVING keeps trafficServingOk.
 */
function liveTrafficTitle(code: string): string {
  if (code === 'SERVING') return ENDPOINT_FIELD_HELP.trafficServingOk
  if (code === 'DRAINING') {
    // Same meaning as ENDPOINT_FIELD_HELP.traffic DRAINING clause.
    return '요청 처리 상태(Traffic) DRAINING: 신규 요청 차단·진행 중 요청 대기.'
  }
  if (code === 'MAINTENANCE') {
    // Same meaning as ENDPOINT_FIELD_HELP.traffic MAINTENANCE clause.
    return '요청 처리 상태(Traffic) MAINTENANCE: 신규 요청 차단·upstream 전달 금지.'
  }
  return ENDPOINT_FIELD_HELP.traffic
}

/**
 * Endpoint-only traffic presentation.
 * Does not change global StatusBadge meanings or mutate API traffic_state.
 */
export function EndpointTrafficDisplay({
  isEnabled,
  trafficState,
  hasActiveRoute,
}: EndpointTrafficDisplayProps) {
  const code = (trafficState || 'UNKNOWN').toUpperCase()

  if (!isEnabled) {
    return (
      <span
        className="status-badge status-badge--muted"
        data-testid="endpoint-traffic-display"
        data-traffic-code={code}
        title={`${ENDPOINT_FIELD_HELP.trafficDisplay} 원본 Traffic: ${code}`}
      >
        <span className="status-badge__dot" aria-hidden="true" />
        <span className="status-badge__text">
          사용 중지
          <span className="status-badge__code"> (Traffic: {code})</span>
        </span>
      </span>
    )
  }

  if (!hasActiveRoute) {
    return (
      <span
        className="status-badge status-badge--muted"
        data-testid="endpoint-traffic-display"
        data-traffic-code={code}
        title={`${ENDPOINT_FIELD_HELP.unusedAlias} 원본 Traffic: ${code}`}
      >
        <span className="status-badge__dot" aria-hidden="true" />
        <span className="status-badge__text">
          모델 미연결
          <span className="status-badge__code"> (Traffic: {code})</span>
        </span>
      </span>
    )
  }

  const trafficLabel = TRAFFIC_STATE_LABELS[code]
  return (
    <span data-testid="endpoint-traffic-display" data-traffic-code={code}>
      <StatusBadge
        status={trafficState}
        label={trafficLabel}
        title={liveTrafficTitle(code)}
      />
    </span>
  )
}
