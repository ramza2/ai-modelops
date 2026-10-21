import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listNodes } from '../api/nodes'
import type { NodeSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime } from '../utils/date'
import { formatMemoryMb } from '../utils/number'
import { parsePositivePage } from '../utils/query'
import { NODE_FIELD_HELP } from '../utils/uiLabels'

const PAGE_SIZE = 20
const KNOWN_STATUSES = new Set([
  'ONLINE',
  'DEGRADED',
  'OFFLINE',
  'UNKNOWN',
])

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
  const page = parsePositivePage(searchParams.get('page'))

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

    const parsedPage = parsePositivePage(rawPage)
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

        // Out-of-range page (including total=0) → canonical page 1; preserve status.
        const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
        if (page > maxPage) {
          if (result.total === 0) {
            setItems([])
            setTotal(0)
            setLastUpdated(new Date())
            hasLoadedRef.current = true
          }
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
              : '서버 목록을 불러오지 못했습니다.'
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
      title="서버 및 GPU"
      description="등록된 GPU 서버(Node)의 연결 상태를 조회합니다. GPU/VRAM 수치는 서버 상세에서 확인하며, GPU 간 남은 VRAM을 합산하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {NODE_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="서버 상태 (ONLINE 등)">
            {NODE_FIELD_HELP.status}
          </HelpHint>
          <HelpHint showTopic label="마지막 연결 확인 vs 자원 측정">
            {NODE_FIELD_HELP.heartbeat}
          </HelpHint>
          <HelpHint showTopic label="VRAM 합산 금지">
            {NODE_FIELD_HELP.noPoolVram}
          </HelpHint>
        </div>
      </details>

      <div className="toolbar">
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="node-status-filter">상태</label>
            <HelpHint label="서버 상태">{NODE_FIELD_HELP.status}</HelpHint>
          </div>
          <select
            id="node-status-filter"
            value={status ?? 'ALL'}
            onChange={(e) => onStatusChange(e.target.value)}
          >
            <option value="ALL">전체</option>
            <option value="ONLINE">온라인 (ONLINE)</option>
            <option value="DEGRADED">저하 (DEGRADED)</option>
            <option value="OFFLINE">오프라인 (OFFLINE)</option>
            <option value="UNKNOWN">알 수 없음 (UNKNOWN)</option>
          </select>
        </div>
        <p className="toolbar__hint">
          상단 새로고침은 DB에 저장된 서버 목록을 다시 읽습니다. Node Agent를
          호출하지 않으며, GPU 실시간 수집은 서버 상세의 「리소스 갱신」에서
          합니다.
        </p>
      </div>

      {error ? (
        <SectionError title="서버 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="서버 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="서버" apiTerm="Node" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="상태"
                      apiTerm="Status"
                      help={NODE_FIELD_HELP.status}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="서버 호스트명" apiTerm="Hostname" />
                  </th>
                  <th scope="col">
                    <TermLabel text="운영 환경" apiTerm="Environment" />
                  </th>
                  <th scope="col">
                    <TermLabel text="위치/리전" apiTerm="Region" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="마지막 연결 확인"
                      apiTerm="Last Heartbeat"
                      help={NODE_FIELD_HELP.heartbeat}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="CPU" apiTerm="CPU" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="시스템 메모리"
                      apiTerm="RAM"
                      help={NODE_FIELD_HELP.ramVsVram}
                    />
                  </th>
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
                      <StatusBadge
                        status={node.status}
                        title={NODE_FIELD_HELP.status}
                      />
                    </td>
                    <td className="table-cell-clip" title={node.hostname || undefined}>
                      {node.hostname || '—'}
                    </td>
                    <td>{node.environment || '—'}</td>
                    <td>{node.region || '—'}</td>
                    <td>{formatApiDateTime(node.last_heartbeat_at)}</td>
                    <td className="table-cell-clip" title={node.cpu_model || undefined}>
                      {node.cpu_model || '—'}
                    </td>
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
          등록된 서버가 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          선택한 상태에 해당하는 서버가 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
