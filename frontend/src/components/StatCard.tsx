import type { ReactNode } from 'react'
import { HelpHint } from './HelpHint'

type StatCardProps = {
  label: string
  value: string
  /** Optional unit shown beside the value (e.g. 건, ms). */
  unit?: string
  hint?: string
  help?: ReactNode
  loading?: boolean
}

/** Hide unit beside non-numeric placeholder values (keep 0 / positives). */
function shouldShowUnit(value: string, unit: string | undefined): boolean {
  if (!unit) return false
  if (value === '미수집' || value === '확인 불가') return false
  return true
}

export function StatCard({
  label,
  value,
  unit,
  hint,
  help,
  loading,
}: StatCardProps) {
  const showUnit = !loading && shouldShowUnit(value, unit)
  return (
    <article className="stat-card" aria-busy={loading || undefined}>
      <div className="stat-card__label-row">
        <h3 className="stat-card__label">{label}</h3>
        {help ? <HelpHint label={label}>{help}</HelpHint> : null}
      </div>
      <p className="stat-card__value">
        {loading ? (
          '…'
        ) : (
          <>
            <span className="stat-card__value-text">{value}</span>
            {showUnit ? <span className="stat-card__unit">{unit}</span> : null}
          </>
        )}
      </p>
      {hint ? <p className="stat-card__hint">{hint}</p> : null}
    </article>
  )
}
