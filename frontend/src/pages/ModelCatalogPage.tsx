import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { analyzeHfResourceFit, listHfCatalog } from '../api/catalog'
import { listNodes } from '../api/nodes'
import type {
  HfCatalogModel,
  NodeSummary,
  ResourceFitAnalysis,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20
const KNOWN_TYPES = new Set(['LLM', 'VLM', 'EMBEDDING'])

function parseModelType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TYPES.has(upper)) return null
  return upper
}

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

function fitReason(item: HfCatalogModel, detail?: ResourceFitAnalysis | null): string {
  if (detail?.reasons?.length) return detail.reasons[0]
  if (item.resource_fit?.reasons?.length) return item.resource_fit.reasons[0]
  if (detail?.result) return `Overall ${detail.result} (advisory)`
  if (item.resource_fit?.result) return `Overall ${item.resource_fit.result} (advisory)`
  return 'Node를 선택하면 자원 적합도를 분석합니다.'
}

export function ModelCatalogPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const modelType = parseModelType(searchParams.get('model_type'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositivePage(searchParams.get('page'))
  const nodeId = searchParams.get('node_id') ?? ''
  const fitOnly = searchParams.get('fit_only') === 'true'

  const [draftQ, setDraftQ] = useState(q)
  const [nodes, setNodes] = useState<NodeSummary[]>([])
  const [items, setItems] = useState<HfCatalogModel[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [fitByRepo, setFitByRepo] = useState<Record<string, ResourceFitAnalysis>>({})
  const [fitLoading, setFitLoading] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  useEffect(() => {
    setDraftQ(q)
  }, [q])

  const syncUrl = useCallback(
    (next: {
      q: string
      modelType: string | null
      page: number
      nodeId: string
      fitOnly: boolean
    }) => {
      const params = new URLSearchParams()
      const trimmedQ = next.q.trim()
      if (trimmedQ) params.set('q', trimmedQ)
      if (next.modelType) params.set('model_type', next.modelType)
      if (next.nodeId) params.set('node_id', next.nodeId)
      if (next.fitOnly) params.set('fit_only', 'true')
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

      if (mode === 'initial' || !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const [nodePage, catalog] = await Promise.all([
          listNodes({ page: 1, pageSize: 100, signal: controller.signal }),
          listHfCatalog({
            q: q.trim() || null,
            modelType,
            page,
            pageSize: PAGE_SIZE,
            fitOnly,
            nodeId: nodeId || null,
            signal: controller.signal,
          }),
        ])
        if (controller.signal.aborted || gen !== readGenRef.current) return
        setNodes(nodePage.items)
        setItems(catalog.items)
        setTotal(catalog.total)
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError('Hugging Face 카탈로그를 불러오지 못했습니다.')
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
    [q, modelType, page, fitOnly, nodeId],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onSubmitSearch = (event: FormEvent) => {
    event.preventDefault()
    syncUrl({
      q: draftQ,
      modelType,
      page: 1,
      nodeId,
      fitOnly,
    })
  }

  const onAnalyze = async (item: HfCatalogModel) => {
    if (!nodeId) return
    setFitLoading(item.repository_id)
    try {
      const result = await analyzeHfResourceFit({
        repositoryId: item.repository_id,
        revision: item.revision,
        nodeId,
        modelType: item.model_type,
      })
      setFitByRepo((prev) => ({ ...prev, [item.repository_id]: result }))
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : '자원 적합도 분석에 실패했습니다.',
      )
    } finally {
      setFitLoading(null)
    }
  }

  return (
    <AppShell
      title="Model Catalog"
      description="Hugging Face 모델을 검색하고 현재 Node 자원과의 적합도를 자문합니다. Download/Deploy는 다음 단계에서 제공됩니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--ghost" to="/models">
          ← Models / Versions
        </Link>
        <span className="secondary-text">
          FIT 결과는 자문용이며 배포 성공을 보장하지 않습니다.
        </span>
      </div>

      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="catalog-q">
          <span>검색</span>
          <input
            id="catalog-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="repository / keyword"
          />
        </label>
        <label className="toolbar__field" htmlFor="catalog-type">
          <span>Type</span>
          <select
            id="catalog-type"
            value={modelType ?? 'ALL'}
            onChange={(e) => {
              const next =
                e.target.value === 'ALL' ? null : e.target.value.toUpperCase()
              syncUrl({
                q,
                modelType: next && KNOWN_TYPES.has(next) ? next : null,
                page: 1,
                nodeId,
                fitOnly,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="LLM">LLM</option>
            <option value="VLM">VLM</option>
            <option value="EMBEDDING">EMBEDDING</option>
          </select>
        </label>
        <label className="toolbar__field" htmlFor="catalog-node">
          <span>Node</span>
          <select
            id="catalog-node"
            value={nodeId}
            onChange={(e) => {
              syncUrl({
                q,
                modelType,
                page: 1,
                nodeId: e.target.value,
                fitOnly: e.target.value ? fitOnly : false,
              })
              setFitByRepo({})
            }}
          >
            <option value="">선택…</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.status})
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="catalog-fit-only">
          <span>설치 가능만</span>
          <input
            id="catalog-fit-only"
            type="checkbox"
            checked={fitOnly}
            disabled={!nodeId}
            onChange={(e) =>
              syncUrl({
                q,
                modelType,
                page: 1,
                nodeId,
                fitOnly: e.target.checked,
              })
            }
          />
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
        </div>
      </form>

      {error ? (
        <SectionError title="카탈로그 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Hugging Face 카탈로그를 불러오는 중…" />
      ) : null}

      {items !== null && items.length === 0 ? (
        <p className="secondary-text">조건에 맞는 모델이 없습니다.</p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Type</th>
                  <th scope="col">Size / VRAM est.</th>
                  <th scope="col">Fit</th>
                  <th scope="col">Reason</th>
                  <th scope="col">Next</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const detail = fitByRepo[item.repository_id]
                  const fitCode =
                    detail?.result || item.resource_fit?.result || null
                  return (
                    <tr key={item.repository_id}>
                      <td>
                        <div className="mono">{item.repository_id}</div>
                        <div className="secondary-text">
                          {item.pipeline_tag || '—'}
                          {item.gated ? ' · gated' : ''}
                          {item.quantization_hint
                            ? ` · ${item.quantization_hint}`
                            : ''}
                        </div>
                      </td>
                      <td>{item.model_type || '—'}</td>
                      <td>
                        <div>
                          {formatBytes(item.estimated_download_size_bytes)}
                        </div>
                        <div className="secondary-text">
                          {item.estimated_required_vram_mb != null
                            ? `~${item.estimated_required_vram_mb} MiB VRAM`
                            : 'VRAM unknown'}
                        </div>
                      </td>
                      <td>
                        {fitCode ? (
                          <StatusBadge status={fitCode} />
                        ) : (
                          <span className="secondary-text">—</span>
                        )}
                      </td>
                      <td className="secondary-text">
                        {fitReason(item, detail)}
                      </td>
                      <td>
                        <div className="toolbar__actions">
                          <button
                            type="button"
                            className="btn"
                            disabled={!nodeId || fitLoading === item.repository_id}
                            onClick={() => void onAnalyze(item)}
                          >
                            {fitLoading === item.repository_id
                              ? '분석 중…'
                              : 'Fit 분석'}
                          </button>
                          <button type="button" className="btn" disabled title="M7-B">
                            Download
                          </button>
                          <button type="button" className="btn" disabled title="M7-B">
                            Deploy
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(next) =>
              syncUrl({
                q,
                modelType,
                page: next,
                nodeId,
                fitOnly,
              })
            }
          />
        </>
      ) : null}
    </AppShell>
  )
}
