import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listNodes } from '../api/nodes'
import type { NodeSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime } from '../utils/date'
import { formatMemoryMb } from '../utils/number'

const PAGE_SIZE = 20
const KNOWN_STATUSES = new Set([
  'ONLINE',
  'DEGRADED',
  'OFFLINE',
  'UNKNOWN',
])

function parsePage(raw: string | null): number {
  if (!raw) return 1
  const n = Number.parseInt(raw, 10)
  if (!Number.isFinite(n) || n < 1) return 1
  return n
}

function parseStatus(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_STATUSES.has(upper)) return null
  return upper
}

export function NodesPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const status = parseStatus(searchParams.get('status'))
  const page = parsePage(searchParams.get('page'))

  const [items, setItems] = useState<NodeSummary[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)

  const syncUrl = useCallback(
    (next: { status: string | null; page: number }) => {
      const params = new URLSearchParams()
      if (next.status) params.set('status', next.status)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  // Normalize invalid query values in the URL.
  useEffect(() => {
    const rawStatus = searchParams.get('status')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    const parsedStatus = parseStatus(rawStatus)
    if (rawStatus && parsedStatus === null && rawStatus.toUpperCase() !== 'ALL') {
      needsFix = true
    } else if (parsedStatus) {
      fixed.set('status', parsedStatus)
    }

    const parsedPage = parsePage(rawPage)
    if (rawPage !== null && String(parsedPage) !== rawPage) {
      needsFix = true
    }
    if (parsedPage > 1) fixed.set('page', String(parsedPage))

    if (needsFix) {
      setSearchParams(fixed, { replace: true })
    }
  }, [searchParams, setSearchParams])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      if (mode === 'initial' && !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const result = await listNodes({
          status,
          page,
          pageSize: PAGE_SIZE,
          signal: controller.signal,
        })
        if (controller.signal.aborted) return

        // Filter change may leave page beyond total — reset to 1.
        const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
        if (page > maxPage && result.total > 0) {
          syncUrl({ status, page: 1 })
          return
        }

        setItems(result.items)
        setTotal(result.total)
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Node 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) {
          setItems(null)
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [page, status, syncUrl],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onStatusChange = (value: string) => {
    const next = value === 'ALL' ? null : value
    syncUrl({ status: next, page: 1 })
  }

  const onPageChange = (nextPage: number) => {
    syncUrl({ status, page: nextPage })
  }

  const emptyFiltered = items !== null && items.length === 0 && status !== null
  const emptyDatabase = items !== null && items.length === 0 && status === null

  return (
    <AppShell
      title="Nodes / GPUs"
      description="등록된 serving host의 지속 상태(Management API DB)를 조회합니다. GPU/VRAM 수치는 Node 상세에서 확인하며, GPU 간 free VRAM을 합산하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar">
        <label className="toolbar__field" htmlFor="node-status-filter">
          <span>상태</span>
          <select
            id="node-status-filter"
            value={status ?? 'ALL'}
            onChange={(e) => onStatusChange(e.target.value)}
          >
            <option value="ALL">ALL</option>
            <option value="ONLINE">ONLINE</option>
            <option value="DEGRADED">DEGRADED</option>
            <option value="OFFLINE">OFFLINE</option>
            <option value="UNKNOWN">UNKNOWN</option>
          </select>
        </label>
        <p className="toolbar__hint">
          새로고침은 DB에 저장된 Node 목록을 다시 읽습니다. Node Agent를 호출하지
          않습니다.
        </p>
      </div>

      {error ? (
        <SectionError title="Node 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Node 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Node</th>
                  <th scope="col">Status</th>
                  <th scope="col">Hostname</th>
                  <th scope="col">Environment</th>
                  <th scope="col">Region</th>
                  <th scope="col">Last Heartbeat</th>
                  <th scope="col">CPU</th>
                  <th scope="col">RAM</th>
                </tr>
              </thead>
              <tbody>
                {items.map((node) => (
                  <tr key={node.id}>
                    <td>
                      <Link className="table-link" to={`/nodes/${node.id}`}>
                        {node.name}
                      </Link>
                    </td>
                    <td>
                      <StatusBadge status={node.status} />
                    </td>
                    <td>{node.hostname || '—'}</td>
                    <td>{node.environment || '—'}</td>
                    <td>{node.region || '—'}</td>
                    <td>{formatApiDateTime(node.last_heartbeat_at)}</td>
                    <td>{node.cpu_model || '—'}</td>
                    <td>{formatMemoryMb(node.ram_total_mb)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={onPageChange}
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 Node가 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          선택한 상태에 해당하는 Node가 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
