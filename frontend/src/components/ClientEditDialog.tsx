import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { getClient, updateClient } from '../api/clients'
import type { ClientApp } from '../api/types'
import {
  createIdempotencyKey,
  mutationPayloadFingerprint,
} from '../utils/idempotencyKey'
import { CLIENT_FIELD_HELP } from '../utils/uiLabels'
import { HelpHint, TermLabel } from './HelpHint'
import { SectionError } from './SectionError'

const NAME_MAX = 255

export type ClientEditDialogProps = {
  open: boolean
  client: ClientApp
  onClose: () => void
  onSaved: (client: ClientApp) => void
}

function mutationErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
      return `Idempotency-Key 충돌(IDEMPOTENCY_KEY_CONFLICT): ${err.message} 최신 상세를 확인한 뒤 다시 시도하세요.`
    }
    if (err.status === 422) {
      return `입력 검증 실패(422): ${err.message}`
    }
    if (err.status === 404) {
      return '대상 사용 시스템을 찾을 수 없습니다(404). 목록으로 돌아가 다시 선택하세요.'
    }
    return `저장 실패(${err.status}${err.code ? ` ${err.code}` : ''}): ${err.message}`
  }
  if (err instanceof Error) {
    return `${err.message} 응답이 불확실할 수 있습니다. 상세를 새로고침해 확인하세요. 같은 내용으로 다시 저장하면 이전 Idempotency-Key가 재사용됩니다.`
  }
  return '저장에 실패했습니다.'
}

export function ClientEditDialog({
  open,
  client,
  onClose,
  onSaved,
}: ClientEditDialogProps) {
  const [displayName, setDisplayName] = useState(client.display_name)
  const [description, setDescription] = useState(client.description ?? '')
  const [baseline, setBaseline] = useState(client)
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [revalidating, setRevalidating] = useState(false)
  const idempotencyKeyRef = useRef<string | null>(null)
  const payloadFpRef = useRef<string | null>(null)
  const submitLockRef = useRef(false)

  useEffect(() => {
    if (!open) return
    setDisplayName(client.display_name)
    setDescription(client.description ?? '')
    setBaseline(client)
    setFieldError(null)
    setSubmitError(null)
    setSubmitting(false)
    setRevalidating(false)
    idempotencyKeyRef.current = null
    payloadFpRef.current = null
    submitLockRef.current = false
  }, [open, client])

  if (!open) return null

  const dirty =
    displayName.trim() !== baseline.display_name ||
    (description.trim() || null) !== (baseline.description ?? null)

  const buildPatch = (): {
    display_name?: string
    description?: string | null
  } | null => {
    const name = displayName.trim()
    if (!name) {
      setFieldError('표시 이름은 필수입니다.')
      return null
    }
    if (name.length > NAME_MAX) {
      setFieldError(`표시 이름은 최대 ${NAME_MAX}자입니다.`)
      return null
    }
    setFieldError(null)
    const nextDesc = description.trim() ? description.trim() : null
    const patch: { display_name?: string; description?: string | null } = {}
    if (name !== baseline.display_name) patch.display_name = name
    if (nextDesc !== (baseline.description ?? null)) {
      patch.description = nextDesc
    }
    if (Object.keys(patch).length === 0) {
      setSubmitError('변경된 내용이 없습니다.')
      return null
    }
    return patch
  }

  const ensureKey = (fp: string): string => {
    if (idempotencyKeyRef.current && payloadFpRef.current === fp) {
      return idempotencyKeyRef.current
    }
    const key = createIdempotencyKey()
    idempotencyKeyRef.current = key
    payloadFpRef.current = fp
    return key
  }

  const handleClose = () => {
    if (submitting || revalidating) return
    if (dirty && !window.confirm('수정 중인 내용이 있습니다. 닫으시겠습니까?')) {
      return
    }
    onClose()
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (submitLockRef.current || submitting || revalidating) return
    const patch = buildPatch()
    if (!patch) return

    const fp = mutationPayloadFingerprint({
      client_id: baseline.id,
      ...patch,
    })
    let idemKey: string
    try {
      idemKey = ensureKey(fp)
    } catch (err) {
      setSubmitError(
        err instanceof Error ? err.message : 'Idempotency-Key를 만들 수 없습니다.',
      )
      return
    }

    submitLockRef.current = true
    setSubmitting(true)
    setRevalidating(true)
    setSubmitError(null)
    try {
      const latest = await getClient(baseline.id)
      if (latest.id !== baseline.id || latest.client_key !== baseline.client_key) {
        setSubmitError(
          '대상 사용 시스템이 변경되었습니다. 화면을 새로고침한 뒤 다시 확인하세요. 저장을 중단했습니다.',
        )
        return
      }
      if (latest.updated_at !== baseline.updated_at) {
        setSubmitError(
          `다른 변경이 감지되었습니다(updated_at: ${latest.updated_at}). 최신 내용을 다시 확인한 뒤 수정하세요. Frontend 검증은 동시 수정의 완전한 방지책이 아닙니다.`,
        )
        setBaseline(latest)
        setDisplayName(latest.display_name)
        setDescription(latest.description ?? '')
        idempotencyKeyRef.current = null
        payloadFpRef.current = null
        return
      }
      setRevalidating(false)
      const saved = await updateClient(baseline.id, patch, {
        idempotencyKey: idemKey,
      })
      idempotencyKeyRef.current = null
      payloadFpRef.current = null
      onSaved(saved)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
        idempotencyKeyRef.current = null
        payloadFpRef.current = null
      }
      setSubmitError(mutationErrorMessage(err))
    } finally {
      submitLockRef.current = false
      setSubmitting(false)
      setRevalidating(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="client-edit-title"
      data-testid="client-edit-dialog"
    >
      <div className="modal-card">
        <h2 id="client-edit-title">등록 정보 수정</h2>
        <p className="panel-hint">
          서비스 키·Client ID·등록 시각은 변경할 수 없습니다.
        </p>
        <dl className="meta-grid">
          <div>
            <dt>
              <TermLabel text="서비스 키" apiTerm="client_key" />
            </dt>
            <dd className="mono">{baseline.client_key}</dd>
          </div>
          <div>
            <dt>Client ID</dt>
            <dd className="mono">{baseline.id}</dd>
          </div>
        </dl>
        {revalidating ? (
          <p className="metric-line" role="status">
            저장 전 최신 상태를 확인하는 중…
          </p>
        ) : null}
        {submitError ? (
          <SectionError title="수정 오류" message={submitError} />
        ) : null}
        <form onSubmit={(e) => void onSubmit(e)}>
          <label className="toolbar__field" htmlFor="edit-display-name">
            <span>
              <TermLabel
                text="표시 이름"
                apiTerm="display_name"
                help={CLIENT_FIELD_HELP.displayName}
              />{' '}
              <span className="form-field-hint">필수</span>
            </span>
            <input
              id="edit-display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              maxLength={NAME_MAX + 8}
              disabled={submitting}
              aria-invalid={Boolean(fieldError)}
            />
            {fieldError ? (
              <span className="form-field-error">{fieldError}</span>
            ) : null}
          </label>
          <label className="toolbar__field" htmlFor="edit-description">
            <span>
              <TermLabel
                text="설명"
                apiTerm="description"
                help={CLIENT_FIELD_HELP.description}
              />{' '}
              <HelpHint label="설명">
                내용을 비운 채 저장하면 설명이 삭제됩니다(API null).
              </HelpHint>
            </span>
            <textarea
              id="edit-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              disabled={submitting}
            />
          </label>
          <div className="toolbar__actions" style={{ marginTop: '1rem' }}>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={handleClose}
              disabled={submitting || revalidating}
            >
              취소
            </button>
            <button
              type="submit"
              className="btn"
              disabled={submitting || revalidating}
            >
              {submitting || revalidating ? '저장 중…' : '저장'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
