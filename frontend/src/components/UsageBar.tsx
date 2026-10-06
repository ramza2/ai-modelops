type UsageBarProps = {
  label: string
  displayValue: string
  percentage: number | null
}

export function UsageBar({ label, displayValue, percentage }: UsageBarProps) {
  const known = percentage !== null && Number.isFinite(percentage)
  const clamped = known ? Math.min(100, Math.max(0, percentage)) : null

  return (
    <div className="usage-bar">
      <div className="usage-bar__meta">
        <span className="usage-bar__label">{label}</span>
        <span className="usage-bar__value">{displayValue}</span>
      </div>
      <div className="usage-bar__track" aria-hidden={!known}>
        {known ? (
          <div
            className="usage-bar__fill"
            style={{ width: `${clamped}%` }}
            role="progressbar"
            aria-label={label}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(clamped!)}
          />
        ) : (
          <div className="usage-bar__fill usage-bar__fill--unknown" />
        )}
      </div>
    </div>
  )
}
