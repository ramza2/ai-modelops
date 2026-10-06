type StatCardProps = {
  label: string
  value: string
  hint?: string
  loading?: boolean
}

export function StatCard({ label, value, hint, loading }: StatCardProps) {
  return (
    <article className="stat-card" aria-busy={loading || undefined}>
      <h3 className="stat-card__label">{label}</h3>
      <p className="stat-card__value">{loading ? '…' : value}</p>
      {hint ? <p className="stat-card__hint">{hint}</p> : null}
    </article>
  )
}
