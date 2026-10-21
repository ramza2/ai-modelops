import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getClient, getClientRuntimePolicy } from '../api/clients'
import type { ClientApp, ClientRuntimePolicyResponse } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime } from '../utils/date'
import {
  CLIENT_FIELD_HELP,
  formatNullableLimit,
  formatPriorityDisplay,
  userFacingLoadError,
} from '../utils/uiLabels'

export function ClientDetailPage() {
  const { clientId = '' } = useParams<{ clientId: string }>()

  const [client, setClient] = useState<ClientApp | null>(null)
  const [policyResp, setPolicyResp] =
    useState<ClientRuntimePolicyResponse | null>(null)
  const [clientLoading, setClientLoading] = useState(true)
  const [policyLoading, setPolicyLoading] = useState(true)
  const [clientError, setClientError] = useState<string | null>(null)
  const [policyError, setPolicyError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const activeIdRef = useRef(clientId)
  activeIdRef.current = clientId

  const resetIdentity = useCallback(() => {
    setClient(null)
    setPolicyResp(null)
    setClientError(null)
    setPolicyError(null)
    setNotFound(false)
    setLastUpdated(null)
  }, [])

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const requestId = clientId
      if (!requestId) {
        resetIdentity()
        setNotFound(true)
        setClientLoading(false)
        setPolicyLoading(false)
        setClientError('요청한 사용 시스템을 찾을 수 없습니다.')
        return
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      const gen = ++readGenRef.current
      const isCurrent = () =>
        !controller.signal.aborted &&
        gen === readGenRef.current &&
        activeIdRef.current === requestId

      if (mode === 'initial') {
        resetIdentity()
        setClientLoading(true)
        setPolicyLoading(true)
      } else {
        setRefreshing(true)
      }

      const clientPromise = getClient(requestId, controller.signal)
        .then((data) => {
          if (!isCurrent()) return
          if (data.id !== requestId) return
          setClient(data)
          setNotFound(false)
          setClientError(null)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            setClient(null)
            setPolicyResp(null)
            setNotFound(true)
            setClientError('요청한 사용 시스템을 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : '사용 시스템을 불러오지 못했습니다.'
          setClientError(message)
        })
        .finally(() => {
          if (isCurrent()) setClientLoading(false)
        })

      const policyPromise = getClientRuntimePolicy(requestId, controller.signal)
        .then((data) => {
          if (!isCurrent()) return
          if (data.client_id !== requestId) return
          setPolicyResp(data)
          setPolicyError(null)
          setLastUpdated(new Date())
        })
        .catch((err: unknown) => {
          if (!isCurrent()) return
          if (err instanceof DOMException && err.name === 'AbortError') return
          if (err instanceof ApiError && err.status === 404) {
            setPolicyResp(null)
            setPolicyError('런타임 정책을 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : '런타임 정책을 불러오지 못했습니다.'
          setPolicyError(message)
        })
        .finally(() => {
          if (isCurrent()) setPolicyLoading(false)
        })

      await Promise.all([clientPromise, policyPromise])
      if (isCurrent()) setRefreshing(false)
    },
    [clientId, resetIdentity],
  )

  useEffect(() => {
    void load('initial')
    return () => {
      abortRef.current?.abort()
    }
  }, [load])

  const displayClient = client && client.id === clientId ? client : null
  const displayPolicy =
    policyResp && policyResp.client_id === clientId ? policyResp : null

  if (notFound) {
    return (
      <AppShell
        title="AI 사용 시스템"
        description="사용 시스템 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/clients">
          ← AI 사용 시스템 목록
        </Link>
        <SectionError
          title="사용 시스템을 찾을 수 없습니다"
          message="원인: 요청한 ID에 해당하는 등록이 없거나 삭제되었습니다."
          action="목록으로 돌아가 다시 선택하세요. 이 화면에서는 새 사용 시스템을 만들 수 없습니다."
        />
      </AppShell>
    )
  }

  const policy = displayPolicy?.policy ?? null
  const clientErr = clientError
    ? userFacingLoadError('사용 시스템', clientError, {
        keptPrevious: Boolean(displayClient),
      })
    : null
  const policyErr = policyError
    ? userFacingLoadError('런타임 정책', policyError, {
        keptPrevious: Boolean(displayPolicy),
      })
    : null

  return (
    <AppShell
      title={displayClient ? displayClient.display_name : '사용 시스템 상세'}
      description="직원 계정이 아니라 AI 모델을 호출하는 업무 서비스의 등록 정보와 런타임 제한을 조회합니다. 조회 전용입니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <Link className="back-link" to="/clients">
        ← AI 사용 시스템 목록
      </Link>

      <p className="panel-hint" role="note">
        {CLIENT_FIELD_HELP.readOnly}
      </p>

      {clientErr ? (
        <SectionError
          title={clientErr.title}
          message={clientErr.message}
          action={clientErr.action}
        />
      ) : null}

      {clientLoading && !displayClient ? (
        <LoadingBlock label="사용 시스템을 불러오는 중…" />
      ) : null}

      {displayClient ? (
        <section
          className="detail-panel"
          aria-labelledby="client-identity-heading"
        >
          <div className="detail-panel__header">
            <h2 id="client-identity-heading">{displayClient.display_name}</h2>
            <ActiveBadge active={displayClient.is_active} />
          </div>
          <p className="panel-hint">
            등록 상태(ACTIVE/INACTIVE)는 레지스트리 플래그입니다. 실제 Gateway
            호출 가능 여부는 Endpoint Alias·배포·정책에 따라 달라집니다.
          </p>
          <dl className="meta-grid">
            <div>
              <dt>
                <TermLabel
                  text="서비스 키"
                  apiTerm="client_key"
                  help={CLIENT_FIELD_HELP.clientKey}
                />
              </dt>
              <dd className="mono">{displayClient.client_key}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="설명"
                  apiTerm="description"
                  help={CLIENT_FIELD_HELP.description}
                />
              </dt>
              <dd>{displayClient.description || '—'}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="등록 시각"
                  apiTerm="created_at"
                  help={CLIENT_FIELD_HELP.createdAt}
                />
              </dt>
              <dd>{formatApiDateTime(displayClient.created_at)}</dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="수정 시각"
                  apiTerm="updated_at"
                  help={CLIENT_FIELD_HELP.updatedAt}
                />
              </dt>
              <dd>{formatApiDateTime(displayClient.updated_at)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      <section
        className="detail-panel"
        aria-labelledby="client-policy-heading"
      >
        <div className="detail-panel__heading">
          <h2 id="client-policy-heading">런타임 정책</h2>
          <span className="term-label__api">(Runtime Policy)</span>
          <HelpHint label="런타임 정책">
            {CLIENT_FIELD_HELP.runtimePolicy}
          </HelpHint>
        </div>
        <p className="panel-hint">{CLIENT_FIELD_HELP.runtimePolicy}</p>
        {policyErr ? (
          <SectionError
            title={policyErr.title}
            message={policyErr.message}
            action={policyErr.action}
          />
        ) : null}
        {policyLoading && !displayPolicy ? (
          <LoadingBlock label="런타임 정책을 불러오는 중…" />
        ) : null}
        {displayPolicy && policy === null ? (
          <p className="empty-state" role="status">
            {CLIENT_FIELD_HELP.policyNull}
          </p>
        ) : null}
        {policy ? (
          <dl className="meta-grid">
            <div>
              <dt>
                <TermLabel
                  text="정책 사용"
                  apiTerm="is_enabled"
                  help={CLIENT_FIELD_HELP.policyEnabled}
                />
              </dt>
              <dd>
                <StatusBadge
                  status={policy.is_enabled ? 'ACTIVE' : 'INACTIVE'}
                  label={policy.is_enabled ? '사용 중' : '중지됨'}
                />
              </dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="입력 토큰 상한"
                  apiTerm="max_input_tokens"
                  help={CLIENT_FIELD_HELP.maxInputTokens}
                />
              </dt>
              <dd>
                {formatNullableLimit(policy.max_input_tokens).display}
                <span className="term-label__api">
                  {' '}
                  ({formatNullableLimit(policy.max_input_tokens).apiHint})
                </span>
              </dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="출력 토큰 상한"
                  apiTerm="max_output_tokens"
                  help={CLIENT_FIELD_HELP.maxOutputTokens}
                />
              </dt>
              <dd>
                {formatNullableLimit(policy.max_output_tokens).display}
                <span className="term-label__api">
                  {' '}
                  ({formatNullableLimit(policy.max_output_tokens).apiHint})
                </span>
              </dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="동시 요청 상한"
                  apiTerm="max_concurrent_requests"
                  help={CLIENT_FIELD_HELP.maxConcurrentRequests}
                />
              </dt>
              <dd>
                {formatNullableLimit(policy.max_concurrent_requests).display}
                <span className="term-label__api">
                  {' '}
                  (
                  {
                    formatNullableLimit(policy.max_concurrent_requests)
                      .apiHint
                  }
                  )
                </span>
              </dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="우선순위"
                  apiTerm="priority"
                  help={CLIENT_FIELD_HELP.priority}
                />
              </dt>
              <dd>
                {formatPriorityDisplay(policy.priority).display}
                <span className="term-label__api">
                  {' '}
                  ({formatPriorityDisplay(policy.priority).apiHint})
                </span>
              </dd>
            </div>
            <div>
              <dt>
                <TermLabel
                  text="정책 수정 시각"
                  apiTerm="updated_at"
                  help={CLIENT_FIELD_HELP.updatedAt}
                />
              </dt>
              <dd>{formatApiDateTime(policy.updated_at)}</dd>
            </div>
          </dl>
        ) : null}
      </section>
    </AppShell>
  )
}
