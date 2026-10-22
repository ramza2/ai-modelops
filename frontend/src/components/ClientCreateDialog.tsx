import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { createClient } from '../api/clients'
import type { ClientApp } from '../api/types'
import { createClientServiceKey } from '../utils/clientServiceKey'
import {
  createIdempotencyKey,
  mutationPayloadFingerprint,
} from '../utils/idempotencyKey'
import { CLIENT_FIELD_HELP } from '../utils/uiLabels'
import { ClientKeyCopyActions } from './ClientKeyCopyActions'
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
      return `서비스 키 중복(CLIENT_KEY_CONFLICT): ${err.message} 자동으로 다른 키를 만들어 재등록하지 않습니다. 목록을 확인한 뒤, 필요하면 「키 재생성」으로 새 키를 만든 다음 다시 등록하세요.`
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
    return `${err.message} 응답이 불확실할 수 있습니다. 목록에서 동일 서비스 키 등록 여부를 먼저 확인하세요. 확인 전에 키를 재생성하면 중복 등록될 수 있습니다. 같은 내용으로 다시 저장하면 이전 Idempotency-Key가 재사용됩니다.`
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
  const [keyGenError, setKeyGenError] = useState<string | null>(null)
  const [responseUncertain, setResponseUncertain] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const idempotencyKeyRef = useRef<string | null>(null)
  const payloadFpRef = useRef<string | null>(null)
  const submitLockRef = useRef(false)
  /** True while this open cycle has initialized form + auto key. */
  const sessionReadyRef = useRef(false)

  const applyGeneratedKey = (): boolean => {
    try {
      const next = createClientServiceKey()
      setClientKey(next)
      setKeyGenError(null)
      setFieldErrors((prev) => {
        if (!prev.client_key) return prev
        const { client_key: _removed, ...rest } = prev
        return rest
      })
      return true
    } catch (err) {
      setClientKey('')
      setKeyGenError(
        err instanceof Error
          ? err.message
          : '안전한 서비스 키를 만들 수 없습니다.',
      )
      return false
    }
  }

  useEffect(() => {
    if (!open) {
      sessionReadyRef.current = false
      return
    }
    // Same open session: do not regenerate (avoids wiping user edits / retries).
    if (sessionReadyRef.current) return
    sessionReadyRef.current = true
    setDisplayName('')
    setDescription('')
    setFieldErrors({})
    setSubmitError(null)
    setResponseUncertain(false)
    setSubmitting(false)
    idempotencyKeyRef.current = null
    payloadFpRef.current = null
    submitLockRef.current = false
    applyGeneratedKey()
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
    if (keyGenError) {
      errors.client_key = keyGenError
    } else if (!key) {
      errors.client_key =
        '서비스 키가 없습니다. 「키 재생성」으로 만들거나 직접 입력하세요.'
    } else if (key.length > KEY_MAX) {
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

  const fieldsLocked = submitting || responseUncertain

  const handleClose = () => {
    if (submitting) return
    if (responseUncertain) {
      if (
        !window.confirm(
          '등록 응답이 불확실합니다. 지금 닫으면 목록에서 동일 서비스 키 등록 여부를 확인하기 어렵고, 이후 다른 내용으로 다시 등록하면 중복 등록될 수 있습니다. 닫으시겠습니까?',
        )
      ) {
        return
      }
      onClose()
      return
    }
    if (dirty && !window.confirm('작성 중인 내용이 있습니다. 닫으시겠습니까?')) {
      return
    }
    onClose()
  }

  const handleRegenerate = () => {
    if (submitting || submitLockRef.current || responseUncertain) return
    applyGeneratedKey()
    setSubmitError(null)
  }

  const handleAcknowledgeUncertainAndRegen = () => {
    if (submitting || submitLockRef.current) return
    // Explicit recovery only: unlock fields, clear prior Idempotency-Key, new service key.
    setResponseUncertain(false)
    idempotencyKeyRef.current = null
    payloadFpRef.current = null
    applyGeneratedKey()
    setSubmitError(null)
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (submitLockRef.current || submitting) return
    if (keyGenError) {
      setSubmitError(
        '서비스 키를 만들 수 없어 등록할 수 없습니다. 브라우저를 확인한 뒤 「키 재생성」을 시도하세요.',
      )
      return
    }
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
      setResponseUncertain(false)
      onCreated(created)
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === 'IDEMPOTENCY_KEY_CONFLICT') {
          idempotencyKeyRef.current = null
          payloadFpRef.current = null
        }
        // Known API outcomes are not "uncertain" — keep key for conflict review.
        setResponseUncertain(false)
      } else {
        // Network / abort / unknown — protect against casual key regen + duplicate POST.
        setResponseUncertain(true)
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
        <ul className="panel-hint-list">
          <li>
            서비스 키는 AI 호출 시스템을 구분하는 식별값이며, API 인증키나
            비밀번호가 아닙니다.
          </li>
          <li>
            등록만으로 다른 프로젝트에{' '}
            <span className="mono">X-AI-Client</span> 헤더가 자동 설정되지
            않습니다. 각 프로젝트의 Gateway 공통 클라이언트에 적용하세요.
          </li>
          <li>
            환경변수 <span className="mono">MODELOPS_CLIENT_KEY</span> 사용을
            권장합니다.
          </li>
          <li>
            서비스 키는 등록 후 변경할 수 없습니다. 대소문자를 구분합니다.
          </li>
        </ul>
        {submitError ? (
          <SectionError title="등록 오류" message={submitError} />
        ) : null}
        {responseUncertain ? (
          <div
            className="panel-hint panel-hint--warn"
            data-testid="create-uncertain-banner"
          >
            <p>
              응답이 불확실합니다. 목록에서 동일 서비스 키 등록 여부를 먼저
              확인하세요. 입력·키 재생성은 잠겨 있으며, 같은 내용으로 다시
              등록하면 이전 Idempotency-Key가 재사용됩니다. 확인 후 새 키로
              다시 작성하려면 아래 버튼을 사용하세요.
            </p>
            <button
              type="button"
              className="btn btn--ghost btn--small"
              disabled={submitting}
              onClick={handleAcknowledgeUncertainAndRegen}
            >
              확인했고 새 키로 다시 작성
            </button>
          </div>
        ) : null}
        {keyGenError ? (
          <SectionError title="키 생성 오류" message={keyGenError} />
        ) : null}
        <form onSubmit={(e) => void onSubmit(e)}>
          <label className="toolbar__field" htmlFor="create-client-key">
            <span>
              <TermLabel
                text="서비스 키"
                apiTerm="client_key"
                help={CLIENT_FIELD_HELP.clientKey}
              />{' '}
              <span className="form-field-hint">
                필수 · 최대 {KEY_MAX}자 · 자동 생성
              </span>
            </span>
            <input
              id="create-client-key"
              name="client_key"
              value={clientKey}
              onChange={(e) => {
                if (responseUncertain) return
                setClientKey(e.target.value)
                setKeyGenError(null)
              }}
              maxLength={KEY_MAX + 8}
              autoComplete="off"
              spellCheck={false}
              disabled={fieldsLocked}
              readOnly={responseUncertain}
              aria-invalid={Boolean(fieldErrors.client_key || keyGenError)}
              placeholder="svc-…"
              className="mono"
            />
            <div className="toolbar__actions" style={{ marginTop: '0.35rem' }}>
              <button
                type="button"
                className="btn btn--ghost btn--small"
                disabled={fieldsLocked}
                onClick={handleRegenerate}
                data-testid="client-key-regenerate"
              >
                키 재생성
              </button>
            </div>
            <ClientKeyCopyActions
              clientKey={clientKey}
              disabled={submitting || !clientKey.trim()}
            />
            <p className="form-field-hint">
              {CLIENT_FIELD_HELP.clientKeyIntegration}
            </p>
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
              onChange={(e) => {
                if (responseUncertain) return
                setDisplayName(e.target.value)
              }}
              maxLength={NAME_MAX + 8}
              disabled={fieldsLocked}
              readOnly={responseUncertain}
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
              onChange={(e) => {
                if (responseUncertain) return
                setDescription(e.target.value)
              }}
              rows={3}
              disabled={fieldsLocked}
              readOnly={responseUncertain}
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
            <button
              type="submit"
              className="btn"
              disabled={submitting || Boolean(keyGenError)}
            >
              {submitting ? '등록 중…' : '등록'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
