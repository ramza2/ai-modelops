import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getModel,
  getModelVersion,
  listModelArtifacts,
} from '../api/models'
import type {
  ModelArtifact,
  ModelDetail,
  ModelVersion,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { Pagination } from '../components/Pagination'
import { SectionError } from '../components/SectionError'
import { TruncatedValue } from '../components/TruncatedValue'
import { formatApiDateTime, shortId } from '../utils/date'
import {
  abbreviateMiddle,
  formatBytes,
  pickRuntimeConfigEntries,
  safeRuntimeConfigValue,
} from '../utils/format'
import { formatInt, formatMemoryMb } from '../utils/number'
import { parsePositivePage } from '../utils/query'

const PAGE_SIZE = 20

export function ModelVersionDetailPage() {
  const { versionId = '' } = useParams<{ versionId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const artifactPage = parsePositivePage(searchParams.get('artifact_page'))

  const [version, setVersion] = useState<ModelVersion | null>(null)
  const [parentModel, setParentModel] = useState<ModelDetail | null>(null)
  const [artifacts, setArtifacts] = useState<ModelArtifact[] | null>(null)
  const [artifactTotal, setArtifactTotal] = useState(0)

  const [versionLoading, setVersionLoading] = useState(true)
  const [parentLoading, setParentLoading] = useState(false)
  const [artifactsLoading, setArtifactsLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const [versionError, setVersionError] = useState<string | null>(null)
  const [versionNotFound, setVersionNotFound] = useState(false)
  const [parentWarning, setParentWarning] = useState<string | null>(null)
  const [artifactsError, setArtifactsError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const parentAbortRef = useRef<AbortController | null>(null)

  const syncArtifactPage = useCallback(
    (nextPage: number) => {
      const params = new URLSearchParams()
      if (nextPage > 1) params.set('artifact_page', String(nextPage))
      setSearchParams(params, { replace: true })
    },
    [setSearchParams],
  )

  useEffect(() => {
    const raw = searchParams.get('artifact_page')
    const parsed = parsePositivePage(raw)
    if (raw !== null && String(parsed) !== raw) {
      const params = new URLSearchParams()
      if (parsed > 1) params.set('artifact_page', String(parsed))
      setSearchParams(params, { replace: true })
    }
  }, [searchParams, setSearchParams])

  const loadParent = useCallback(async (modelId: string, gen: number) => {
    parentAbortRef.current?.abort()
    const controller = new AbortController()
    parentAbortRef.current = controller
    setParentLoading(true)
    setParentWarning(null)
    try {
      const model = await getModel(modelId, controller.signal)
      if (controller.signal.aborted || gen !== readGenRef.current) return
      setParentModel(model)
      setLastUpdated(new Date())
    } catch (err) {
      if (controller.signal.aborted || gen !== readGenRef.current) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : '부모 Model을 불러오지 못했습니다.'
      setParentWarning(message)
    } finally {
      if (!controller.signal.aborted && gen === readGenRef.current) {
        setParentLoading(false)
      }
    }
  }, [])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (!versionId) {
        setVersionNotFound(true)
        setVersionLoading(false)
        setArtifactsLoading(false)
        setVersionError('Model Version을 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current

      if (mode === 'initial') {
        setVersionLoading(true)
        setArtifactsLoading(true)
      } else {
        setRefreshing(true)
      }
      setVersionError(null)
      setArtifactsError(null)
      setVersionNotFound(false)
      setParentWarning(null)

      const versionPromise = getModelVersion(versionId, controller.signal)
        .then(async (data) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          setVersion(data)
          setVersionNotFound(false)
          setLastUpdated(new Date())
          await loadParent(data.model_id, gen)
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            // Authoritative not-found: never keep a stale Version as current.
            setVersion(null)
            setParentModel(null)
            setArtifacts(null)
            setArtifactTotal(0)
            setVersionNotFound(true)
            setVersionError('Model Version을 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Model Version을 불러오지 못했습니다.'
          setVersionError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setVersionLoading(false)
          }
        })

      const artifactsPromise = listModelArtifacts(versionId, {
        page: artifactPage,
        pageSize: PAGE_SIZE,
        signal: controller.signal,
      })
        .then((result) => {
          if (controller.signal.aborted || gen !== readGenRef.current) return
          const maxPage = Math.max(1, Math.ceil(result.total / PAGE_SIZE) || 1)
          if (artifactPage > maxPage) {
            if (result.total === 0) {
              setArtifacts([])
              setArtifactTotal(0)
              setLastUpdated(new Date())
            }
            syncArtifactPage(1)
            return
          }
          setArtifacts(result.items)
          setArtifactTotal(result.total)
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
                : 'Artifact 목록을 불러오지 못했습니다.'
          setArtifactsError(message)
        })
        .finally(() => {
          if (!controller.signal.aborted && gen === readGenRef.current) {
            setArtifactsLoading(false)
          }
        })

      await Promise.all([versionPromise, artifactsPromise])
      if (!controller.signal.aborted && gen === readGenRef.current) {
        setRefreshing(false)
      }
    },
    [versionId, artifactPage, loadParent, syncArtifactPage],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
      parentAbortRef.current?.abort()
    }
  }, [load])

  if (versionNotFound) {
    return (
      <AppShell
        title="Models / Versions"
        description="Model Version 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/models">
          ← Models / Versions
        </Link>
        <SectionError
          title="Model Version을 찾을 수 없습니다."
          message="요청한 Version이 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const runtimeEntries = pickRuntimeConfigEntries(version?.runtime_config)

  return (
    <AppShell
      title={version?.version_label ?? 'Model Version 상세'}
      description="Model Version에 저장된 Registry 정의값입니다. Deployment override 및 실제 실행 observed 값은 Deployment Capacity Profile에서 확인합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <nav className="breadcrumb" aria-label="경로">
        <Link to="/models">Models / Versions</Link>
        <span aria-hidden="true"> → </span>
        {parentModel ? (
          <Link to={`/models/${parentModel.id}`}>{parentModel.name}</Link>
        ) : version ? (
          <Link to={`/models/${version.model_id}`}>
            Model {shortId(version.model_id)}
          </Link>
        ) : (
          <span>Model</span>
        )}
        <span aria-hidden="true"> → </span>
        <span>{version?.version_label ?? 'Version'}</span>
      </nav>

      <div className="back-row">
        {version ? (
          <Link className="back-link" to={`/models/${version.model_id}`}>
            ← Model
          </Link>
        ) : null}
        <Link className="back-link" to="/models">
          ← Models / Versions
        </Link>
      </div>

      {parentWarning ? (
        <SectionError title="부모 Model 경고" message={parentWarning} />
      ) : null}

      {versionLoading && !version ? (
        <LoadingBlock label="Model Version을 불러오는 중…" />
      ) : null}

      {versionError && !versionNotFound ? (
        <SectionError
          title={
            version ? 'Version 새로고침 실패' : 'Model Version 오류'
          }
          message={
            version
              ? `기존 Version 정보를 표시하고 있습니다. 새로고침 실패: ${versionError}`
              : versionError
          }
        />
      ) : null}

      {version ? (
        <>
          <section
            className="detail-panel"
            aria-labelledby="version-identity-heading"
          >
            <div className="detail-panel__header">
              <h2 id="version-identity-heading">{version.version_label}</h2>
              <span
                className={`status-badge status-badge--${version.archived_at ? 'muted' : 'ok'}`}
              >
                <span className="status-badge__dot" aria-hidden="true" />
                <span className="status-badge__text">
                  {version.archived_at ? 'Archived' : 'Active Version'}
                </span>
              </span>
            </div>
            <dl className="meta-grid">
              <div>
                <dt>Runtime Type</dt>
                <dd>{version.runtime_type || '—'}</dd>
              </div>
              <div>
                <dt>Served Model Name</dt>
                <dd className="mono">{version.served_model_name || '—'}</dd>
              </div>
              <div>
                <dt>Quantization</dt>
                <dd>{version.quantization || '—'}</dd>
              </div>
              <div>
                <dt>DType</dt>
                <dd>{version.dtype || '—'}</dd>
              </div>
              <div>
                <dt>Source Repository</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={version.source_repository}
                    abbreviated={abbreviateMiddle(
                      version.source_repository,
                      24,
                      12,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Source Revision</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={version.source_revision}
                    abbreviated={abbreviateMiddle(
                      version.source_revision,
                      10,
                      6,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Runtime Image</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={version.runtime_image}
                    abbreviated={abbreviateMiddle(
                      version.runtime_image,
                      28,
                      12,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Runtime Image Digest</dt>
                <dd>
                  <TruncatedValue
                    className="mono"
                    value={version.runtime_image_digest}
                    abbreviated={abbreviateMiddle(
                      version.runtime_image_digest,
                      12,
                      8,
                    )}
                  />
                </dd>
              </div>
              <div>
                <dt>Archived At</dt>
                <dd>
                  {version.archived_at
                    ? formatApiDateTime(version.archived_at)
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{formatApiDateTime(version.created_at)}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{formatApiDateTime(version.updated_at)}</dd>
              </div>
            </dl>
            {parentLoading ? (
              <p className="metric-line">부모 Model 메타데이터 로드 중…</p>
            ) : null}
          </section>

          <section
            className="detail-panel"
            aria-labelledby="expected-resources-heading"
          >
            <h2 id="expected-resources-heading">Expected Resources</h2>
            <p className="panel-hint">
              Version에 저장된 예상 자원 정의입니다. 현재 GPU 가용성이나
              Deployment Preflight 판정을 의미하지 않습니다.
            </p>
            <dl className="meta-grid">
              <div>
                <dt>Expected Idle VRAM</dt>
                <dd>{formatMemoryMb(version.expected_idle_vram_mb)}</dd>
              </div>
              <div>
                <dt>Expected Peak VRAM</dt>
                <dd>{formatMemoryMb(version.expected_peak_vram_mb)}</dd>
              </div>
              <div>
                <dt>Default Max Model Len</dt>
                <dd>{formatInt(version.default_max_model_len)}</dd>
              </div>
            </dl>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="runtime-config-heading"
          >
            <h2 id="runtime-config-heading">Version Runtime Configuration</h2>
            <p className="panel-hint">
              이 값은 Model Version에 저장된 정의값입니다. Deployment의
              deployment_config가 일부 값을 override할 수 있으며, 실제 실행
              컨테이너의 observed 값과도 다를 수 있습니다. 실행 적용 여부는
              Deployment Capacity Profile에서 확인합니다.
            </p>
            <dl className="meta-grid">
              {runtimeEntries.known.map(({ key, value }) => (
                <div key={key}>
                  <dt>
                    runtime_config.{key}
                    {key === 'scheduling_policy'
                      ? ' (정의값)'
                      : key === 'max_num_seqs'
                        ? ' (정의값)'
                        : ''}
                  </dt>
                  <dd className="mono">{safeRuntimeConfigValue(value)}</dd>
                </div>
              ))}
            </dl>
            {runtimeEntries.known.length === 0 ? (
              <p className="empty-state" role="status">
                표시할 알려진 runtime_config 항목이 없습니다.
              </p>
            ) : null}
            {runtimeEntries.otherCount > 0 ? (
              <p className="metric-line">
                기타 설정 {runtimeEntries.otherCount}개
              </p>
            ) : null}
          </section>
        </>
      ) : null}

      <section className="detail-panel" aria-labelledby="artifacts-heading">
        <h2 id="artifacts-heading">Artifacts</h2>
        <p className="panel-hint">
          Artifact Registry 메타데이터입니다. Node Model Cache 준비 상태와는
          별개입니다.
        </p>

        {artifactsError ? (
          <SectionError title="Artifact 목록 오류" message={artifactsError} />
        ) : null}

        {artifactsLoading && artifacts === null ? (
          <LoadingBlock label="Artifact 목록을 불러오는 중…" />
        ) : null}

        {artifacts !== null && artifacts.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">Type</th>
                    <th scope="col">Source URI</th>
                    <th scope="col">Revision</th>
                    <th scope="col">Size</th>
                    <th scope="col">Checksum</th>
                    <th scope="col">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {artifacts.map((a) => (
                    <tr key={a.id}>
                      <td>{a.artifact_type || '—'}</td>
                      <td>
                        <TruncatedValue
                          className="mono"
                          value={a.source_uri}
                          abbreviated={abbreviateMiddle(a.source_uri, 28, 12)}
                        />
                      </td>
                      <td>
                        <TruncatedValue
                          className="mono"
                          value={a.revision}
                          abbreviated={abbreviateMiddle(a.revision, 10, 6)}
                        />
                      </td>
                      <td>{formatBytes(a.size_bytes)}</td>
                      <td>
                        <TruncatedValue
                          className="mono"
                          value={a.checksum}
                          abbreviated={abbreviateMiddle(a.checksum, 8, 4)}
                        />
                      </td>
                      <td>{formatApiDateTime(a.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={artifactPage}
              pageSize={PAGE_SIZE}
              total={artifactTotal}
              onPageChange={syncArtifactPage}
              disabled={refreshing}
            />
          </>
        ) : null}

        {artifacts !== null && artifacts.length === 0 ? (
          <p className="empty-state" role="status">
            등록된 Artifact가 없습니다.
          </p>
        ) : null}
      </section>
    </AppShell>
  )
}
