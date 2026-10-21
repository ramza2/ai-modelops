import type { ReactNode } from 'react'
import { SectionError } from './SectionError'

export type DangerConfirmDialogProps = {
  title: string
  /** Short target description shown prominently. */
  targetLabel: string
  targetId?: string | null
  impact: ReactNode
  /** Extra advisory copy (recovery, related steps, etc.). */
  notes?: ReactNode
  consentLabel: string
  consent: boolean
  onConsentChange: (next: boolean) => void
  /** Optional typed confirmation (e.g. exact ``PURGE``). */
  typedConfirm?: {
    expected: string
    value: string
    onChange: (next: string) => void
    placeholder?: string
    hint: string
  }
  refreshing?: boolean
  submitting?: boolean
  error?: string | null
  confirmLabel: string
  submittingLabel?: string
  onCancel: () => void
  onConfirm: () => void
  /** When false, Confirm stays disabled even if consent is checked. */
  canConfirm?: boolean
  testId?: string
}

/**
 * Reusable destructive-action confirmation dialog (display + consent only).
 * Callers must revalidate Backend state before invoking onConfirm side effects.
 */
export function DangerConfirmDialog({
  title,
  targetLabel,
  targetId,
  impact,
  notes,
  consentLabel,
  consent,
  onConsentChange,
  typedConfirm,
  refreshing = false,
  submitting = false,
  error = null,
  confirmLabel,
  submittingLabel = '실행 중…',
  onCancel,
  onConfirm,
  canConfirm = true,
  testId = 'danger-confirm-dialog',
}: DangerConfirmDialogProps) {
  const typedOk =
    !typedConfirm || typedConfirm.value === typedConfirm.expected
  const busy = refreshing || submitting
  const submitEnabled = Boolean(consent && typedOk && canConfirm && !busy)

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="danger-confirm-title"
      data-testid={testId}
    >
      <div className="modal-card">
        <h2 id="danger-confirm-title">{title}</h2>
        {refreshing ? (
          <p className="metric-line" role="status">
            최신 상태를 다시 조회하는 중…
          </p>
        ) : null}
        <p>
          대상: <strong>{targetLabel}</strong>
          {targetId ? (
            <>
              {' '}
              <span className="mono secondary-text">({targetId})</span>
            </>
          ) : null}
        </p>
        <div className="panel-hint">{impact}</div>
        {notes ? <div className="panel-hint secondary-text">{notes}</div> : null}
        {typedConfirm ? (
          <label className="toolbar__field">
            <span>{typedConfirm.hint}</span>
            <input
              value={typedConfirm.value}
              onChange={(e) => typedConfirm.onChange(e.target.value)}
              placeholder={typedConfirm.placeholder || typedConfirm.expected}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </label>
        ) : null}
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={consent}
            disabled={busy}
            onChange={(e) => onConsentChange(e.target.checked)}
          />
          <span>{consentLabel}</span>
        </label>
        {error ? (
          <SectionError title="확인 또는 실행 실패" message={error} />
        ) : null}
        <div className="toolbar__actions">
          <button
            type="button"
            className="btn"
            disabled={busy}
            title={
              busy
                ? '재검증·실행 중에는 취소할 수 없습니다'
                : undefined
            }
            onClick={onCancel}
          >
            취소
          </button>
          <button
            type="button"
            className="btn btn--danger"
            disabled={!submitEnabled}
            onClick={onConfirm}
          >
            {submitting ? submittingLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
