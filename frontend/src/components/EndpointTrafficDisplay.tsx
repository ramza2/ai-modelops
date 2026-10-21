import { StatusBadge } from './StatusBadge'
import { ENDPOINT_FIELD_HELP } from '../utils/uiLabels'

type EndpointTrafficDisplayProps = {
  isEnabled: boolean
  trafficState: string | null | undefined
  hasActiveRoute: boolean
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

  return (
    <span data-testid="endpoint-traffic-display" data-traffic-code={code}>
      <StatusBadge
        status={trafficState}
        title={ENDPOINT_FIELD_HELP.trafficServingOk}
      />
    </span>
  )
}
