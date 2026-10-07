import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getClient, getClientRuntimePolicy } from '../api/clients'
import type { ClientApp, ClientRuntimePolicyResponse } from '../api/types'
import { ActiveBadge } from '../components/ActiveBadge'
import { AppShell } from '../components/AppShell'
import { LoadingBlock } from '../components/LoadingBlock'
import { SectionError } from '../components/SectionError'
import { StatusBadge } from '../components/StatusBadge'
import { formatApiDateTime } from '../utils/date'

function formatLimit(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'unrestricted'
  return String(value)
}

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
        setClientError('Client를 찾을 수 없습니다.')
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
            setClientError('Client를 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Client를 불러오지 못했습니다.'
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
            setPolicyError('Runtime Policy를 찾을 수 없습니다.')
            return
          }
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Runtime Policy를 불러오지 못했습니다.'
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
        title="Clients"
        description="Client 상세"
        onRefresh={() => void load('refresh')}
        refreshing={false}
        lastUpdated={lastUpdated}
      >
        <Link className="back-link" to="/clients">
          ← Clients
        </Link>
        <SectionError
          title="Client를 찾을 수 없습니다."
          message="요청한 Client가 없거나 삭제되었습니다."
        />
      </AppShell>
    )
  }

  const policy = displayPolicy?.policy ?? null

  return (
    <AppShell
      title={displayClient ? displayClient.display_name : 'Client 상세'}
      description="Client metadata와 Runtime Policy 조회 전용입니다. POST/PATCH Client 및 PUT Runtime Policy UI는 제공하지 않습니다."
      onRefresh={() => void load('refresh')}
      refreshing={refreshing}
      lastUpdated={lastUpdated}
    >
      <Link className="back-link" to="/clients">
        ← Clients
      </Link>

      {clientError ? (
        <SectionError
          title={
            displayClient ? 'Client 새로고침 실패' : 'Client 오류'
          }
          message={
            displayClient
              ? `기존 Client 정보를 표시하고 있습니다. 새로고침 실패: ${clientError}`
              : clientError
          }
        />
      ) : null}

      {clientLoading && !displayClient ? (
        <LoadingBlock label="Client를 불러오는 중…" />
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
          <dl className="meta-grid">
            <div>
              <dt>Client Key</dt>
              <dd className="mono">{displayClient.client_key}</dd>
            </div>
            <div>
              <dt>Description</dt>
              <dd>{displayClient.description || '—'}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatApiDateTime(displayClient.created_at)}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{formatApiDateTime(displayClient.updated_at)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      <section
        className="detail-panel"
        aria-labelledby="client-policy-heading"
      >
        <h2 id="client-policy-heading">Runtime Policy</h2>
        <p className="panel-hint">
          policy null은 정책 행이 없음을 의미합니다. nullable limit은
          unrestricted입니다. priority는 숫자가 작을수록 높은 우선순위
          intent이며, requested priority가 항상 Gateway에서 전달된다고 단정하지
          않습니다.
        </p>
        {policyError ? (
          <SectionError
            title={
              displayPolicy
                ? 'Runtime Policy 새로고침 실패'
                : 'Runtime Policy 오류'
            }
            message={
              displayPolicy
                ? `기존 Runtime Policy를 표시하고 있습니다. 새로고침 실패: ${policyError}`
                : policyError
            }
          />
        ) : null}
        {policyLoading && !displayPolicy ? (
          <LoadingBlock label="Runtime Policy를 불러오는 중…" />
        ) : null}
        {displayPolicy && policy === null ? (
          <p className="empty-state" role="status">
            Runtime Policy 행이 없습니다 (policy = null).
          </p>
        ) : null}
        {policy ? (
          <dl className="meta-grid">
            <div>
              <dt>Enabled</dt>
              <dd>
                <StatusBadge
                  status={policy.is_enabled ? 'ACTIVE' : 'INACTIVE'}
                  label={policy.is_enabled ? 'enabled' : 'disabled'}
                />
              </dd>
            </div>
            <div>
              <dt>max_input_tokens</dt>
              <dd>{formatLimit(policy.max_input_tokens)}</dd>
            </div>
            <div>
              <dt>max_output_tokens</dt>
              <dd>{formatLimit(policy.max_output_tokens)}</dd>
            </div>
            <div>
              <dt>max_concurrent_requests</dt>
              <dd>{formatLimit(policy.max_concurrent_requests)}</dd>
            </div>
            <div>
              <dt>priority</dt>
              <dd>
                {policy.priority === null || policy.priority === undefined
                  ? 'unrestricted / unset'
                  : `${policy.priority} (smaller = higher priority intent)`}
              </dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{formatApiDateTime(policy.updated_at)}</dd>
            </div>
          </dl>
        ) : null}
      </section>
    </AppShell>
  )
}
