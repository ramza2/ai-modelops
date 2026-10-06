type TruncatedValueProps = {
  value: string | null | undefined
  abbreviated: string
  className?: string
}

/** Visual truncation with full value in title for accessibility. */
export function TruncatedValue({
  value,
  abbreviated,
  className,
}: TruncatedValueProps) {
  if (value === null || value === undefined || value === '') {
    return <span className={className}>—</span>
  }
  return (
    <span className={className} title={value}>
      {abbreviated}
    </span>
  )
}
