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
import { HelpHint, TermLabel } from '../components/HelpHint'
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
import {
  MODEL_VERSION_DETAIL_FIELD_HELP,
  userFacingLoadError,
} from '../utils/uiLabels'

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
        title="등록된 모델 관리"
        description="모델 버전 상세"
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
  const versionLoadError = versionError
    ? userFacingLoadError('모델 버전', versionError, {
        keptPrevious: version !== null,
      })
    : null
  const artifactsLoadError = artifactsError
    ? userFacingLoadError('모델 파일 정보', artifactsError, {
        keptPrevious: artifacts !== null,
      })
    : null
  const parentLoadError = parentWarning
    ? userFacingLoadError('부모 모델', parentWarning, {
        keptPrevious: parentModel !== null,
      })
    : null

  return (
    <AppShell
      title={version?.version_label ?? '모델 버전 상세'}
      description="Model Version에 저장된 정의값(Registry)입니다. Deployment override 및 실제 실행 observed 값은 Deployment Capacity Profile에서 확인합니다."
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
            ← 부모 모델 (Model)
          </Link>
        ) : null}
        <Link className="back-link" to="/models">
          ← Models / Versions
        </Link>
      </div>

      <p className="panel-hint" role="note">
        {MODEL_VERSION_DETAIL_FIELD_HELP.overview}
      </p>
      <details className="help-glossary">
        <summary>용어 및 상태 안내</summary>
        <div className="help-glossary__items">
          <HelpHint showTopic label="버전 실행 설정">
            {MODEL_VERSION_DETAIL_FIELD_HELP.runtimeConfig}
          </HelpHint>
          <HelpHint showTopic label="예상 자원">
            {MODEL_VERSION_DETAIL_FIELD_HELP.expectedResources}
          </HelpHint>
          <HelpHint showTopic label="모델 파일 (Artifacts)">
            {MODEL_VERSION_DETAIL_FIELD_HELP.artifacts}
          </HelpHint>
          <HelpHint showTopic label="보관 (Archived)">
            {MODEL_VERSION_DETAIL_FIELD_HELP.archived}
          </HelpHint>
          <HelpHint showTopic label="원본 식별값">
            {MODEL_VERSION_DETAIL_FIELD_HELP.identifiers}
          </HelpHint>
          <HelpHint showTopic label="부모 모델">
            {MODEL_VERSION_DETAIL_FIELD_HELP.parentModel}
          </HelpHint>
        </div>
      </details>

      {parentLoadError ? (
        <SectionError
          title={parentLoadError.title}
          message={parentLoadError.message}
          action={
            parentModel
              ? parentLoadError.action
              : `${MODEL_VERSION_DETAIL_FIELD_HELP.parentModel} ${parentLoadError.action}`
          }
        />
      ) : null}

      {versionLoading && !version ? (
        <LoadingBlock label="모델 버전을 불러오는 중…" />
      ) : null}

      {versionLoadError && !versionNotFound ? (
        <SectionError
          title={versionLoadError.title}
          message={
            version
              ? `기존 Version 정보를 표시하고 있습니다. ${versionLoadError.message}`
              : versionLoadError.message
          }
          action={versionLoadError.action}
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
                  {version.archived_at
                    ? '보관됨 (Archived)'
                    : '활성 버전 (Active Version)'}
                </span>
              </span>
            </div>
            <dl className="meta-grid">
              <div>
                <dt>
                  <TermLabel
                    text="실행 환경 유형"
                    apiTerm="Runtime Type"
                  />
                </dt>
                <dd>{version.runtime_type || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="모델 실행 이름"
                    apiTerm="Served Model Name"
                    help={MODEL_VERSION_DETAIL_FIELD_HELP.identifiers}
                  />
                </dt>
                <dd className="mono">{version.served_model_name || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="양자화 방식" apiTerm="Quantization" />
                </dt>
                <dd>{version.quantization || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="연산 형식" apiTerm="DType" />
                </dt>
                <dd>{version.dtype || '—'}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="원본 저장소"
                    apiTerm="Source Repository"
                  />
                </dt>
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
                <dt>
                  <TermLabel
                    text="원본 버전"
                    apiTerm="Source Revision"
                  />
                </dt>
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
                <dt>
                  <TermLabel text="실행 이미지" apiTerm="Runtime Image" />
                </dt>
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
                <dt>
                  <TermLabel
                    text="이미지 식별값"
                    apiTerm="Runtime Image Digest"
                    help={MODEL_VERSION_DETAIL_FIELD_HELP.identifiers}
                  />
                </dt>
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
                <dt>
                  <TermLabel
                    text="보관 시각"
                    apiTerm="Archived At"
                    help={MODEL_VERSION_DETAIL_FIELD_HELP.archived}
                  />
                </dt>
                <dd>
                  {version.archived_at
                    ? formatApiDateTime(version.archived_at)
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="생성 시각" apiTerm="Created" />
                </dt>
                <dd>{formatApiDateTime(version.created_at)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel text="수정 시각" apiTerm="Updated" />
                </dt>
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
            <h2 id="expected-resources-heading">
              예상 자원 (Expected Resources)
            </h2>
            <p className="panel-hint" role="note">
              {MODEL_VERSION_DETAIL_FIELD_HELP.expectedResources} Version에
              저장된 예상 자원 정의입니다. 현재 GPU 가용성이나 Deployment
              Preflight 판정을 의미하지 않습니다.
            </p>
            <dl className="meta-grid">
              <div>
                <dt>
                  <TermLabel
                    text="예상 Idle VRAM"
                    apiTerm="Expected Idle VRAM"
                  />
                </dt>
                <dd>{formatMemoryMb(version.expected_idle_vram_mb)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="예상 Peak VRAM"
                    apiTerm="Expected Peak VRAM"
                  />
                </dt>
                <dd>{formatMemoryMb(version.expected_peak_vram_mb)}</dd>
              </div>
              <div>
                <dt>
                  <TermLabel
                    text="기본 최대 문맥 길이"
                    apiTerm="Default Max Model Len"
                  />
                </dt>
                <dd>{formatInt(version.default_max_model_len)}</dd>
              </div>
            </dl>
          </section>

          <section
            className="detail-panel"
            aria-labelledby="runtime-config-heading"
          >
            <h2 id="runtime-config-heading">
              버전 실행 설정 (Version Runtime Configuration)
            </h2>
            <p className="panel-hint" role="note">
              {MODEL_VERSION_DETAIL_FIELD_HELP.runtimeConfig} 실행 적용 여부는
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
        <h2 id="artifacts-heading">모델 파일 정보 (Artifacts)</h2>
        <p className="panel-hint" role="note">
          {MODEL_VERSION_DETAIL_FIELD_HELP.artifacts}
        </p>

        {artifactsLoadError ? (
          <SectionError
            title={artifactsLoadError.title}
            message={artifactsLoadError.message}
            action={artifactsLoadError.action}
          />
        ) : null}

        {artifactsLoading && artifacts === null ? (
          <LoadingBlock label="모델 파일 정보를 불러오는 중…" />
        ) : null}

        {artifacts !== null && artifacts.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">
                      <TermLabel text="유형" apiTerm="Type" />
                    </th>
                    <th scope="col">
                      <TermLabel
                        text="파일 원본 주소"
                        apiTerm="Source URI"
                        help={MODEL_VERSION_DETAIL_FIELD_HELP.identifiers}
                      />
                    </th>
                    <th scope="col">
                      <TermLabel text="개정" apiTerm="Revision" />
                    </th>
                    <th scope="col">
                      <TermLabel text="크기" apiTerm="Size" />
                    </th>
                    <th scope="col">
                      <TermLabel
                        text="무결성 확인값"
                        apiTerm="Checksum"
                        help={MODEL_VERSION_DETAIL_FIELD_HELP.identifiers}
                      />
                    </th>
                    <th scope="col">
                      <TermLabel text="생성 시각" apiTerm="Created" />
                    </th>
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
