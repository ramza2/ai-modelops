type LoadingBlockProps = {
  label?: string
  rows?: number
}

export function LoadingBlock({
  label = '불러오는 중',
  rows = 3,
}: LoadingBlockProps) {
  return (
    <div className="loading-block" role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="loading-block__row" />
      ))}
    </div>
  )
}
