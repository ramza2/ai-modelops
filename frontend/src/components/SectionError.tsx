type SectionErrorProps = {
  title?: string
  /** What went wrong (cause). */
  message: string
  /** Optional guidance on what the user can do next. */
  action?: string
  onRetry?: () => void
}

export function SectionError({
  title = '이 구역을 불러오지 못했습니다',
  message,
  action,
  onRetry,
}: SectionErrorProps) {
  return (
    <div className="section-error" role="alert">
      <p className="section-error__title">{title}</p>
      <p className="section-error__message">{message}</p>
      {action ? <p className="section-error__action">{action}</p> : null}
      {onRetry ? (
        <button type="button" className="btn btn--ghost" onClick={onRetry}>
          다시 시도
        </button>
      ) : null}
    </div>
  )
}
