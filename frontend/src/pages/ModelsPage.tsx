import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listModels } from '../api/models'
import type { ModelSummary } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { formatApiDateTime } from '../utils/date'
import { parsePositivePage } from '../utils/query'
import { MODEL_TYPE_HELP, WIZARD_FIELD_HELP } from '../utils/uiLabels'

const PAGE_SIZE = 20
const KNOWN_TYPES = new Set(['LLM', 'VLM', 'EMBEDDING'])

function parseModelType(raw: string | null): string | null {
  if (!raw) return null
  const upper = raw.toUpperCase()
  if (upper === 'ALL') return null
  if (!KNOWN_TYPES.has(upper)) return null
  return upper
}

/** Strict active query: only lowercase true/false canonical. */
export function parseActiveQuery(raw: string | null): boolean | null {
  if (!raw) return null
  const lower = raw.toLowerCase()
  if (lower === 'true') return true
  if (lower === 'false') return false
  return null
}

function activeLabel(value: boolean | null): string {
  if (value === true) return 'ACTIVE'
  if (value === false) return 'INACTIVE'
  return 'ALL'
}

export function ModelsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const modelType = parseModelType(searchParams.get('model_type'))
  const isActive = parseActiveQuery(searchParams.get('active'))
  const q = searchParams.get('q') ?? ''
  const provider = searchParams.get('provider') ?? ''
  const page = parsePositivePage(searchParams.get('page'))

  const [draftQ, setDraftQ] = useState(q)
  const [draftProvider, setDraftProvider] = useState(provider)

  const [items, setItems] = useState<ModelSummary[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const hasLoadedRef = useRef(false)
  const readGenRef = useRef(0)

  useEffect(() => {
    setDraftQ(q)
    setDraftProvider(provider)
  }, [q, provider])

  const syncUrl = useCallback(
    (next: {
      q: string
      provider: string
      modelType: string | null
      isActive: boolean | null
      page: number
    }) => {
      const params = new URLSearchParams()
      const trimmedQ = next.q.trim()
      const trimmedProvider = next.provider.trim()
      if (trimmedQ) params.set('q', trimmedQ)
      if (trimmedProvider) params.set('provider', trimmedProvider)
      if (next.modelType) params.set('model_type', next.modelType)
      if (next.isActive === true) params.set('active', 'true')
      if (next.isActive === false) params.set('active', 'false')
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  // Canonicalize invalid URL query values.
  useEffect(() => {
    const rawType = searchParams.get('model_type')
    const rawActive = searchParams.get('active')
    const rawPage = searchParams.get('page')
    let needsFix = false
    const fixed = new URLSearchParams()

    const parsedType = parseModelType(rawType)
    if (rawType) {
      if (rawType.toUpperCase() === 'ALL') {
        // Default ALL → omit model_type
        needsFix = true
      } else if (parsedType === null) {
        needsFix = true
      } else {
        fixed.set('model_type', parsedType)
        if (rawType !== parsedType) {
          needsFix = true
        }
      }
    }

    const parsedActive = parseActiveQuery(rawActive)
    if (rawActive !== null && rawActive !== '') {
      if (parsedActive === null) {
        needsFix = true
      } else {
        const canonical = parsedActive ? 'true' : 'false'
        if (rawActive !== canonical) needsFix = true
        fixed.set('active', canonical)
      }
    }

    if (searchParams.get('q')) fixed.set('q', searchParams.get('q')!)
    if (searchParams.get('provider')) {
      fixed.set('provider', searchParams.get('provider')!)
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
      const gen = ++readGenRef.current

      if (mode === 'initial' && !hasLoadedRef.current) {
        setLoading(true)
      } else {
        setRefreshing(true)
      }
      setError(null)

      try {
        const result = await listModels({
          q: q.trim() || null,
          provider: provider.trim() || null,
          modelType,
          isActive,
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
            q,
            provider,
            modelType,
            isActive,
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
              : '모델 목록을 불러오지 못했습니다.'
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
    [q, provider, modelType, isActive, page, syncUrl],
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
      provider: draftProvider,
      modelType,
      isActive,
      page: 1,
    })
  }

  const onClearSearch = () => {
    setDraftQ('')
    setDraftProvider('')
    syncUrl({
      q: '',
      provider: '',
      modelType: null,
      isActive: null,
      page: 1,
    })
  }

  const emptyFiltered =
    items !== null &&
    items.length === 0 &&
    (modelType !== null ||
      isActive !== null ||
      q.trim() !== '' ||
      provider.trim() !== '')
  const emptyDatabase =
    items !== null &&
    items.length === 0 &&
    modelType === null &&
    isActive === null &&
    q.trim() === '' &&
    provider.trim() === ''

  return (
    <AppShell
      title="등록된 모델 관리"
      description="레지스트리에 등록된 모델 정의와 버전을 조회합니다. 지금 서버에서 실행 중인 모델(Deployment) 목록과 다릅니다. Version runtime_config는 저장 정의값이며 Deployment/observed 실행값과 다를 수 있습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <p className="panel-hint" role="note">
        {WIZARD_FIELD_HELP.registryList}
      </p>

      <div className="toolbar toolbar--wrap" style={{ marginBottom: '0.75rem' }}>
        <Link className="btn btn--primary" to="/models/catalog">
          모델 검색 및 다운로드
        </Link>
        <Link className="btn btn--ghost" to="/models/cache">
          다운로드한 모델 관리
        </Link>
        <span className="secondary-text">
          Hugging Face 검색 · 다운로드 · 배포 설정은 카탈로그/캐시에서 이어집니다
        </span>
      </div>

      <form className="toolbar toolbar--wrap" onSubmit={onSubmitSearch}>
        <label className="toolbar__field" htmlFor="model-search-q">
          <span>검색</span>
          <input
            id="model-search-q"
            type="search"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            placeholder="slug / 이름 / 설명"
          />
        </label>
        <label className="toolbar__field" htmlFor="model-search-provider">
          <span>
            제공자 <span className="term-label__api">(Provider)</span>
          </span>
          <input
            id="model-search-provider"
            type="text"
            value={draftProvider}
            onChange={(e) => setDraftProvider(e.target.value)}
            placeholder="제공자(정확 일치)"
          />
        </label>
        <div className="toolbar__field">
          <div className="toolbar__field-head">
            <label htmlFor="model-type-filter">
              모델 유형 <span className="term-label__api">(Type)</span>
            </label>
            <HelpHint label="모델 유형">
              {MODEL_TYPE_HELP.LLM} {MODEL_TYPE_HELP.VLM}{' '}
              {MODEL_TYPE_HELP.EMBEDDING}
            </HelpHint>
          </div>
          <select
            id="model-type-filter"
            value={modelType ?? 'ALL'}
            onChange={(e) => {
              const next =
                e.target.value === 'ALL' ? null : e.target.value.toUpperCase()
              syncUrl({
                q,
                provider,
                modelType: next && KNOWN_TYPES.has(next) ? next : null,
                isActive,
                page: 1,
              })
            }}
          >
            <option value="ALL">전체</option>
            <option value="LLM">LLM — 언어 모델</option>
            <option value="VLM">VLM — 비전·언어</option>
            <option value="EMBEDDING">EMBEDDING — 임베딩</option>
          </select>
        </div>
        <label className="toolbar__field" htmlFor="model-active-filter">
          <span>
            등록 상태 <span className="term-label__api">(Active)</span>
          </span>
          <select
            id="model-active-filter"
            value={activeLabel(isActive)}
            onChange={(e) => {
              const v = e.target.value
              const next =
                v === 'ACTIVE' ? true : v === 'INACTIVE' ? false : null
              syncUrl({
                q,
                provider,
                modelType,
                isActive: next,
                page: 1,
              })
            }}
          >
            <option value="ALL">전체</option>
            <option value="ACTIVE">활성 (ACTIVE)</option>
            <option value="INACTIVE">비활성 (INACTIVE)</option>
          </select>
        </label>
        <div className="toolbar__actions">
          <button type="submit" className="btn btn--primary">
            검색
          </button>
          <button type="button" className="btn" onClick={onClearSearch}>
            필터 초기화
          </button>
        </div>
      </form>

      {error ? (
        <SectionError title="모델 목록 오류" message={error} />
      ) : null}

      {loading && items === null ? (
        <LoadingBlock label="모델 목록을 불러오는 중…" />
      ) : null}

      {items !== null && items.length > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <TermLabel text="모델" apiTerm="Model" />
                  </th>
                  <th scope="col">
                    <TermLabel text="모델 유형" apiTerm="Type" />
                  </th>
                  <th scope="col">
                    <TermLabel text="제공자" apiTerm="Provider" />
                  </th>
                  <th scope="col">
                    <TermLabel text="모델 출처" apiTerm="Source" />
                  </th>
                  <th scope="col">
                    <TermLabel text="등록 상태" apiTerm="Active" />
                  </th>
                  <th scope="col">
                    <TermLabel text="라이선스" apiTerm="License" />
                  </th>
                  <th scope="col">
                    <TermLabel text="수정 시각" apiTerm="Updated" />
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((model) => (
                  <tr key={model.id}>
                    <td>
                      <Link className="table-link" to={`/models/${model.id}`}>
                        {model.name}
                      </Link>
                      <div className="secondary-text mono">{model.slug}</div>
                    </td>
                    <td>{model.model_type || '—'}</td>
                    <td>{model.provider || '—'}</td>
                    <td>{model.source_type || '—'}</td>
                    <td>
                      <ActiveBadge active={model.is_active} />
                    </td>
                    <td>{model.license_name || '—'}</td>
                    <td>{formatApiDateTime(model.updated_at)}</td>
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
                q,
                provider,
                modelType,
                isActive,
                page: nextPage,
              })
            }
            disabled={refreshing}
          />
        </>
      ) : null}

      {emptyDatabase ? (
        <p className="empty-state" role="status">
          등록된 모델이 없습니다.
        </p>
      ) : null}

      {emptyFiltered ? (
        <p className="empty-state" role="status">
          검색/필터 조건에 해당하는 모델이 없습니다.
        </p>
      ) : null}
    </AppShell>
  )
}
