type SectionErrorProps = {
  title?: string
  message: string
  onRetry?: () => void
}

export function SectionError({
  title = '이 구역을 불러오지 못했습니다',
  message,
  onRetry,
}: SectionErrorProps) {
  return (
    <div className="section-error" role="alert">
      <p className="section-error__title">{title}</p>
      <p className="section-error__message">{message}</p>
      {onRetry ? (
        <button type="button" className="btn btn--ghost" onClick={onRetry}>
          다시 시도
        </button>
      ) : null}
    </div>
  )
}
