import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listEndpoints } from '../api/endpoints'
import type { Endpoint } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { EndpointEnabledConfirmDialog } from '../components/EndpointEnabledConfirmDialog'
import { EndpointTrafficDisplay } from '../components/EndpointTrafficDisplay'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { useEndpointEnabledAction } from '../hooks/useEndpointEnabledAction'
import { formatApiDateTime, shortId } from '../utils/date'
import { parsePositivePage } from '../utils/query'
import {
  ENDPOINT_FIELD_HELP,
  formatApiTypeDisplay,
  formatTrafficStateDisplay,
  userFacingLoadError,
} from '../utils/uiLabels'

const PAGE_SIZE = 20
const KNOWN_API_TYPES = new Set(['CHAT', 'EMBEDDING'])
const KNOWN_TRAFFIC = new Set(['SERVING', 'DRAINING', 'MAINTENANCE'])

function parseApiType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_API_TYPES.has(upper)) return null
  return upper
}

function parseTrafficState(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TRAFFIC.has(upper)) return null
  return upper
}

/** Strict enabled query: only lowercase true/false canonical. */
export function parseEnabledQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function enabledLabel(value: boolean | null): string {
  if (value === true) return 'ENABLED'
  if (value === false) return 'DISABLED'
  return 'ALL'
}

export function EndpointsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const apiType = parseApiType(searchParams.get('api_type'))
  const isEnabled = parseEnabledQuery(searchParams.get('is_enabled'))
  const trafficState = parseTrafficState(searchParams.get('traffic_state'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositivePage(searchParams.get('page'))

  const [draftQ, setDraftQ] = useState(q)
  const [items, setItems] = useState<Endpoint[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)
  const loadRef = useRef<(mode: 'initial' | 'refresh') => Promise<void>>(
    async () => {},
  )

  const enabledAction = useEndpointEnabledAction({
    onSuccess: async () => {
      await loadRef.current('refresh')
    },
  })

  useEffect(() => {
    setDraftQ(q)
  }, [q])

  const syncUrl = useCallback(
    (next: {
      q: string
      apiType: string | null
      isEnabled: boolean | null
      trafficState: string | null
      page: number
    }) => {
      const params = new URLSearchParams()
      const trimmedQ = next.q.trim()
      if (trimmedQ) params.set('q', trimmedQ)
      if (next.apiType) params.set('api_type', next.apiType)
      if (next.isEnabled === true) params.set('is_enabled', 'true')
      if (next.isEnabled === false) params.set('is_enabled', 'false')
      if (next.trafficState) params.set('traffic_state', next.trafficState)
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawType = searchParams.get('api_type')
    const rawEnabled = searchParams.get('is_enabled')
    const rawTraffic = searchParams.get('traffic_state')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    if (rawType) {
      const parsed = parseApiType(rawType)
      if (rawType.toUpperCase() === 'ALL' || parsed === null) {
        needsFix = true
      } else {
        fixed.set('api_type', parsed)
        if (rawType !== parsed) needsFix = true
      }
    }

    if (rawEnabled !== null && rawEnabled !== '') {
      const parsed = parseEnabledQuery(rawEnabled)
      if (parsed === null) {
        needsFix = true
      } else {
        const canonical = parsed ? 'true' : 'false'
        if (rawEnabled !== canonical) needsFix = true
        fixed.set('is_enabled', canonical)
      }
    }

    if (rawTraffic) {
      const parsed = parseTrafficState(rawTraffic)
      if (rawTraffic.toUpperCase() === 'ALL' || parsed === null) {
        needsFix = true
      } else {
        fixed.set('traffic_state', parsed)
        if (rawTraffic !== parsed) needsFix = true
      }
    }

    if (searchParams.get('q')) fixed.set('q', searchParams.get('q')!)

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
        const result = await listEndpoints({
          q: q.trim() || null,
          apiType,
          isEnabled,
          trafficState,
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
          syncUrl({ q, apiType, isEnabled, trafficState, page: 1 })
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
              : 'API 연결 목록을 불러오지 못했습니다.'
        setError(message)
        if (!hasLoadedRef.current) setItems(null)
      } finally {
        if (!controller.signal.aborted && gen === readGenRef.current) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    },
    [q, apiType, isEnabled, trafficState, page, syncUrl],
  )
  loadRef.current = load

  useEffect(() => {
    void load(hasLoadedRef.current ? 'refresh' : 'initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const onSubmitSearch = (event: FormEvent) => {
    event.preventDefault()
    syncUrl({ q: draftQ, apiType, isEnabled, trafficState, page: 1 })
  }

  const onClearSearch = () => {
    setDraftQ('')
    syncUrl({ q: '', apiType, isEnabled, trafficState, page: 1 })
  }

  const hasFilters =
    apiType !== null ||
    isEnabled !== null ||
    trafficState !== null ||
    q.trim() !== ''
  const emptyFiltered = items !== null && items.length === 0 && hasFilters
  const emptyDatabase = items !== null && items.length === 0 && !hasFilters
  const listError = error
    ? userFacingLoadError('API 연결 목록', error, {
        keptPrevious: items !== null,
      })
    : null

  return (
    <AppShell
      title="API 연결 관리"
      description="업무 시스템이 Gateway로 호출하는 API 별칭과 현재 활성 연결을 조회합니다. HOT/COLD Switch는 상세에서 Preflight Preview 후 enqueue합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {ENDPOINT_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="API 별칭">
            {ENDPOINT_FIELD_HELP.alias}
          </HelpHint>
          <HelpHint showTopic label="사용 설정">
            {ENDPOINT_FIELD_HELP.enabled}
          </HelpHint>
          <HelpHint showTopic label="사용 중지 vs 다른 정리 작업">
            {ENDPOINT_FIELD_HELP.disableVsOthers}
          </HelpHint>
          <HelpHint showTopic label="요청 처리 상태">
            {ENDPOINT_FIELD_HELP.traffic}
          </HelpHint>
          <HelpHint showTopic label="Traffic 표시 (사용 중지·미연결)">
            {ENDPOINT_FIELD_HELP.trafficDisplay}
          </HelpHint>
          <HelpHint showTopic label="현재 활성 연결">
            {ENDPOINT_FIELD_HELP.activeRoute}
          </HelpHint>
          <HelpHint showTopic label="미연결 별칭">
            {ENDPOINT_FIELD_HELP.unusedAlias}
          </HelpHint>
        </div>
      </details>

      {enabledAction.resultMessage ? (
        <div className="operation-result" role="status" data-testid="endpoint-enabled-result">
          <p>{enabledAction.resultMessage}</p>
          <p className="secondary-text">
            {ENDPOINT_FIELD_HELP.gatewayAfterEnabledChange}
          </p>
        </div>
      ) : null}
      {enabledAction.openError ? (
        <SectionError
          title="사용 설정 변경"
          message={enabledAction.openError}
        />
      ) : null}

      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="endpoint-search-q">
          <span>검색</span>
          <input
            id="endpoint-search-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="별칭 / 표시 이름"
          />
        </label>
        <label className="toolbar__field" htmlFor="endpoint-api-type">
          <span>API 유형 (API Type)</span>
          <select
            id="endpoint-api-type"
            value={apiType ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                q,
                apiType: v === 'ALL' ? null : v,
                isEnabled,
                trafficState,
                page: 1,
              })
            }}
          >
            <option value="ALL">전체</option>
            <option value="CHAT">{formatApiTypeDisplay('CHAT')}</option>
            <option value="EMBEDDING">{formatApiTypeDisplay('EMBEDDING')}</option>
          </select>
        </label>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="endpoint-enabled">사용 설정 (Enabled)</label>
            <HelpHint label="사용 설정">{ENDPOINT_FIELD_HELP.enabled}</HelpHint>
          </div>
          <select
            id="endpoint-enabled"
            value={enabledLabel(isEnabled)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'ENABLED' ? true : v === 'DISABLED' ? false : null
              syncUrl({
                q,
                apiType,
                isEnabled: next,
                trafficState,
                page: 1,
              })
            }}
          >
            <option value="ALL">전체</option>
            <option value="ENABLED">사용 중 (ENABLED)</option>
            <option value="DISABLED">사용 중지됨 (DISABLED)</option>
          </select>
        </div>
        <label
          className="toolbar__field toolbar__field--checkbox"
          htmlFor="endpoint-enabled-only"
        >
          <span className="checkbox-row">
            <input
              id="endpoint-enabled-only"
              type="checkbox"
              checked={isEnabled === true}
              data-testid="endpoint-enabled-only"
              onChange={(e) => {
                syncUrl({
                  q,
                  apiType,
                  isEnabled: e.target.checked ? true : null,
                  trafficState,
                  page: 1,
                })
              }}
            />
            <span>사용 설정된 API만 보기</span>
          </span>
          <span className="secondary-text">
            사용 설정(is_enabled=true)된 API만 조회합니다. 실제 모델 연결
            여부와는 다릅니다.
          </span>
        </label>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="endpoint-traffic">요청 처리 상태 (Traffic)</label>
            <HelpHint label="요청 처리 상태">
              {ENDPOINT_FIELD_HELP.traffic}
            </HelpHint>
          </div>
          <select
            id="endpoint-traffic"
            value={trafficState ?? 'ALL'}
            onChange={(e) => {
              const v = e.target.value
              syncUrl({
                q,
                apiType,
                isEnabled,
                trafficState: v === 'ALL' ? null : v,
                page: 1,
              })
            }}
          >
            <option value="ALL">전체</option>
            {[...KNOWN_TRAFFIC].map((s) => (
              <option key={s} value={s}>
                {formatTrafficStateDisplay(s)}
              </option>
            ))}
          </select>
        </div>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
          <button type="button" className="btn" onClick={onClearSearch}>
            지우기
          </button>
        </div>
        <p className="toolbar__hint">{ENDPOINT_FIELD_HELP.filterApply}</p>
      </form>

      {listError ? (
        <SectionError
          title={listError.title}
          message={listError.message}
          action={listError.action}
        />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="API 연결 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel
                      text="API 별칭"
                      apiTerm="Endpoint Alias"
                      help={ENDPOINT_FIELD_HELP.alias}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="API 유형" apiTerm="API Type" />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="사용 설정"
                      apiTerm="Enabled"
                      help={ENDPOINT_FIELD_HELP.enabled}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="요청 처리 상태"
                      apiTerm="Traffic"
                      help={ENDPOINT_FIELD_HELP.traffic}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel
                      text="현재 연결된 모델 배포"
                      apiTerm="ACTIVE Deployment"
                      help={ENDPOINT_FIELD_HELP.activeRoute}
                    />
                  </th>
                  <th scope="col">
                    <TermLabel text="수정 시각" apiTerm="Updated" />
                  </th>
                  <th scope="col">정리</th>
                </tr>
              </thead>
              <tbody>
                {items.map((ep) => {
                  const active = ep.active_route
                  const dep = active?.deployment
                  const actionBusy =
                    enabledAction.isDialogOpen || enabledAction.submitting
                  return (
                    <tr key={ep.id}>
                      <td>
                        <Link
                          className="table-link"
                          to={`/endpoints/${ep.id}`}
                        >
                          {ep.display_name || ep.alias}
                        </Link>
                        <div className="secondary-text mono">{ep.alias}</div>
                      </td>
                      <td>{formatApiTypeDisplay(ep.api_type)}</td>
                      <td>
                        <ActiveBadge active={ep.is_enabled} />
                      </td>
                      <td>
                        <EndpointTrafficDisplay
                          isEnabled={ep.is_enabled}
                          trafficState={ep.traffic_state}
                          hasActiveRoute={Boolean(active?.deployment_id)}
                        />
                      </td>
                      <td>
                        {active ? (
                          <>
                            <div className="secondary-text">모델 연결됨</div>
                            <Link
                              className="table-link"
                              to={`/deployments/${active.deployment_id}`}
                            >
                              {dep?.name ?? shortId(active.deployment_id)}
                            </Link>
                          </>
                        ) : (
                          <span
                            className="secondary-text"
                            title={ENDPOINT_FIELD_HELP.unusedAlias}
                          >
                            활성 연결 없음
                          </span>
                        )}
                      </td>
                      <td>{formatApiDateTime(ep.updated_at)}</td>
                      <td>
                        {ep.is_enabled ? (
                          <button
                            type="button"
                            className="btn btn--danger btn--small"
                            data-testid={`endpoint-disable-${ep.id}`}
                            disabled={actionBusy || refreshing}
                            onClick={() =>
                              void enabledAction.open(ep.id, 'disable')
                            }
                          >
                            사용 중지
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn--primary btn--small"
                            data-testid={`endpoint-enable-${ep.id}`}
                            disabled={actionBusy || refreshing}
                            onClick={() =>
                              void enabledAction.open(ep.id, 'enable')
                            }
                          >
                            다시 사용
                          </button>
                        )}
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
            onPageChange={(nextPage) =>
              syncUrl({
                q,
                apiType,
                isEnabled,
                trafficState,
                page: nextPage,
              })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 API 연결이 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          검색/필터 조건에 해당하는 API 연결이 없습니다.
        </p>
      ) : null}

      {enabledAction.isDialogOpen && enabledAction.mode ? (
        <EndpointEnabledConfirmDialog
          mode={enabledAction.mode}
          endpoint={enabledAction.draft}
          refreshing={enabledAction.refreshing}
          submitting={enabledAction.submitting}
          error={enabledAction.error}
          consent={enabledAction.consent}
          onConsentChange={enabledAction.setConsent}
          aliasInput={enabledAction.aliasInput}
          onAliasInputChange={enabledAction.setAliasInput}
          onCancel={enabledAction.close}
          onConfirm={() => void enabledAction.confirm()}
        />
      ) : null}
    </AppShell>
  )
}
