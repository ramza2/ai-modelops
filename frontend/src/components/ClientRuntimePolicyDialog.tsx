import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import {
  getClient,
  getClientRuntimePolicy,
  putClientRuntimePolicy,
  type PutClientRuntimePolicyBody,
} from '../api/clients'
import type { ClientApp, ClientRuntimePolicy } from '../api/types'
import {
  formatPolicyScalar,
  formatPriorityCompare,
  parseOptionalPositiveLimit,
  parseOptionalPriority,
  runtimePolicyBodiesEqual,
  type RuntimePolicyFormBody,
} from '../utils/runtimePolicyForm'
import { CLIENT_FIELD_HELP } from '../utils/uiLabels'
import { HelpHint, TermLabel } from './HelpHint'
import { SectionError } from './SectionError'

export type ClientRuntimePolicyDialogProps = {
  open: boolean
  client: ClientApp
  /** Current policy row, or null when creating. */
  policy: ClientRuntimePolicy | null
  onClose: () => void
  /** Called after successful PUT; parent should re-GET policy (and optionally client). */
  onSaved: () => void
}

type FieldErrors = {
  max_input_tokens?: string
  max_output_tokens?: string
  max_concurrent_requests?: string
  priority?: string
}

type Step = 'edit' | 'confirm'

function nullableToInput(value: number | null): string {
  return value === null || value === undefined ? '' : String(value)
}

function policyErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) {
      return '대상 사용 시스템 또는 정책을 찾을 수 없습니다(404). 화면을 새로고침하세요.'
    }
    if (err.status === 422) {
      return `입력 검증 실패(422): ${err.message}`
    }
    return `저장 실패(${err.status}${err.code ? ` ${err.code}` : ''}): ${err.message}`
  }
  if (err instanceof Error) {
    return `${err.message} 응답이 불확실할 수 있습니다. 자동으로 다시 저장하지 않습니다. 「최신 값 다시 확인」으로 서버 상태를 조회한 뒤, 이미 요청값과 같은지 확인하세요.`
  }
  return '저장에 실패했습니다. 최신 정책을 다시 확인하세요.'
}

export function ClientRuntimePolicyDialog({
  open,
  client,
  policy,
  onClose,
  onSaved,
}: ClientRuntimePolicyDialogProps) {
  const creating = policy === null
  const [step, setStep] = useState<Step>('edit')
  const [isEnabled, setIsEnabled] = useState(false)
  const [maxInput, setMaxInput] = useState('')
  const [maxOutput, setMaxOutput] = useState('')
  const [maxConcurrent, setMaxConcurrent] = useState('')
  const [priority, setPriority] = useState('')
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [consent, setConsent] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [revalidating, setRevalidating] = useState(false)
  const [pendingBody, setPendingBody] = useState<RuntimePolicyFormBody | null>(
    null,
  )
  const [responseUncertain, setResponseUncertain] = useState(false)

  const baselineRef = useRef<RuntimePolicyFormBody | null>(null)
  const baselineClientRef = useRef<ClientApp | null>(null)
  const baselinePolicyRef = useRef<ClientRuntimePolicy | null>(null)
  const submitLockRef = useRef(false)
  const openRef = useRef(false)
  const sessionGenRef = useRef(0)
  const sessionClientIdRef = useRef<string | null>(null)

  const initFromProps = (c: ClientApp, p: ClientRuntimePolicy | null) => {
    sessionGenRef.current += 1
    sessionClientIdRef.current = c.id
    baselineClientRef.current = c
    baselinePolicyRef.current = p
    const baseline: RuntimePolicyFormBody = p
      ? {
          is_enabled: p.is_enabled,
          max_input_tokens: p.max_input_tokens,
          max_output_tokens: p.max_output_tokens,
          max_concurrent_requests: p.max_concurrent_requests,
          priority: p.priority,
        }
      : {
          is_enabled: false,
          max_input_tokens: null,
          max_output_tokens: null,
          max_concurrent_requests: null,
          priority: null,
        }
    baselineRef.current = baseline
    setIsEnabled(baseline.is_enabled)
    setMaxInput(nullableToInput(baseline.max_input_tokens))
    setMaxOutput(nullableToInput(baseline.max_output_tokens))
    setMaxConcurrent(nullableToInput(baseline.max_concurrent_requests))
    setPriority(nullableToInput(baseline.priority))
    setFieldErrors({})
    setSubmitError(null)
    setConsent(false)
    setSubmitting(false)
    setRevalidating(false)
    setPendingBody(null)
    setResponseUncertain(false)
    setStep('edit')
    submitLockRef.current = false
  }

  useEffect(() => {
    if (!open) {
      openRef.current = false
      sessionClientIdRef.current = null
      return
    }
    openRef.current = true
    // Re-init when opening or when Client / policy identity changes.
    const policyId = policy?.id ?? null
    const needInit =
      sessionClientIdRef.current !== client.id ||
      (baselinePolicyRef.current?.id ?? null) !== policyId ||
      !baselineRef.current
    if (needInit) {
      initFromProps(client, policy)
    }
    return () => {
      openRef.current = false
      sessionGenRef.current += 1
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional identity gates
  }, [open, client.id, policy?.id])

  if (!open) return null

  const parseBody = (): RuntimePolicyFormBody | null => {
    const errors: FieldErrors = {}
    const input = parseOptionalPositiveLimit(maxInput, '입력 토큰 상한')
    if (!input.ok) errors.max_input_tokens = input.error
    const output = parseOptionalPositiveLimit(maxOutput, '출력 토큰 상한')
    if (!output.ok) errors.max_output_tokens = output.error
    const concurrent = parseOptionalPositiveLimit(
      maxConcurrent,
      '동시 요청 상한',
    )
    if (!concurrent.ok) errors.max_concurrent_requests = concurrent.error
    const prio = parseOptionalPriority(priority)
    if (!prio.ok) errors.priority = prio.error
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return null
    return {
      is_enabled: isEnabled,
      max_input_tokens: input.ok ? input.value : null,
      max_output_tokens: output.ok ? output.value : null,
      max_concurrent_requests: concurrent.ok ? concurrent.value : null,
      priority: prio.ok ? prio.value : null,
    }
  }

  const dirty =
    baselineRef.current !== null &&
    (() => {
      const parsed = parseBodyQuiet()
      if (!parsed || !baselineRef.current) return true
      return !runtimePolicyBodiesEqual(parsed, baselineRef.current)
    })()

  function parseBodyQuiet(): RuntimePolicyFormBody | null {
    const input = parseOptionalPositiveLimit(maxInput, '입력 토큰 상한')
    const output = parseOptionalPositiveLimit(maxOutput, '출력 토큰 상한')
    const concurrent = parseOptionalPositiveLimit(
      maxConcurrent,
      '동시 요청 상한',
    )
    const prio = parseOptionalPriority(priority)
    if (!input.ok || !output.ok || !concurrent.ok || !prio.ok) return null
    return {
      is_enabled: isEnabled,
      max_input_tokens: input.value,
      max_output_tokens: output.value,
      max_concurrent_requests: concurrent.value,
      priority: prio.value,
    }
  }

  const handleClose = () => {
    if (submitting || revalidating) return
    if (
      dirty &&
      !window.confirm('작성 중인 정책 내용이 있습니다. 닫으시겠습니까?')
    ) {
      return
    }
    onClose()
  }

  const goConfirm = (e: FormEvent) => {
    e.preventDefault()
    if (submitLockRef.current || submitting || revalidating) return
    const body = parseBody()
    if (!body) return
    if (baselineRef.current && runtimePolicyBodiesEqual(body, baselineRef.current)) {
      setSubmitError('변경된 내용이 없습니다. 저장하지 않습니다.')
      return
    }
    setPendingBody(body)
    setConsent(false)
    setSubmitError(null)
    setResponseUncertain(false)
    setStep('confirm')
  }

  const stillCurrentSession = (sessionGen: number, lockedClientId: string) =>
    openRef.current &&
    sessionClientIdRef.current === lockedClientId &&
    sessionGenRef.current === sessionGen

  const reloadServerSnapshot = async () => {
    if (submitLockRef.current || submitting) return
    const lockedClientId = client.id
    const sessionGen = sessionGenRef.current
    setRevalidating(true)
    setSubmitError(null)
    try {
      const [latestClient, latestPolicyResp] = await Promise.all([
        getClient(lockedClientId),
        getClientRuntimePolicy(lockedClientId),
      ])
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      if (latestClient.id !== lockedClientId) {
        setSubmitError('대상 사용 시스템이 변경되었습니다.')
        return
      }
      const serverPolicy = latestPolicyResp.policy
      const pending = pendingBody
      if (
        pending &&
        serverPolicy &&
        runtimePolicyBodiesEqual(
          {
            is_enabled: serverPolicy.is_enabled,
            max_input_tokens: serverPolicy.max_input_tokens,
            max_output_tokens: serverPolicy.max_output_tokens,
            max_concurrent_requests: serverPolicy.max_concurrent_requests,
            priority: serverPolicy.priority,
          },
          pending,
        )
      ) {
        setSubmitError(
          '서버 정책이 이미 요청값과 같습니다. 자동 재저장하지 않습니다. 확인 후 창을 닫거나 상세를 새로고침하세요.',
        )
      } else {
        setSubmitError(
          '서버 최신 값을 다시 읽었습니다. 작성 중인 값은 유지했습니다. 차이가 있으면 상세를 확인한 뒤 유지된 입력을 검토해 다시 저장하세요. 자동 PUT 재시도는 하지 않습니다. GET→PUT은 원자적이지 않습니다.',
        )
      }
      // Refresh baselines for next conflict check without wiping draft inputs.
      baselineClientRef.current = latestClient
      baselinePolicyRef.current = serverPolicy
    } catch (err) {
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      setSubmitError(policyErrorMessage(err))
    } finally {
      if (sessionGenRef.current === sessionGen) {
        setRevalidating(false)
      }
    }
  }

  const executePut = async () => {
    if (submitLockRef.current || submitting || revalidating) return
    if (!consent || !pendingBody) return
    const baselineClient = baselineClientRef.current
    if (!baselineClient) return

    const lockedClientId = baselineClient.id
    const lockedClientKey = baselineClient.client_key
    const lockedActive = baselineClient.is_active
    const lockedClientUpdatedAt = baselineClient.updated_at
    const lockedPolicy = baselinePolicyRef.current
    const sessionGen = sessionGenRef.current
    const body: PutClientRuntimePolicyBody = { ...pendingBody }

    submitLockRef.current = true
    setSubmitting(true)
    setRevalidating(true)
    setSubmitError(null)
    setResponseUncertain(false)
    try {
      const [latestClient, latestPolicyResp] = await Promise.all([
        getClient(lockedClientId),
        getClientRuntimePolicy(lockedClientId),
      ])
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      if (
        latestClient.id !== lockedClientId ||
        latestClient.client_key !== lockedClientKey
      ) {
        setSubmitError(
          '대상 사용 시스템이 변경되었습니다. 저장을 중단했습니다. 작성 중인 값은 유지했습니다.',
        )
        return
      }
      if (
        latestClient.is_active !== lockedActive ||
        latestClient.updated_at !== lockedClientUpdatedAt
      ) {
        setSubmitError(
          `ClientApp 상태가 변경되어 저장을 중단했습니다(is_active=${String(latestClient.is_active)}, updated_at=${latestClient.updated_at}). 작성 중인 값은 유지했습니다. GET→PUT은 원자적이지 않으며 Backend ETag/If-Match는 없습니다.`,
        )
        baselineClientRef.current = latestClient
        baselinePolicyRef.current = latestPolicyResp.policy
        return
      }
      const latestPolicy = latestPolicyResp.policy
      if (lockedPolicy === null) {
        if (latestPolicy !== null) {
          setSubmitError(
            `다른 변경으로 정책 행이 이미 생겼습니다(policy id=${latestPolicy.id}, updated_at=${latestPolicy.updated_at}). PUT을 보내지 않았습니다. 작성 중인 값은 유지했습니다.`,
          )
          baselinePolicyRef.current = latestPolicy
          return
        }
      } else {
        if (latestPolicy === null) {
          setSubmitError(
            '정책 행이 없어졌습니다. PUT을 보내지 않았습니다. 작성 중인 값은 유지했습니다.',
          )
          baselinePolicyRef.current = null
          return
        }
        if (
          latestPolicy.id !== lockedPolicy.id ||
          latestPolicy.updated_at !== lockedPolicy.updated_at
        ) {
          setSubmitError(
            `정책이 다른 변경으로 갱신되었습니다(서버 updated_at=${latestPolicy.updated_at}). PUT을 보내지 않았습니다. 작성 중인 값은 유지했습니다.`,
          )
          baselinePolicyRef.current = latestPolicy
          return
        }
      }
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      setRevalidating(false)
      await putClientRuntimePolicy(lockedClientId, body)
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      onSaved()
    } catch (err) {
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      if (!(err instanceof ApiError)) {
        setResponseUncertain(true)
      }
      setSubmitError(policyErrorMessage(err))
    } finally {
      if (sessionGenRef.current === sessionGen) {
        submitLockRef.current = false
        setSubmitting(false)
        setRevalidating(false)
      } else {
        submitLockRef.current = false
      }
    }
  }

  const baseline = baselineRef.current
  const title = creating ? '새 런타임 정책 만들기' : '런타임 정책 수정'

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="client-policy-dialog-title"
      data-testid="client-runtime-policy-dialog"
    >
      <div className="modal-card">
        <h2 id="client-policy-dialog-title">{title}</h2>
        <p className="panel-hint">
          PUT은 전체 필드 교체(upsert)입니다. 빈 숫자 칸은 null(제한 없음)로
          저장됩니다. Idempotency-Key는 사용하지 않습니다.
        </p>
        <p className="panel-hint secondary-text">
          대상: <span className="mono">{client.client_key}</span> /{' '}
          {client.display_name}
          {!client.is_active
            ? ' — 등록이 비활성이면 Gateway 정책 스냅샷에 올라가지 않을 수 있습니다.'
            : null}
        </p>

        {submitError ? (
          <SectionError title="정책 저장 오류" message={submitError} />
        ) : null}
        {responseUncertain ? (
          <div
            className="panel-hint panel-hint--warn"
            data-testid="policy-uncertain-banner"
          >
            <p>
              응답이 불확실합니다. 자동으로 PUT을 다시 보내지 않습니다. 목록에서
              등록 여부를 확인한 뒤 「최신 값 다시 확인」으로 서버 상태를
              조회하세요.
            </p>
            <button
              type="button"
              className="btn btn--ghost btn--small"
              disabled={submitting || revalidating}
              onClick={() => void reloadServerSnapshot()}
            >
              최신 값 다시 확인
            </button>
          </div>
        ) : null}

        {step === 'edit' ? (
          <form onSubmit={goConfirm}>
            <label className="toolbar__field" htmlFor="policy-is-enabled">
              <span>
                <TermLabel
                  text="정책 사용 여부"
                  apiTerm="is_enabled"
                  help={CLIENT_FIELD_HELP.policyEnabled}
                />
              </span>
              <select
                id="policy-is-enabled"
                value={isEnabled ? 'true' : 'false'}
                disabled={submitting}
                onChange={(e) => setIsEnabled(e.target.value === 'true')}
              >
                <option value="false">사용 안함</option>
                <option value="true">사용</option>
              </select>
              <span className="form-field-hint">
                사용 안함은 AI 호출 차단이 아니라 클라이언트별 정책 제한
                미적용입니다. 새 정책 기본값은 사용 안함입니다.
              </span>
            </label>

            <label className="toolbar__field" htmlFor="policy-max-input">
              <span>
                <TermLabel
                  text="입력 토큰 상한"
                  apiTerm="max_input_tokens"
                  help={CLIENT_FIELD_HELP.maxInputTokens}
                />{' '}
                <span className="form-field-hint">선택 · 양의 정수</span>
              </span>
              <input
                id="policy-max-input"
                inputMode="numeric"
                value={maxInput}
                disabled={submitting}
                onChange={(e) => setMaxInput(e.target.value)}
                placeholder="비우면 null (제한 없음)"
                aria-invalid={Boolean(fieldErrors.max_input_tokens)}
              />
              <HelpHint label="입력 토큰">
                한 번의 Chat 요청에서 사용할 수 있는 프롬프트 크기 상한입니다.
                VLM 등 토큰 계산을 지원하지 못하는 요청은 거부될 수 있습니다.
              </HelpHint>
              {fieldErrors.max_input_tokens ? (
                <span className="form-field-error">
                  {fieldErrors.max_input_tokens}
                </span>
              ) : null}
            </label>

            <label className="toolbar__field" htmlFor="policy-max-output">
              <span>
                <TermLabel
                  text="출력 토큰 상한"
                  apiTerm="max_output_tokens"
                  help={CLIENT_FIELD_HELP.maxOutputTokens}
                />{' '}
                <span className="form-field-hint">선택 · 양의 정수</span>
              </span>
              <input
                id="policy-max-output"
                inputMode="numeric"
                value={maxOutput}
                disabled={submitting}
                onChange={(e) => setMaxOutput(e.target.value)}
                placeholder="비우면 null (제한 없음)"
                aria-invalid={Boolean(fieldErrors.max_output_tokens)}
              />
              {fieldErrors.max_output_tokens ? (
                <span className="form-field-error">
                  {fieldErrors.max_output_tokens}
                </span>
              ) : null}
            </label>

            <label className="toolbar__field" htmlFor="policy-max-concurrent">
              <span>
                <TermLabel
                  text="동시 요청 상한"
                  apiTerm="max_concurrent_requests"
                  help={CLIENT_FIELD_HELP.maxConcurrentRequests}
                />{' '}
                <span className="form-field-hint">선택 · 양의 정수</span>
              </span>
              <input
                id="policy-max-concurrent"
                inputMode="numeric"
                value={maxConcurrent}
                disabled={submitting}
                onChange={(e) => setMaxConcurrent(e.target.value)}
                placeholder="비우면 null (제한 없음)"
                aria-invalid={Boolean(fieldErrors.max_concurrent_requests)}
              />
              <span className="form-field-hint">
                Gateway 프로세스별 제한입니다. 분산 세마포어가 아닙니다.
              </span>
              {fieldErrors.max_concurrent_requests ? (
                <span className="form-field-error">
                  {fieldErrors.max_concurrent_requests}
                </span>
              ) : null}
            </label>

            <label className="toolbar__field" htmlFor="policy-priority">
              <span>
                <TermLabel
                  text="요청 우선순위"
                  apiTerm="priority"
                  help={CLIENT_FIELD_HELP.priority}
                />{' '}
                <span className="form-field-hint">선택 · 정수(0 허용)</span>
              </span>
              <input
                id="policy-priority"
                inputMode="numeric"
                value={priority}
                disabled={submitting}
                onChange={(e) => setPriority(e.target.value)}
                placeholder="비우면 null (미설정)"
                aria-invalid={Boolean(fieldErrors.priority)}
              />
              <span className="form-field-hint">
                null=미설정, 0=중립값(null과 다름), 비0=신뢰된 priority
                scheduler가 없으면 요청이 거부될 수 있습니다.
              </span>
              {fieldErrors.priority ? (
                <span className="form-field-error">{fieldErrors.priority}</span>
              ) : null}
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
                저장 검토…
              </button>
            </div>
          </form>
        ) : null}

        {step === 'confirm' && pendingBody && baseline ? (
          <div data-testid="policy-confirm-step">
            {revalidating ? (
              <p className="metric-line" role="status">
                저장 전 최신 상태를 확인하는 중…
              </p>
            ) : null}
            <h3 className="detail-panel__subheading">변경 비교</h3>
            <dl className="meta-grid">
              <div>
                <dt>정책 사용</dt>
                <dd>
                  {formatPolicyScalar(baseline.is_enabled)} →{' '}
                  {formatPolicyScalar(pendingBody.is_enabled)}
                </dd>
              </div>
              <div>
                <dt>입력 토큰 상한</dt>
                <dd>
                  {formatPolicyScalar(baseline.max_input_tokens)} →{' '}
                  {formatPolicyScalar(pendingBody.max_input_tokens)}
                </dd>
              </div>
              <div>
                <dt>출력 토큰 상한</dt>
                <dd>
                  {formatPolicyScalar(baseline.max_output_tokens)} →{' '}
                  {formatPolicyScalar(pendingBody.max_output_tokens)}
                </dd>
              </div>
              <div>
                <dt>동시 요청 상한</dt>
                <dd>
                  {formatPolicyScalar(baseline.max_concurrent_requests)} →{' '}
                  {formatPolicyScalar(pendingBody.max_concurrent_requests)}
                </dd>
              </div>
              <div>
                <dt>요청 우선순위</dt>
                <dd>
                  {formatPriorityCompare(baseline.priority)} →{' '}
                  {formatPriorityCompare(pendingBody.priority)}
                </dd>
              </div>
            </dl>
            <ul className="panel-hint-list">
              <li>
                정책 사용 안함은 Gateway 호출 차단이 아닙니다. 클라이언트별 제한만
                미적용됩니다.
              </li>
              <li>
                ClientApp이 비활성이면 정책이 Gateway 스냅샷에 적용되지 않을 수
                있습니다.
              </li>
              <li>
                X-AI-Client 헤더가 없거나 등록된 client_key와 다르면 의도한 정책이
                적용되지 않을 수 있습니다.
              </li>
              <li>
                max_input_tokens 적용 시 토큰 계산을 지원하지 못하는 요청(일부
                VLM 등)은 거부될 수 있습니다.
              </li>
              <li>
                max_output_tokens를 넘는 요청은 거부될 수 있습니다(조용한 자르기
                없음).
              </li>
              <li>
                동시 요청 상한은 Gateway 프로세스별입니다.
              </li>
              <li>
                priority가 0이 아니면 신뢰된 priority scheduler 증거가 없을 때
                요청이 거부될 수 있습니다.
              </li>
              <li>
                DB 저장과 Gateway 적용 확인은 별개이며 전파 지연이 있을 수
                있습니다. Frontend 확인창은 SSO/RBAC가 아닙니다.
              </li>
              <li>
                GET→PUT은 원자적이지 않으며 Backend ETag/If-Match는 없습니다.
              </li>
            </ul>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={consent}
                disabled={submitting || revalidating}
                onChange={(e) => setConsent(e.target.checked)}
              />
              <span>
                위 영향과 변경 내용을 확인했으며, 전체 교체 PUT으로 저장하는 것에
                동의합니다.
              </span>
            </label>
            <div className="toolbar__actions" style={{ marginTop: '1rem' }}>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={submitting || revalidating}
                onClick={() => {
                  setStep('edit')
                  setConsent(false)
                  setSubmitError(null)
                }}
              >
                뒤로
              </button>
              <button
                type="button"
                className="btn"
                disabled={!consent || submitting || revalidating}
                onClick={() => void executePut()}
              >
                {submitting || revalidating ? '저장 중…' : '동의하고 저장'}
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
