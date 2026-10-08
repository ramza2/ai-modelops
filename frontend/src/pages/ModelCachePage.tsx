import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listModelCaches, purgeModelCache } from '../api/downloads'
import { listNodes } from '../api/nodes'
import type { ModelCacheEntry, NodeSummary } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime } from '../utils/date'
import { abbreviateMiddle } from '../utils/format'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20

function formatBytes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let v = value / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`
}

export function ModelCachePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const nodeId = searchParams.get('node_id') ?? ''
  const page = parsePositivePage(searchParams.get('page'))

  const [nodes, setNodes] = useState<NodeSummary[]>([])
  const [items, setItems] = useState<ModelCacheEntry[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [purgeTarget, setPurgeTarget] = useState<ModelCacheEntry | null>(null)
  const [purging, setPurging] = useState(false)
  const [purgeError, setPurgeError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  const syncUrl = useCallback(
    (next: { nodeId: string; page: number }) => {
      const params = new URLSearchParams()
      if (next.nodeId) params.set('node_id', next.nodeId)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial' && !hasLoadedRef.current) setLoading(true)
      else setRefreshing(true)
      setError(null)

      try {
        const [nodePage, cachePage] = await Promise.all([
          listNodes({ page: 1, pageSize: 100, signal: controller.signal }),
          listModelCaches({
            nodeId: nodeId || null,
            page,
            pageSize: PAGE_SIZE,
            signal: controller.signal,
          }),
        ])
        if (controller.signal.aborted || gen !== readGenRef.current) return

        const maxPage = Math.max(1, Math.ceil(cachePage.total / PAGE_SIZE) || 1)
        if (page > maxPage && cachePage.total > 0) {
          syncUrl({ nodeId, page: 1 })
          return
        }

        setNodes(nodePage.items)
        setItems(cachePage.items)
        setTotal(cachePage.total)
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('모델 캐시 목록을 불러오지 못했습니다.')
        }
        setItems([])
        setTotal(0)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [nodeId, page, syncUrl],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onConfirmPurge = async () => {
    if (!purgeTarget) return
    setPurging(true)
    setPurgeError(null)
    try {
      await purgeModelCache(purgeTarget.id)
      setPurgeTarget(null)
      await load('refresh')
    } catch (err) {
      setPurgeError(
        err instanceof ApiError ? err.message : '캐시 삭제에 실패했습니다.',
      )
    } finally {
      setPurging(false)
    }
  }

  return (
    <AppShell
      title="Model Cache"
      description="Node에 다운로드된 Hugging Face 아티팩트 캐시입니다. Downloaded ≠ Deployed ≠ Published."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← Models / Versions
        </Link>
        <Link className="btn btn--ghost" to="/models/catalog">
          Model Catalog
        </Link>
        <span className="secondary-text">
          Purge는 Node Agent의 로컬 파일을 제거합니다. Registry Model/Version은
          유지됩니다.
        </span>
      </div>

      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <label className="toolbar__field" htmlFor="cache-node-filter">
          <span>Node</span>
          <select
            id="cache-node-filter"
            value={nodeId}
            onChange={(e) =>
              syncUrl({ nodeId: e.target.value, page: 1 })
            }
          >
            <option value="">전체</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.status})
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <SectionError title="캐시 목록 오류" message={error} /> : null}

      {loading && items === null ? (
        <LoadingBlock label="모델 캐시를 불러오는 중…" />
      ) : null}

      {items !== null && items.length === 0 ? (
        <p className="secondary-text">등록된 캐시가 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Revision</th>
                  <th scope="col">Node</th>
                  <th scope="col">Size</th>
                  <th scope="col">Status</th>
                  <th scope="col">Path</th>
                  <th scope="col">Updated</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.repository_id || '—'}</td>
                    <td className="mono">{row.resolved_revision || '—'}</td>
                    <td>{row.node_name || row.node_id}</td>
                    <td>{formatBytes(row.size_bytes)}</td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td>
                      {row.local_path ? (
                        <TruncatedValue
                          className="mono"
                          value={row.local_path}
                          abbreviated={abbreviateMiddle(row.local_path, 28, 12)}
                        />
                      ) : (
                        <span className="secondary-text">—</span>
                      )}
                    </td>
                    <td className="secondary-text">
                      {formatApiDateTime(row.updated_at)}
                    </td>
                    <td>
                      <div className="toolbar__actions">
                        <Link
                          className="btn"
                          to={`/models/cache/${row.id}/deploy`}
                          aria-disabled={row.status !== 'READY'}
                          onClick={(e) => {
                            if (row.status !== 'READY') e.preventDefault()
                          }}
                          title={
                            row.status === 'READY'
                              ? 'Deploy READY cache'
                              : 'Deploy requires READY cache'
                          }
                          style={
                            row.status !== 'READY'
                              ? { pointerEvents: 'none', opacity: 0.5 }
                              : undefined
                          }
                        >
                          Deploy
                        </Link>
                        <button
                          type="button"
                          className="btn btn--danger"
                          onClick={() => {
                            setPurgeError(null)
                            setPurgeTarget(row)
                          }}
                        >
                          Purge
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(next) => syncUrl({ nodeId, page: next })}
          />
        </>
      ) : null}

      {purgeTarget ? (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="purge-cache-title"
        >
          <div className="modal-card">
            <h2 id="purge-cache-title">캐시 Purge 확인</h2>
            <p>
              <strong>{purgeTarget.repository_id}</strong> (
              {purgeTarget.resolved_revision || 'revision unknown'}) on{' '}
              {purgeTarget.node_name || purgeTarget.node_id}의 로컬 캐시를
              삭제합니다.
            </p>
            <p className="secondary-text">
              Downloaded cache ≠ Running deployment. 실행 중 Deployment가 이
              버전을 사용 중이면 Purge가 거부될 수 있습니다.
            </p>
            {purgeError ? (
              <SectionError title="Purge 실패" message={purgeError} />
            ) : null}
            <div className="toolbar__actions">
              <button
                type="button"
                className="btn"
                disabled={purging}
                onClick={() => {
                  setPurgeTarget(null)
                  setPurgeError(null)
                }}
              >
                취소
              </button>
              <button
                type="button"
                className="btn btn--danger"
                disabled={purging}
                onClick={() => void onConfirmPurge()}
              >
                {purging ? '삭제 중…' : 'Purge 확인'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
