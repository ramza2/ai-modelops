type PaginationProps = {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  disabled?: boolean
}

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  disabled = false,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize) || 1)
  const safePage = Math.min(Math.max(1, page), totalPages)
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1
  const end = Math.min(safePage * pageSize, total)
  const atFirst = safePage <= 1
  const atLast = safePage >= totalPages || total === 0

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
        {total === 0 ? '0 / 0' : `${start}–${end} / ${total}`}
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
