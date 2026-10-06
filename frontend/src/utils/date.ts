export function formatClock(date: Date | null | undefined): string {
  if (!date) return '—'
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDurationMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const totalSec = Math.floor(ms / 1000)
  const hours = Math.floor(totalSec / 3600)
  const minutes = Math.floor((totalSec % 3600) / 60)
  const seconds = totalSec % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

export function operationElapsed(
  op: {
    started_at: string | null
    finished_at: string | null
    created_at: string
  },
  now: Date = new Date(),
): string {
  const start =
    parseApiDate(op.started_at) ?? parseApiDate(op.created_at) ?? null
  if (!start) return '—'
  const end = parseApiDate(op.finished_at) ?? now
  return formatDurationMs(end.getTime() - start.getTime())
}

export function shortId(id: string | null | undefined, size = 8): string {
  if (!id) return '—'
  return id.length <= size ? id : id.slice(0, size)
}
