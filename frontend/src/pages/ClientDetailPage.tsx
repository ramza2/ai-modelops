import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getClient,
  getClientRuntimePolicy,
  updateClient,
} from '../api/clients'
import type { ClientApp, ClientRuntimePolicyResponse } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { ClientEditDialog } from '../components/ClientEditDialog'
import { ClientKeyCopyActions } from '../components/ClientKeyCopyActions'
import { ClientRuntimePolicyDialog } from '../components/ClientRuntimePolicyDialog'
import { DangerConfirmDialog } from '../components/DangerConfirmDialog'
import { HelpHint, TermLabel } from '../components/HelpHint'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime } from '../utils/date'
import {
  createIdempotencyKey,
  mutationPayloadFingerprint,
} from '../utils/idempotencyKey'
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
  const [editOpen, setEditOpen] = useState(false)
  const [policyEditOpen, setPolicyEditOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [activeConfirm, setActiveConfirm] = useState<'deactivate' | 'reactivate' | null>(
    null,
  )
  const [consent, setConsent] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [revalidating, setRevalidating] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const readGenRef = useRef(0)
  const activeIdRef = useRef(clientId)
  activeIdRef.current = clientId
  const activeIdemKeyRef = useRef<string | null>(null)
  const activeFpRef = useRef<string | null>(null)
  const actionLockRef = useRef(false)

  const resetIdentity = useCallback(() => {
    setClient(null)
    setPolicyResp(null)
    setClientError(null)
    setPolicyError(null)
    setNotFound(false)
    setLastUpdated(null)
    setEditOpen(false)
    setPolicyEditOpen(false)
    setActiveConfirm(null)
    setConsent(false)
    setActionError(null)
    activeIdemKeyRef.current = null
    activeFpRef.current = null
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

  const closeActiveConfirm = () => {
    if (actionBusy || revalidating) return
    setActiveConfirm(null)
    setConsent(false)
  }

  const runActiveToggle = async () => {
    if (!displayClient || !activeConfirm || actionLockRef.current) return
    const targetActive = activeConfirm === 'reactivate'
    const fp = mutationPayloadFingerprint({
      client_id: displayClient.id,
      is_active: targetActive,
    })
    let idemKey: string
    try {
      if (activeIdemKeyRef.current && activeFpRef.current === fp) {
        idemKey = activeIdemKeyRef.current
      } else {
        idemKey = createIdempotencyKey()
        activeIdemKeyRef.current = idemKey
        activeFpRef.current = fp
      }
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : 'Idempotency-Key를 만들 수 없습니다. 저장을 중단했습니다.',
      )
      return
    }

    actionLockRef.current = true
    setActionBusy(true)
    setRevalidating(true)
    setActionError(null)
    const lockedId = displayClient.id
    const lockedKey = displayClient.client_key
    const lockedUpdatedAt = displayClient.updated_at
    const lockedActive = displayClient.is_active
    try {
      const latest = await getClient(lockedId)
      if (activeIdRef.current !== lockedId) return
      if (latest.id !== lockedId || latest.client_key !== lockedKey) {
        setActionError(
          '대상 사용 시스템이 변경되었습니다. 저장을 중단했습니다. 화면을 새로고침하세요.',
        )
        return
      }
      if (latest.is_active === targetActive) {
        setActionError(
          `이미 ${targetActive ? '활성' : '비활성'} 상태입니다. 중복 변경을 보내지 않습니다.`,
        )
        setClient(latest)
        setActiveConfirm(null)
        setConsent(false)
        activeIdemKeyRef.current = null
        activeFpRef.current = null
        return
      }
      if (latest.updated_at !== lockedUpdatedAt || latest.is_active !== lockedActive) {
        setActionError(
          '표시된 등록 상태와 최신 상태가 다릅니다. 최신 데이터를 다시 확인한 뒤 진행하세요. Frontend 검증은 동시 수정의 완전한 방지책이 아닙니다.',
        )
        setClient(latest)
        setConsent(false)
        activeIdemKeyRef.current = null
        activeFpRef.current = null
        return
      }
      setRevalidating(false)
      const saved = await updateClient(
        lockedId,
        { is_active: targetActive },
        { idempotencyKey: idemKey },
      )
      if (activeIdRef.current !== lockedId) return
      setClient(saved)
      setLastUpdated(new Date())
      setActiveConfirm(null)
      setConsent(false)
      activeIdemKeyRef.current = null
      activeFpRef.current = null
    } catch (err) {
      if (activeIdRef.current !== lockedId) return
      if (err instanceof ApiError && err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
        activeIdemKeyRef.current = null
        activeFpRef.current = null
        setActionError(
          `Idempotency-Key 충돌(IDEMPOTENCY_KEY_CONFLICT): ${err.message}`,
        )
      } else if (err instanceof ApiError) {
        setActionError(
          `상태 변경 실패(${err.status}${err.code ? ` ${err.code}` : ''}): ${err.message}`,
        )
      } else if (err instanceof Error) {
        setActionError(
          `${err.message} 응답이 불확실할 수 있습니다. 상세를 새로고침해 확인하세요. 같은 작업으로 다시 시도하면 이전 키가 재사용됩니다.`,
        )
      } else {
        setActionError('상태 변경에 실패했습니다.')
      }
    } finally {
      actionLockRef.current = false
      setActionBusy(false)
      setRevalidating(false)
    }
  }

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
          action="목록으로 돌아가 다시 선택하세요."
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
      description="직원 계정이 아니라 AI 모델을 호출하는 업무 서비스의 등록 정보와 런타임 제한을 관리합니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <Link className="back-link" to="/clients">
        ← AI 사용 시스템 목록
      </Link>

      <p className="panel-hint" role="note">
        {CLIENT_FIELD_HELP.registryEditable} {CLIENT_FIELD_HELP.policyEditable}
      </p>

      {clientErr ? (
        <SectionError
          title={clientErr.title}
          message={clientErr.message}
          action={clientErr.action}
        />
      ) : null}
      {actionError ? (
        <SectionError title="작업 오류" message={actionError} />
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
              <dd>
                <span className="mono">{displayClient.client_key}</span>
                <ClientKeyCopyActions clientKey={displayClient.client_key} />
                <p className="form-field-hint">
                  {CLIENT_FIELD_HELP.clientKeyIntegration}
                </p>
              </dd>
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
          <div className="toolbar__actions" style={{ marginTop: '1rem' }}>
            <button
              type="button"
              className="btn"
              disabled={actionBusy}
              onClick={() => {
                setActionError(null)
                setEditOpen(true)
              }}
            >
              등록 정보 수정
            </button>
            {displayClient.is_active ? (
              <button
                type="button"
                className="btn btn--danger"
                disabled={actionBusy}
                onClick={() => {
                  setActionError(null)
                  setConsent(false)
                  setActiveConfirm('deactivate')
                }}
              >
                등록 비활성화
              </button>
            ) : (
              <button
                type="button"
                className="btn"
                disabled={actionBusy}
                onClick={() => {
                  setActionError(null)
                  setConsent(false)
                  setActiveConfirm('reactivate')
                }}
              >
                등록 재활성화
              </button>
            )}
          </div>
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
        <p className="panel-hint secondary-text">
          {CLIENT_FIELD_HELP.policyEditable} {CLIENT_FIELD_HELP.policySaveImpact}
        </p>
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
        {displayClient && displayPolicy && !policyLoading ? (
          <div className="toolbar__actions" style={{ marginBottom: '0.75rem' }}>
            <button
              type="button"
              className="btn"
              disabled={actionBusy || policyEditOpen}
              onClick={() => {
                setActionError(null)
                setPolicyEditOpen(true)
              }}
            >
              {policy === null ? '새 정책 만들기' : '정책 수정'}
            </button>
          </div>
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

      {displayClient ? (
        <ClientEditDialog
          open={editOpen}
          client={displayClient}
          onClose={() => setEditOpen(false)}
          onSaved={(saved) => {
            // Ignore completions that no longer match the routed Client identity.
            if (saved.id !== activeIdRef.current) return
            if (saved.id !== clientId) return
            setClient(saved)
            setLastUpdated(new Date())
            setEditOpen(false)
            setActionError(null)
          }}
        />
      ) : null}

      {displayClient && policyEditOpen ? (
        <ClientRuntimePolicyDialog
          open={policyEditOpen}
          client={displayClient}
          policy={policy}
          onClose={(opts) => {
            setPolicyEditOpen(false)
            // Refresh after gated dismiss — never treat as save success.
            if (opts?.refresh && activeIdRef.current === clientId) {
              void load('refresh')
            }
          }}
          onSaved={() => {
            if (activeIdRef.current !== clientId) return
            setPolicyEditOpen(false)
            setActionError(null)
            void load('refresh')
          }}
        />
      ) : null}

      {displayClient && activeConfirm ? (
        <DangerConfirmDialog
          title={
            activeConfirm === 'deactivate'
              ? '등록 비활성화 확인'
              : '등록 재활성화 확인'
          }
          targetLabel={displayClient.display_name}
          targetId={displayClient.client_key}
          impact={
            activeConfirm === 'deactivate' ? (
              <div>
                <p>{CLIENT_FIELD_HELP.deactivateImpact}</p>
                <p>
                  이 작업은 보안 차단이 아닙니다. Runtime Policy 행은 유지되며
                  자동으로 바뀌지 않습니다.
                </p>
              </div>
            ) : (
              <p>
                등록을 다시 활성(ACTIVE)으로 표시합니다. 기존 Runtime Policy 행은
                그대로 유지되며 자동 변경되지 않습니다. Gateway 정책 스냅샷
                반영은 활성 ClientApp + enabled 정책 조건에 따릅니다.
              </p>
            )
          }
          notes="저장 전 최신 GET으로 상태를 다시 확인합니다. GET→PATCH는 원자적이지 않으며 Backend ETag/If-Match는 없습니다."
          consentLabel={
            activeConfirm === 'deactivate'
              ? '위 영향을 이해했으며 등록 비활성화를 진행합니다'
              : '등록 재활성화를 진행합니다'
          }
          consent={consent}
          onConsentChange={setConsent}
          refreshing={revalidating}
          submitting={actionBusy}
          error={null}
          confirmLabel={
            activeConfirm === 'deactivate' ? '비활성화 실행' : '재활성화 실행'
          }
          submittingLabel="실행 중…"
          canConfirm={!actionBusy && !revalidating}
          onCancel={closeActiveConfirm}
          onConfirm={() => void runActiveToggle()}
          testId="client-active-confirm-dialog"
        />
      ) : null}
    </AppShell>
  )
}
