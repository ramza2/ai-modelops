import { useEffect, useRef, useState } from 'react'
import { copyTextToClipboard } from '../utils/clipboard'
import { formatXAiClientHeader } from '../utils/clientServiceKey'

export type ClientKeyCopyActionsProps = {
  clientKey: string
  disabled?: boolean
  /** Extra class on the actions row. */
  className?: string
}

type CopyFeedback = { kind: 'ok' | 'err'; message: string } | null

export function ClientKeyCopyActions({
  clientKey,
  disabled = false,
  className,
}: ClientKeyCopyActionsProps) {
  const [feedback, setFeedback] = useState<CopyFeedback>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const key = clientKey.trim()
  const canCopy = key.length > 0 && !disabled

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current)
    }
  }, [])

  const flash = (next: CopyFeedback) => {
    setFeedback(next)
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setFeedback(null), 2500)
  }

  const copyKey = async () => {
    if (!canCopy) return
    try {
      await copyTextToClipboard(key)
      flash({ kind: 'ok', message: '서비스 키를 복사했습니다.' })
    } catch (err) {
      flash({
        kind: 'err',
        message:
          err instanceof Error
            ? err.message
            : '서비스 키를 복사하지 못했습니다.',
      })
    }
  }

  const copyHeader = async () => {
    if (!canCopy) return
    try {
      await copyTextToClipboard(formatXAiClientHeader(key))
      flash({ kind: 'ok', message: 'X-AI-Client 헤더를 복사했습니다.' })
    } catch (err) {
      flash({
        kind: 'err',
        message:
          err instanceof Error
            ? err.message
            : 'X-AI-Client 헤더를 복사하지 못했습니다.',
      })
    }
  }

  return (
    <div
      className={className}
      data-testid="client-key-copy-actions"
      style={{ marginTop: '0.35rem' }}
    >
      <div className="toolbar__actions">
        <button
          type="button"
          className="btn btn--ghost btn--small"
          disabled={!canCopy}
          onClick={() => void copyKey()}
        >
          서비스 키 복사
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          disabled={!canCopy}
          onClick={() => void copyHeader()}
        >
          X-AI-Client 헤더 복사
        </button>
      </div>
      {feedback ? (
        <p
          className={
            feedback.kind === 'ok'
              ? 'form-field-hint'
              : 'form-field-error'
          }
          role="status"
          data-testid="client-key-copy-feedback"
        >
          {feedback.message}
        </p>
      ) : null}
    </div>
  )
}
