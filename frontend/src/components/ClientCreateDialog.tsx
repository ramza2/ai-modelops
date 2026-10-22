import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { createClient } from '../api/clients'
import type { ClientApp } from '../api/types'
import {
  createIdempotencyKey,
  mutationPayloadFingerprint,
} from '../utils/idempotencyKey'
import { CLIENT_FIELD_HELP } from '../utils/uiLabels'
import { HelpHint, TermLabel } from './HelpHint'
import { SectionError } from './SectionError'

const KEY_MAX = 120
const NAME_MAX = 255

export type ClientCreateDialogProps = {
  open: boolean
  onClose: () => void
  onCreated: (client: ClientApp) => void
}

function clientMutationErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'CLIENT_KEY_CONFLICT') {
      return `서비스 키 중복(CLIENT_KEY_CONFLICT): ${err.message}`
    }
    if (err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
      return `Idempotency-Key 충돌(IDEMPOTENCY_KEY_CONFLICT): ${err.message} 최신 목록·상세를 확인한 뒤, 내용을 바꿔 새로 저장하세요. 자동으로 다른 키로 재시도하지 않습니다.`
    }
    if (err.status === 422) {
      return `입력 검증 실패(422): ${err.message}`
    }
    return `저장 실패(${err.status}${err.code ? ` ${err.code}` : ''}): ${err.message}`
  }
  if (err instanceof Error) {
    return `${err.message} 응답이 불확실할 수 있습니다. 목록·상세를 먼저 확인하세요. 같은 내용으로 다시 저장하면 이전 Idempotency-Key가 재사용됩니다.`
  }
  return '저장에 실패했습니다. 목록·상세를 확인한 뒤 다시 시도하세요.'
}

export function ClientCreateDialog({
  open,
  onClose,
  onCreated,
}: ClientCreateDialogProps) {
  const [clientKey, setClientKey] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [fieldErrors, setFieldErrors] = useState<{
    client_key?: string
    display_name?: string
  }>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const idempotencyKeyRef = useRef<string | null>(null)
  const payloadFpRef = useRef<string | null>(null)
  const submitLockRef = useRef(false)

  useEffect(() => {
    if (!open) return
    setClientKey('')
    setDisplayName('')
    setDescription('')
    setFieldErrors({})
    setSubmitError(null)
    setSubmitting(false)
    idempotencyKeyRef.current = null
    payloadFpRef.current = null
    submitLockRef.current = false
  }, [open])

  if (!open) return null

  const dirty =
    clientKey.trim() !== '' ||
    displayName.trim() !== '' ||
    description.trim() !== ''

  const validate = (): {
    client_key: string
    display_name: string
    description: string | null
  } | null => {
    const errors: { client_key?: string; display_name?: string } = {}
    const key = clientKey.trim()
    const name = displayName.trim()
    if (!key) errors.client_key = '서비스 키는 필수입니다.'
    else if (key.length > KEY_MAX) {
      errors.client_key = `서비스 키는 최대 ${KEY_MAX}자입니다.`
    }
    if (!name) errors.display_name = '표시 이름은 필수입니다.'
    else if (name.length > NAME_MAX) {
      errors.display_name = `표시 이름은 최대 ${NAME_MAX}자입니다.`
    }
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return null
    const descRaw = description.trim()
    return {
      client_key: key,
      display_name: name,
      description: descRaw ? descRaw : null,
    }
  }

  const ensureIdempotencyKey = (fingerprint: string): string => {
    if (
      idempotencyKeyRef.current &&
      payloadFpRef.current === fingerprint
    ) {
      return idempotencyKeyRef.current
    }
    const key = createIdempotencyKey()
    idempotencyKeyRef.current = key
    payloadFpRef.current = fingerprint
    return key
  }

  const handleClose = () => {
    if (submitting) return
    if (dirty && !window.confirm('작성 중인 내용이 있습니다. 닫으시겠습니까?')) {
      return
    }
    onClose()
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (submitLockRef.current || submitting) return
    const parsed = validate()
    if (!parsed) return
    const body = {
      client_key: parsed.client_key,
      display_name: parsed.display_name,
      ...(parsed.description !== null
        ? { description: parsed.description }
        : {}),
    }
    const fp = mutationPayloadFingerprint(body)
    let idemKey: string
    try {
      idemKey = ensureIdempotencyKey(fp)
    } catch (err) {
      setSubmitError(
        err instanceof Error ? err.message : 'Idempotency-Key를 만들 수 없습니다.',
      )
      return
    }

    submitLockRef.current = true
    setSubmitting(true)
    setSubmitError(null)
    try {
      const created = await createClient(body, { idempotencyKey: idemKey })
      idempotencyKeyRef.current = null
      payloadFpRef.current = null
      onCreated(created)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
        idempotencyKeyRef.current = null
        payloadFpRef.current = null
      }
      setSubmitError(clientMutationErrorMessage(err))
    } finally {
      submitLockRef.current = false
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="client-create-title"
      data-testid="client-create-dialog"
    >
      <div className="modal-card">
        <h2 id="client-create-title">신규 시스템 등록</h2>
        <p className="panel-hint">{CLIENT_FIELD_HELP.registryEditable}</p>
        <p className="panel-hint secondary-text">
          서비스 키는 등록 후 변경할 수 없습니다. 대소문자를 구분합니다.
        </p>
        {submitError ? (
          <SectionError title="등록 오류" message={submitError} />
        ) : null}
        <form onSubmit={(e) => void onSubmit(e)}>
          <label className="toolbar__field" htmlFor="create-client-key">
            <span>
              <TermLabel
                text="서비스 키"
                apiTerm="client_key"
                help={CLIENT_FIELD_HELP.clientKey}
              />{' '}
              <span className="form-field-hint">필수 · 최대 {KEY_MAX}자</span>
            </span>
            <input
              id="create-client-key"
              name="client_key"
              value={clientKey}
              onChange={(e) => setClientKey(e.target.value)}
              maxLength={KEY_MAX + 8}
              autoComplete="off"
              disabled={submitting}
              aria-invalid={Boolean(fieldErrors.client_key)}
              placeholder="예: alzi"
            />
            {fieldErrors.client_key ? (
              <span className="form-field-error">{fieldErrors.client_key}</span>
            ) : null}
          </label>
          <label className="toolbar__field" htmlFor="create-display-name">
            <span>
              <TermLabel
                text="표시 이름"
                apiTerm="display_name"
                help={CLIENT_FIELD_HELP.displayName}
              />{' '}
              <span className="form-field-hint">필수 · 최대 {NAME_MAX}자</span>
            </span>
            <input
              id="create-display-name"
              name="display_name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              maxLength={NAME_MAX + 8}
              disabled={submitting}
              aria-invalid={Boolean(fieldErrors.display_name)}
              placeholder="예: 사내 지식 AI 검색"
            />
            {fieldErrors.display_name ? (
              <span className="form-field-error">{fieldErrors.display_name}</span>
            ) : null}
          </label>
          <label className="toolbar__field" htmlFor="create-description">
            <span>
              <TermLabel
                text="설명"
                apiTerm="description"
                help={CLIENT_FIELD_HELP.description}
              />{' '}
              <span className="form-field-hint">선택</span>
              <HelpHint label="설명">{CLIENT_FIELD_HELP.description}</HelpHint>
            </span>
            <textarea
              id="create-description"
              name="description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              disabled={submitting}
              placeholder="예: 사내 문서 검색 및 RAG 서비스를 운영하는 업무 시스템"
            />
          </label>
          <div className="toolbar__actions" style={{ marginTop: '1rem' }}>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={handleClose}
              disabled={submitting}
            >
              취소
            </button>
            <button type="submit" className="btn" disabled={submitting}>
              {submitting ? '등록 중…' : '등록'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
