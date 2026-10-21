import type { Endpoint } from '../api/types'
import { ENDPOINT_FIELD_HELP } from '../utils/uiLabels'
import { shortId } from '../utils/date'
import { SectionError } from './SectionError'
import { StatusBadge } from './StatusBadge'

export type EndpointEnabledMode = 'disable' | 'enable'

type EndpointEnabledConfirmDialogProps = {
  mode: EndpointEnabledMode
  endpoint: Endpoint | null
  refreshing: boolean
  submitting: boolean
  error: string | null
  consent: boolean
  onConsentChange: (next: boolean) => void
  aliasInput: string
  onAliasInputChange: (next: string) => void
  onCancel: () => void
  onConfirm: () => void
}

export function EndpointEnabledConfirmDialog({
  mode,
  endpoint,
  refreshing,
  submitting,
  error,
  consent,
  onConsentChange,
  aliasInput,
  onAliasInputChange,
  onCancel,
  onConfirm,
}: EndpointEnabledConfirmDialogProps) {
  const hasActiveRoute = Boolean(endpoint?.active_route?.deployment_id)
  const requireAlias = mode === 'disable' && hasActiveRoute
  const aliasMatches = Boolean(endpoint && aliasInput === endpoint.alias)
  const busy = refreshing || submitting
  const canSubmit =
    Boolean(endpoint) &&
    !busy &&
    consent &&
    (!requireAlias || aliasMatches) &&
    (mode === 'disable' ? Boolean(endpoint?.is_enabled) : !endpoint?.is_enabled)

  const title =
    mode === 'disable' ? '사용 중지 확인' : '다시 사용 (재활성화) 확인'
  const confirmLabel =
    mode === 'disable'
      ? submitting
        ? '사용 중지 중…'
        : '사용 중지 실행'
      : submitting
        ? '재활성화 중…'
        : '다시 사용 실행'

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="endpoint-enabled-dialog-title"
      data-testid="endpoint-enabled-dialog"
    >
      <div className="modal-card">
        <h2 id="endpoint-enabled-dialog-title">{title}</h2>
        {refreshing || !endpoint ? (
          <p className="metric-line" role="status">
            최신 Endpoint·ACTIVE Route 상태를 다시 조회하는 중…
          </p>
        ) : null}

        {endpoint ? (
        <p>
          대상 API 별칭:{' '}
          <strong className="mono">{endpoint.alias}</strong>
          {endpoint.display_name && endpoint.display_name !== endpoint.alias
            ? ` (${endpoint.display_name})`
            : null}
        </p>
        ) : null}

        {endpoint ? (
          <>
        <dl className="meta-grid">
          <div>
            <dt>사용 설정 (is_enabled)</dt>
            <dd>{endpoint.is_enabled ? '사용 중 (true)' : '사용 중지 (false)'}</dd>
          </div>
          <div>
            <dt>요청 처리 상태 (Traffic)</dt>
            <dd>
              <StatusBadge status={endpoint.traffic_state} />
            </dd>
          </div>
          <div>
            <dt>현재 활성 연결</dt>
            <dd>
              {hasActiveRoute ? (
                <>
                  모델 연결됨 —{' '}
                  <span className="mono">
                    {endpoint.active_route?.deployment?.name ??
                      shortId(endpoint.active_route?.deployment_id)}
                  </span>
                </>
              ) : (
                '활성 연결 없음 (ACTIVE Route 없음)'
              )}
            </dd>
          </div>
        </dl>

        {mode === 'disable' ? (
          <>
            <p className="panel-hint panel-hint--warn" role="note">
              {hasActiveRoute
                ? ENDPOINT_FIELD_HELP.connectedAlias
                : ENDPOINT_FIELD_HELP.unusedAlias}{' '}
              {ENDPOINT_FIELD_HELP.disable}
            </p>
            {hasActiveRoute ? (
              <p className="panel-hint panel-hint--warn" role="alert">
                이 별칭으로 Gateway를 호출하는 기존 서비스 요청이 실패할 수
                있습니다. ACTIVE Route는 자동으로 제거되지 않습니다(필요 시
                연결 해제 Unpublish를 별도로 수행하세요).
              </p>
            ) : (
              <p className="panel-hint" role="note">
                ACTIVE Route가 없는 미사용 별칭입니다. 사용 중지는 정리용으로
                적합하며, 과거 Route History·Operation 이력은 유지됩니다.
              </p>
            )}
          </>
        ) : (
          <p className="panel-hint" role="note">
            {ENDPOINT_FIELD_HELP.enable}{' '}
            {hasActiveRoute
              ? ENDPOINT_FIELD_HELP.connectedAlias
              : ENDPOINT_FIELD_HELP.unusedAlias}
          </p>
        )}

        <p className="secondary-text">{ENDPOINT_FIELD_HELP.disableVsOthers}</p>

        <label className="toolbar__field" htmlFor="endpoint-enabled-consent">
          <span className="checkbox-row">
            <input
              id="endpoint-enabled-consent"
              type="checkbox"
              checked={consent}
              disabled={busy}
              onChange={(e) => onConsentChange(e.target.checked)}
              data-testid="endpoint-enabled-consent"
            />
            <span>
              {mode === 'disable'
                ? '대상 별칭·영향·기존 호출 실패 가능성을 확인했으며 사용 중지에 동의합니다.'
                : '재활성화는 ACTIVE Route·Deployment를 자동 복구하지 않음을 이해했습니다.'}
            </span>
          </span>
        </label>

        {requireAlias ? (
          <label
            className="toolbar__field"
            htmlFor="endpoint-enabled-alias-confirm"
          >
            <span>위험 확인: 현재 Alias를 그대로 입력하세요</span>
            <input
              id="endpoint-enabled-alias-confirm"
              type="text"
              value={aliasInput}
              disabled={busy}
              autoComplete="off"
              onChange={(e) => onAliasInputChange(e.target.value)}
              placeholder={endpoint.alias}
              data-testid="endpoint-enabled-alias-confirm"
            />
          </label>
        ) : null}
          </>
        ) : null}

        {error ? <SectionError title="확인 오류" message={error} /> : null}

        <div className="toolbar__actions">
          <button
            type="button"
            className="btn"
            disabled={submitting}
            onClick={onCancel}
          >
            취소
          </button>
          <button
            type="button"
            className={mode === 'disable' ? 'btn btn--danger' : 'btn btn--primary'}
            data-testid="endpoint-enabled-confirm"
            disabled={!canSubmit}
            aria-busy={submitting}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
