type ActiveBadgeProps = {
  active: boolean
}

export function ActiveBadge({ active }: ActiveBadgeProps) {
  const code = active ? 'ACTIVE' : 'INACTIVE'
  const label = active ? '활성' : '비활성'
  const tone = active ? 'ok' : 'muted'
  return (
    <span className={`status-badge status-badge--${tone}`} title={code}>
      <span className="status-badge__dot" aria-hidden="true" />
      <span className="status-badge__text">
        {label}
        <span className="status-badge__code"> ({code})</span>
      </span>
    </span>
  )
}
