import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getModel, listModelVersions } from '../api/models'
import type { ModelDetail, ModelVersion } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { formatApiDateTime } from '../utils/date'
import { formatInt, formatMemoryMb } from '../utils/number'
import { parsePositivePage } from '../utils/query'
import {
  MODEL_DETAIL_FIELD_HELP,
  MODEL_TYPE_HELP,
  userFacingLoadError,
} from '../utils/uiLabels'

const PAGE_SIZE = 20

function parseIncludeArchived(raw: string | null): boolean {
  return raw === 'true'
}

export function ModelDetailPage() {
  const { modelId = '' } = useParams<{ modelId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const includeArchived = parseIncludeArchived(
    searchParams.get('include_archived'),
  )
  const page = parsePositivePage(searchParams.get('page'))

  const [model, setModel] = useState<ModelDetail | null>(null)
  const [versions, setVersions] = useState<ModelVersion[] | null>(null)
  const [versionTotal, setVersionTotal] = useState(0)
  const [modelLoading, setModelLoading] = useState(true)
  const [versionsLoading, setVersionsLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [modelNotFound, setModelNotFound] = useState(false)
  const [versionsError, setVersionsError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)

  const syncUrl = useCallback(
    (next: { includeArchived: boolean; page: number }) => {
      const params = new URLSearchParams()
      if (next.includeArchived) params.set('include_archived', 'true')
      if (next.page > 1) params.set('page', String(next.page))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const rawPage = searchParams.get('page')
    const rawArchived = searchParams.get('include_archived')
    let needsFix = false
    const fixed = new URLSearchParams()

    if (rawArchived === 'true') {
      fixed.set('include_archived', 'true')
    } else if (rawArchived === 'false') {
      // Default false → omit from canonical URL
      needsFix = true
    } else if (rawArchived !== null && rawArchived !== '') {
      needsFix = true
    }

    const parsedPage = parsePositivePage(rawPage)
    if (rawPage !== null && String(parsedPage) !== rawPage) needsFix = true
    if (parsedPage > 1) fixed.set('page', String(parsedPage))

    if (needsFix) setSearchParams(fixed, { replace: true })
  }, [searchParams, setSearchParams])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!modelId) {
        setModelNotFound(true)
        setModelLoading(false)
        setVersionsLoading(false)
        setModelError('Model을 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial') {
        setModelLoading(true)
        setVersionsLoading(true)
      } else {
        setRefreshing(true)
      }
      setModelError(null)
      setVersionsError(null)
      setModelNotFound(false)

      const modelPromise = getModel(modelId, controller.signal)
        .then((data) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          setModel(data)
          setModelNotFound(false)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            // Authoritative not-found: never keep a stale Model as current.
            setModel(null)
            setVersions(null)
            setVersionTotal(0)
            setModelNotFound(true)
            setModelError('Model을 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Model 메타데이터를 불러오지 못했습니다.'
          setModelError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setModelLoading(false)
          }
        })

      const versionsPromise = listModelVersions(modelId, {
        includeArchived,
        page,
        pageSize: PAGE_SIZE,
        signal: controller.signal,
      })
        .then((result) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
          if (page > maxPage) {
            if (result.total === 0) {
              setVersions([])
              setVersionTotal(0)
              setLastUpdated(new Date())
            }
            syncUrl({ includeArchived, page: 1 })
            return
          }
          setVersions(result.items)
          setVersionTotal(result.total)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) return
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Version 목록을 불러오지 못했습니다.'
          setVersionsError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setVersionsLoading(false)
          }
        })

      await Promise.all([modelPromise, versionsPromise])
      if (!controller.signal.aborted && gen === readGenRef.current) {
        setRefreshing(false)
      }
    },
    [modelId, includeArchived, page, syncUrl],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  if (modelNotFound) {
    return (
      <AppShell
        title="등록된 모델 관리"
        description="모델 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/models">
          ← Models / Versions
        </Link>
        <SectionError
          title="Model을 찾을 수 없습니다."
          message="요청한 Model이 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const metaError = modelError
    ? userFacingLoadError('모델 메타데이터', modelError, {
        keptPrevious: model !== null,
      })
    : null
  const versionsLoadError = versionsError
    ? userFacingLoadError('모델 버전 목록', versionsError, {
        keptPrevious: versions !== null,
      })
    : null

  return (
    <AppShell
      title={model?.name ?? '모델 상세'}
      description="레지스트리 모델 정의와 버전 이력입니다. Version runtime_config는 저장 정의값이며 Deployment/observed 실행값과 다를 수 있습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <Link className="back-link" to="/models">
        ← Models / Versions
      </Link>

      <p className="panel-hint" role="note">
        {MODEL_DETAIL_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="모델 vs 버전">
            {MODEL_DETAIL_FIELD_HELP.modelVsVersion}
          </HelpHint>
          <HelpHint showTopic label="등록 상태">
            {MODEL_DETAIL_FIELD_HELP.registration}
          </HelpHint>
          <HelpHint showTopic label="버전 보관 (Archived)">
            {MODEL_DETAIL_FIELD_HELP.archived}
          </HelpHint>
          <HelpHint showTopic label="예상 자원">
            {MODEL_DETAIL_FIELD_HELP.expectedResources}
          </HelpHint>
          <HelpHint showTopic label="모델 유형">
            {MODEL_TYPE_HELP.LLM} {MODEL_TYPE_HELP.VLM}{' '}
            {MODEL_TYPE_HELP.EMBEDDING}
          </HelpHint>
        </div>
      </details>

      {modelLoading && !model ? (
        <LoadingBlock label="모델 메타데이터를 불러오는 중…" />
      ) : null}

      {metaError && !modelNotFound ? (
        <SectionError
          title={metaError.title}
          message={metaError.message}
          action={metaError.action}
        />
      ) : null}

      {model ? (
        <section className="detail-panel" aria-labelledby="model-meta-heading">
          <div className="detail-panel__header">
            <h2 id="model-meta-heading">{model.name}</h2>
            <ActiveBadge active={model.is_active} />
          </div>
          <dl className="meta-grid">
            <div>
              <dt>
                <TermLabel text="식별자" apiTerm="Slug" />
              </dt>
              <dd className="mono">{model.slug}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="모델 유형"
                  apiTerm="Model Type"
                  help={`${MODEL_TYPE_HELP.LLM} ${MODEL_TYPE_HELP.VLM} ${MODEL_TYPE_HELP.EMBEDDING}`}
                />
              </dt>
              <dd>{model.model_type || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="제공자" apiTerm="Provider" />
              </dt>
              <dd>{model.provider || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="모델 출처" apiTerm="Source Type" />
              </dt>
              <dd>{model.source_type || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="라이선스" apiTerm="License" />
              </dt>
              <dd>{model.license_name || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="생성 시각" apiTerm="Created" />
              </dt>
              <dd>{formatApiDateTime(model.created_at)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel text="수정 시각" apiTerm="Updated" />
              </dt>
              <dd>{formatApiDateTime(model.updated_at)}</dd>
            </div>
          </dl>
          <div className="description-block">
            <h3 className="section-subheading">모델 설명 (Description)</h3>
            <p className="description-text">
              {model.description?.trim() ? model.description : '—'}
            </p>
          </div>
        </section>
      ) : null}

      <section className="detail-panel" aria-labelledby="versions-heading">
        <div className="detail-panel__header">
          <h2 id="versions-heading">모델 버전 이력 (Versions)</h2>
          <label className="checkbox-label" htmlFor="include-archived">
            <input
              id="include-archived"
              type="checkbox"
              checked={includeArchived}
              onChange={(e) =>
                syncUrl({ includeArchived: e.target.checked, page: 1 })
              }
            />
            Archived 포함
          </label>
        </div>
        <p className="panel-hint" role="note">
          {MODEL_DETAIL_FIELD_HELP.versions}{' '}
          {MODEL_DETAIL_FIELD_HELP.archived}
        </p>

        {versionsLoadError ? (
          <SectionError
            title={versionsLoadError.title}
            message={versionsLoadError.message}
            action={versionsLoadError.action}
          />
        ) : null}

        {versionsLoading && versions === null ? (
          <LoadingBlock label="버전 목록을 불러오는 중…" />
        ) : null}

        {versions !== null && versions.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">
                      <TermLabel text="버전" apiTerm="Version" />
                    </th>
                    <th scope="col">
                      <TermLabel text="실행 환경" apiTerm="Runtime" />
                    </th>
                    <th scope="col">
                      <TermLabel text="양자화" apiTerm="Quantization" />
                    </th>
                    <th scope="col">
                      <TermLabel text="연산 형식" apiTerm="DType" />
                    </th>
                    <th scope="col">
                      <TermLabel
                        text="예상 최대 GPU 메모리"
                        apiTerm="Expected Peak VRAM"
                        help={MODEL_DETAIL_FIELD_HELP.expectedResources}
                      />
                    </th>
                    <th scope="col">
                      <TermLabel
                        text="최대 문맥 길이"
                        apiTerm="Max Model Len"
                      />
                    </th>
                    <th scope="col">
                      <TermLabel
                        text="보관된 버전"
                        apiTerm="Archived"
                        help={MODEL_DETAIL_FIELD_HELP.archived}
                      />
                    </th>
                    <th scope="col">
                      <TermLabel text="수정 시각" apiTerm="Updated" />
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {versions.map((v) => (
                    <tr key={v.id}>
                      <td>
                        <Link
                          className="table-link"
                          to={`/model-versions/${v.id}`}
                        >
                          {v.version_label}
                        </Link>
                      </td>
                      <td>{v.runtime_type || '—'}</td>
                      <td>{v.quantization || '—'}</td>
                      <td>{v.dtype || '—'}</td>
                      <td>{formatMemoryMb(v.expected_peak_vram_mb)}</td>
                      <td>{formatInt(v.default_max_model_len)}</td>
                      <td>
                        {v.archived_at
                          ? formatApiDateTime(v.archived_at)
                          : 'Active'}
                      </td>
                      <td>{formatApiDateTime(v.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={page}
              pageSize={PAGE_SIZE}
              total={versionTotal}
              onPageChange={(next) =>
                syncUrl({ includeArchived, page: next })
              }
              disabled={refreshing}
            />
          </>
        ) : null}

        {versions !== null && versions.length === 0 && !includeArchived ? (
          <p className="empty-state" role="status">
            표시할 활성 Model Version이 없습니다. Archived 포함을 켜면 보관된
            Version도 확인할 수 있습니다.
          </p>
        ) : null}

        {versions !== null && versions.length === 0 && includeArchived ? (
          <p className="empty-state" role="status">
            등록된 Model Version이 없습니다.
          </p>
        ) : null}
      </section>
    </AppShell>
  )
}
