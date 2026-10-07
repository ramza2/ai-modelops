import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listDeployments } from '../api/deployments'
import type { Deployment } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime, shortId } from '../utils/date'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20

const KNOWN_TYPES = new Set(['MANAGED', 'IMPORTED'])
const KNOWN_RUNTIME = new Set([
  'CREATED',
  'RUNNING',
  'STOPPED',
  'FAILED',
  'UNKNOWN',
])
const KNOWN_HEALTH = new Set([
  'UNKNOWN',
  'STARTING',
  'HEALTHY',
  'DEGRADED',
  'UNHEALTHY',
])

function parseEnum(
  raw: string | null,
  known: Set<string>,
): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!known.has(upper)) return null
  return upper
}

/** Strict retired query: only lowercase true/false canonical. */
export function parseRetiredQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function retiredLabel(value: boolean | null): string {
  if (value === true) return 'RETIRED'
  if (value === false) return 'ACTIVE'
  return 'ALL'
}

function isUuid(raw: string | null): boolean {
  if (!raw) return false
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    raw,
  )
}

export function DeploymentsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const deploymentType = parseEnum(
    searchParams.get('deployment_type'),
    KNOWN_TYPES,
  )
  const runtimeStatus = parseEnum(
    searchParams.get('runtime_status'),
    KNOWN_RUNTIME,
  )
  const healthStatus = parseEnum(
    searchParams.get('health_status'),
    KNOWN_HEALTH,
  )
  const retired = parseRetiredQuery(searchParams.get('retired'))
  const nodeId = isUuid(searchParams.get('node_id'))
    ? searchParams.get('node_id')
    : null
  const modelId = isUuid(searchParams.get('model_id'))
    ? searchParams.get('model_id')
    : null
  const modelVersionId = isUuid(searchParams.get('model_version_id'))
    ? searchParams.get('model_version_id')
    : null
  const page = parsePositivePage(searchParams.get('page'))

  const [items, setItems] = useState<Deployment[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  const syncUrl = useCallback(
    (next: {
      deploymentType: string | null
      runtimeStatus: string | null
      healthStatus: string | null
      retired: boolean | null
      nodeId: string | null
      modelId: string | null
      modelVersionId: string | null
      page: number
    }) => {
      const params = new URLSearchParams()
      if (next.deploymentType) params.set('deployment_type', next.deploymentType)
      if (next.runtimeStatus) params.set('runtime_status', next.runtimeStatus)
      if (next.healthStatus) params.set('health_status', next.healthStatus)
      if (next.retired === true) params.set('retired', 'true')
      if (next.retired === false) params.set('retired', 'false')
      if (next.nodeId) params.set('node_id', next.nodeId)
      if (next.modelId) params.set('model_id', next.modelId)
      if (next.modelVersionId) {
        params.set('model_version_id', next.modelVersionId)
      }
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawType = searchParams.get('deployment_type')
    const rawRuntime = searchParams.get('runtime_status')
    const rawHealth = searchParams.get('health_status')
    const rawRetired = searchParams.get('retired')
    const rawNode = searchParams.get('node_id')
    const rawModel = searchParams.get('model_id')
    const rawVersion = searchParams.get('model_version_id')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    const applyEnum = (
      raw: string | null,
      known: Set<string>,
      key: string,
    ): string | null => {
      if (!raw) return null
      const parsed = parseEnum(raw, known)
      if (raw.toUpperCase() === 'ALL') {
        needsFix = true
        return null
      }
      if (parsed === null) {
        needsFix = true
        return null
      }
      fixed.set(key, parsed)
      if (raw !== parsed) needsFix = true
      return parsed
    }

    applyEnum(rawType, KNOWN_TYPES, 'deployment_type')
    applyEnum(rawRuntime, KNOWN_RUNTIME, 'runtime_status')
    applyEnum(rawHealth, KNOWN_HEALTH, 'health_status')

    const parsedRetired = parseRetiredQuery(rawRetired)
    if (rawRetired !== null && rawRetired !== '') {
      if (parsedRetired === null) {
        needsFix = true
      } else {
        const canonical = parsedRetired ? 'true' : 'false'
        if (rawRetired !== canonical) needsFix = true
        fixed.set('retired', canonical)
      }
    }

    if (rawNode) {
      if (isUuid(rawNode)) fixed.set('node_id', rawNode)
      else needsFix = true
    }
    if (rawModel) {
      if (isUuid(rawModel)) fixed.set('model_id', rawModel)
      else needsFix = true
    }
    if (rawVersion) {
      if (isUuid(rawVersion)) fixed.set('model_version_id', rawVersion)
      else needsFix = true
    }

    const parsedPage = parsePositivePage(rawPage)
    if (rawPage !== null && String(parsedPage) !== rawPage) needsFix = true
    if (parsedPage > 1) fixed.set('page', String(parsedPage))

    if (needsFix) setSearchParams(fixed, { replace: true })
  }, [searchParams, setSearchParams])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial' && !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const result = await listDeployments({
          nodeId,
          modelId,
          modelVersionId,
          deploymentType,
          runtimeStatus,
          healthStatus,
          retired,
          page,
          pageSize: PAGE_SIZE,
          signal: controller.signal,
        })
        if (controller.signal.aborted || gen !== readGenRef.current) return

        const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
        if (page > maxPage) {
          if (result.total === 0) {
            setItems([])
            setTotal(0)
            setLastUpdated(new Date())
            hasLoadedRef.current = true
          }
          syncUrl({
            deploymentType,
            runtimeStatus,
            healthStatus,
            retired,
            nodeId,
            modelId,
            modelVersionId,
            page: 1,
          })
          return
        }

        setItems(result.items)
        setTotal(result.total)
        setLastUpdated(new Date())
        hasLoadedRef.current = true
      } catch (err) {
        if (controller.signal.aborted || gen !== readGenRef.current) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Deployment 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) {
          setItems(null)
        }
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [
      nodeId,
      modelId,
      modelVersionId,
      deploymentType,
      runtimeStatus,
      healthStatus,
      retired,
      page,
      syncUrl,
    ],
  )

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const hasFilters =
    deploymentType !== null ||
    runtimeStatus !== null ||
    healthStatus !== null ||
    retired !== null ||
    nodeId !== null ||
    modelId !== null ||
    modelVersionId !== null

  const emptyFiltered =
    items !== null && items.length === 0 && hasFilters
  const emptyDatabase =
    items !== null && items.length === 0 && !hasFilters

  return (
    <AppShell
      title="Deployments"
      description="Deployment 메타데이터와 런타임/헬스 상태를 조회합니다. MANAGED lifecycle Start/Stop/Restart는 상세 페이지에서 비동기 Operation으로 enqueue합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <div className="toolbar toolbar--wrap">
        <label className="toolbar__field" htmlFor="dep-type-filter">
          <span>Type</span>
          <select
            id="dep-type-filter"
            value={deploymentType ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                deploymentType: v === 'ALL' ? null : v,
                runtimeStatus,
                healthStatus,
                retired,
                nodeId,
                modelId,
                modelVersionId,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="MANAGED">MANAGED</option>
            <option value="IMPORTED">IMPORTED</option>
          </select>
        </label>
        <label className="toolbar__field" htmlFor="dep-runtime-filter">
          <span>Runtime</span>
          <select
            id="dep-runtime-filter"
            value={runtimeStatus ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                deploymentType,
                runtimeStatus: v === 'ALL' ? null : v,
                healthStatus,
                retired,
                nodeId,
                modelId,
                modelVersionId,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            {[...KNOWN_RUNTIME].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="dep-health-filter">
          <span>Health</span>
          <select
            id="dep-health-filter"
            value={healthStatus ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                deploymentType,
                runtimeStatus,
                healthStatus: v === 'ALL' ? null : v,
                retired,
                nodeId,
                modelId,
                modelVersionId,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            {[...KNOWN_HEALTH].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar__field" htmlFor="dep-retired-filter">
          <span>Retired</span>
          <select
            id="dep-retired-filter"
            value={retiredLabel(retired)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'RETIRED' ? true : v === 'ACTIVE' ? false : null
              syncUrl({
                deploymentType,
                runtimeStatus,
                healthStatus,
                retired: next,
                nodeId,
                modelId,
                modelVersionId,
                page: 1,
              })
            }}
          >
            <option value="ALL">ALL</option>
            <option value="ACTIVE">ACTIVE</option>
            <option value="RETIRED">RETIRED</option>
          </select>
        </label>
      </div>

      {error ? (
        <SectionError title="Deployment 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="Deployment 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  <th scope="col">Type</th>
                  <th scope="col">Desired</th>
                  <th scope="col">Runtime</th>
                  <th scope="col">Health</th>
                  <th scope="col">Node</th>
                  <th scope="col">Version</th>
                  <th scope="col">Retired</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {items.map((dep) => (
                  <tr key={dep.id}>
                    <td>
                      <Link
                        className="table-link"
                        to={`/deployments/${dep.id}`}
                      >
                        {dep.name}
                      </Link>
                      <div className="secondary-text mono">
                        {shortId(dep.id)}
                      </div>
                    </td>
                    <td>{dep.deployment_type || '—'}</td>
                    <td>{dep.desired_state || '—'}</td>
                    <td>
                      <StatusBadge status={dep.runtime_status} />
                    </td>
                    <td>
                      <StatusBadge status={dep.health_status} />
                    </td>
                    <td>
                      {dep.node_id ? (
                        <Link
                          className="table-link mono"
                          to={`/nodes/${dep.node_id}`}
                        >
                          {shortId(dep.node_id)}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      <Link
                        className="table-link mono"
                        to={`/model-versions/${dep.model_version_id}`}
                      >
                        {shortId(dep.model_version_id)}
                      </Link>
                    </td>
                    <td>
                      {dep.retired_at
                        ? formatApiDateTime(dep.retired_at)
                        : '—'}
                    </td>
                    <td>{formatApiDateTime(dep.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(nextPage) =>
              syncUrl({
                deploymentType,
                runtimeStatus,
                healthStatus,
                retired,
                nodeId,
                modelId,
                modelVersionId,
                page: nextPage,
              })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 Deployment가 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          필터 조건에 해당하는 Deployment가 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
