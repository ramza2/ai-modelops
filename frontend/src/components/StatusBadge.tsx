const LABELS: Record<string, string> = {
  ONLINE: '온라인',
  OFFLINE: '오프라인',
  UNKNOWN: '알 수 없음',
  RUNNING: '실행 중',
  STOPPED: '중지',
  FAILED: '실패',
  CREATED: '생성됨',
  HEALTHY: '정상',
  DEGRADED: '저하',
  UNHEALTHY: '비정상',
  STARTING: '시작 중',
  SERVING: '서빙',
  DRAINING: '드레인',
  MAINTENANCE: '유지보수',
  QUEUED: '대기',
  ROLLING_BACK: '롤백 중',
  SUCCEEDED: '성공',
  ROLLED_BACK: '롤백됨',
  CANCELLED: '취소됨',
  MANUAL_INTERVENTION_REQUIRED: '수동 개입 필요',
  AVAILABLE: '사용 가능',
  UNAVAILABLE: '사용 불가',
  HOT_SWITCH_AVAILABLE: 'Hot 가능',
  COLD_SWITCH_ONLY: 'Cold만 가능',
  RESOURCE_INSUFFICIENT: '자원 부족',
  ACTIVE: '활성',
  INACTIVE: '비활성',
}

const TONE: Record<string, string> = {
  ONLINE: 'ok',
  HEALTHY: 'ok',
  SERVING: 'ok',
  SUCCEEDED: 'ok',
  AVAILABLE: 'ok',
  HOT_SWITCH_AVAILABLE: 'ok',
  ACTIVE: 'ok',
  RUNNING: 'info',
  QUEUED: 'info',
  STARTING: 'info',
  COLD_SWITCH_ONLY: 'warn',
  DRAINING: 'warn',
  DEGRADED: 'warn',
  ROLLING_BACK: 'warn',
  MAINTENANCE: 'warn',
  OFFLINE: 'bad',
  FAILED: 'bad',
  UNHEALTHY: 'bad',
  UNAVAILABLE: 'bad',
  RESOURCE_INSUFFICIENT: 'bad',
  STOPPED: 'muted',
  CANCELLED: 'muted',
  ROLLED_BACK: 'muted',
  CREATED: 'muted',
  UNKNOWN: 'muted',
  INACTIVE: 'muted',
  MANUAL_INTERVENTION_REQUIRED: 'bad',
}

type StatusBadgeProps = {
  status: string | null | undefined
  label?: string
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const code = (status || 'UNKNOWN').toUpperCase()
  const tone = TONE[code] || 'muted'
  const text = label || LABELS[code] || code
  return (
    <span className={`status-badge status-badge--${tone}`} title={code}>
      <span className="status-badge__dot" aria-hidden="true" />
      <span className="status-badge__text">
        {text}
        <span className="status-badge__code"> ({code})</span>
      </span>
    </span>
  )
}
