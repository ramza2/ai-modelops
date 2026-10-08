type PaginationProps = {
  page: number
  pageSize: number
  /** Registry-style absolute total. Omit/null when using hasMore (catalog). */
  total?: number | null
  /** Catalog-style cursor flag. When set, Prev/Next ignores total-page math. */
  hasMore?: boolean
  onPageChange: (page: number) => void
  disabled?: boolean
}

export function Pagination({
  page,
  pageSize,
  total = null,
  hasMore,
  onPageChange,
  disabled = false,
}: PaginationProps) {
  const cursorMode = hasMore !== undefined
  const safePage = Math.max(1, page)

  if (cursorMode) {
    const atFirst = safePage <= 1
    const atLast = !hasMore
    return (
      <nav className="pagination" aria-label="페이지 탐색">
        <button
          type="button"
          className="btn"
          disabled={disabled || atFirst}
          onClick={() => onPageChange(safePage - 1)}
        >
          이전
        </button>
        <span className="pagination__range" aria-live="polite">
          페이지 {safePage}
          {hasMore ? '' : ' · 끝'}
        </span>
        <button
          type="button"
          className="btn"
          disabled={disabled || atLast}
          onClick={() => onPageChange(safePage + 1)}
        >
          다음
        </button>
      </nav>
    )
  }

  const totalCount = total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize) || 1)
  const boundedPage = Math.min(safePage, totalPages)
  const start = totalCount === 0 ? 0 : (boundedPage - 1) * pageSize + 1
  const end = Math.min(boundedPage * pageSize, totalCount)
  const atFirst = boundedPage <= 1
  const atLast = boundedPage >= totalPages || totalCount === 0

  return (
    <nav className="pagination" aria-label="페이지 탐색">
      <button
        type="button"
        className="btn"
        disabled={disabled || atFirst}
        onClick={() => onPageChange(boundedPage - 1)}
      >
        이전
      </button>
      <span className="pagination__range" aria-live="polite">
        {totalCount === 0 ? '0 / 0' : `${start}–${end} / ${totalCount}`}
      </span>
      <button
        type="button"
        className="btn"
        disabled={disabled || atLast}
        onClick={() => onPageChange(boundedPage + 1)}
      >
        다음
      </button>
    </nav>
  )
}
