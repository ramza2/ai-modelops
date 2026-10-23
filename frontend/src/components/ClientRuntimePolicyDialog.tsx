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

export type ClientRuntimePolicyDialogCloseOpts = {
  /** Re-GET detail policy after dismiss; never means save succeeded. */
  refresh?: boolean
}

export type ClientRuntimePolicyDialogProps = {
  open: boolean
  client: ClientApp
  /** Current policy row, or null when creating. */
  policy: ClientRuntimePolicy | null
  onClose: (opts?: ClientRuntimePolicyDialogCloseOpts) => void
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

/**
 * Blocks direct PUT until the operator explicitly re-reviews.
 * - conflict: concurrent ClientApp/policy change during preflight
 * - uncertain: PUT outcome unknown (network); must reload policy
 * - matched: server already equals pending; no further PUT
 * - reload_failed: latest policy GET failed; keep blocking PUT
 */
type SaveGate = null | 'conflict' | 'uncertain' | 'matched' | 'reload_failed'

function nullableToInput(value: number | null): string {
  return value === null || value === undefined ? '' : String(value)
}

function policyToBody(p: ClientRuntimePolicy): RuntimePolicyFormBody {
  return {
    is_enabled: p.is_enabled,
    max_input_tokens: p.max_input_tokens,
    max_output_tokens: p.max_output_tokens,
    max_concurrent_requests: p.max_concurrent_requests,
    priority: p.priority,
  }
}

function emptyCreateBody(): RuntimePolicyFormBody {
  return {
    is_enabled: false,
    max_input_tokens: null,
    max_output_tokens: null,
    max_concurrent_requests: null,
    priority: null,
  }
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
    return `${err.message} 응답이 불확실할 수 있습니다. 자동으로 다시 저장하지 않습니다. 「최신 값 다시 확인」으로 최신 런타임 정책을 조회한 뒤, 이미 요청값과 같은지 확인하세요.`
  }
  return '저장에 실패했습니다. 최신 런타임 정책을 다시 확인하세요.'
}

function PolicyCompareRows({
  leftLabel,
  rightLabel,
  left,
  right,
}: {
  leftLabel: string
  rightLabel: string
  left: RuntimePolicyFormBody
  right: RuntimePolicyFormBody
}) {
  return (
    <dl className="meta-grid" data-testid="policy-compare-rows">
      <div>
        <dt>정책 사용 ({leftLabel} → {rightLabel})</dt>
        <dd>
          {formatPolicyScalar(left.is_enabled)} →{' '}
          {formatPolicyScalar(right.is_enabled)}
        </dd>
      </div>
      <div>
        <dt>입력 토큰 상한</dt>
        <dd>
          {formatPolicyScalar(left.max_input_tokens)} →{' '}
          {formatPolicyScalar(right.max_input_tokens)}
        </dd>
      </div>
      <div>
        <dt>출력 토큰 상한</dt>
        <dd>
          {formatPolicyScalar(left.max_output_tokens)} →{' '}
          {formatPolicyScalar(right.max_output_tokens)}
        </dd>
      </div>
      <div>
        <dt>동시 요청 상한</dt>
        <dd>
          {formatPolicyScalar(left.max_concurrent_requests)} →{' '}
          {formatPolicyScalar(right.max_concurrent_requests)}
        </dd>
      </div>
      <div>
        <dt>요청 우선순위</dt>
        <dd>
          {formatPriorityCompare(left.priority)} →{' '}
          {formatPriorityCompare(right.priority)}
        </dd>
      </div>
    </dl>
  )
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
  const [saveGate, setSaveGate] = useState<SaveGate>(null)
  /** Latest server policy body shown after conflict / reload (null = no row). */
  const [serverSnapshot, setServerSnapshot] =
    useState<RuntimePolicyFormBody | null>(null)
  const [serverPolicyMeta, setServerPolicyMeta] = useState<{
    id: string | null
    updated_at: string | null
  } | null>(null)

  const baselineRef = useRef<RuntimePolicyFormBody | null>(null)
  const baselineClientRef = useRef<ClientApp | null>(null)
  const baselinePolicyRef = useRef<ClientRuntimePolicy | null>(null)
  /** True when dialog opened with no policy row (create). */
  const creatingRef = useRef(true)
  const submitLockRef = useRef(false)
  const openRef = useRef(false)
  const sessionGenRef = useRef(0)
  const sessionClientIdRef = useRef<string | null>(null)

  const initFromProps = (c: ClientApp, p: ClientRuntimePolicy | null) => {
    sessionGenRef.current += 1
    sessionClientIdRef.current = c.id
    baselineClientRef.current = c
    baselinePolicyRef.current = p
    creatingRef.current = p === null
    const baseline: RuntimePolicyFormBody = p
      ? policyToBody(p)
      : emptyCreateBody()
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
    setSaveGate(null)
    setServerSnapshot(null)
    setServerPolicyMeta(null)
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

  const dirty =
    baselineRef.current !== null &&
    (() => {
      const parsed = parseBodyQuiet()
      if (!parsed || !baselineRef.current) return true
      // Creating a row with defaults still counts as dirty work-in-progress.
      if (creatingRef.current) return true
      return !runtimePolicyBodiesEqual(parsed, baselineRef.current)
    })()

  const putBlocked =
    saveGate === 'conflict' ||
    saveGate === 'uncertain' ||
    saveGate === 'matched' ||
    saveGate === 'reload_failed'

  const handleClose = () => {
    if (submitting || revalidating) return
    if (saveGate === 'uncertain' || saveGate === 'reload_failed') {
      if (
        !window.confirm(
          '저장 응답이 불확실하거나 최신 런타임 정책 조회에 실패했습니다. 지금 닫으면 이 창에서 서버 반영 여부를 더 확인하지 않습니다. 닫은 뒤 상세에서 최신 정책을 다시 불러옵니다. 닫으시겠습니까?',
        )
      ) {
        return
      }
      onClose({ refresh: true })
      return
    }
    if (saveGate === 'matched') {
      // Already confirmed equal — dismiss without further PUT; refresh detail.
      onClose({ refresh: true })
      return
    }
    if (saveGate === 'conflict') {
      if (
        !window.confirm(
          '동시 변경 충돌로 저장이 중단된 상태입니다. 작성 중인 값은 버려지며, 닫은 뒤 상세에서 최신 정책을 다시 불러옵니다. 닫으시겠습니까?',
        )
      ) {
        return
      }
      onClose({ refresh: true })
      return
    }
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
    // Conflict can only be cleared by「최신 서버 확인 후 다시 검토」.
    if (saveGate === 'conflict') {
      setSubmitError(
        '동시 변경 충돌 상태입니다. 「뒤로 → 저장 검토」로는 해제되지 않습니다. 「최신 서버 확인 후 다시 검토」로 최신 정책을 비교 기준으로 반영한 뒤 다시 저장 검토·동의하세요.',
      )
      return
    }
    if (saveGate === 'uncertain' || saveGate === 'reload_failed') {
      setSubmitError(
        '응답이 불확실하거나 최신 정책 조회에 실패했습니다. 「최신 값 다시 확인」으로 최신 런타임 정책을 확인한 뒤에만 다시 검토할 수 있습니다.',
      )
      return
    }
    if (saveGate === 'matched') {
      setSubmitError(
        '서버 정책이 이미 요청값과 같습니다. 추가 PUT을 보내지 않습니다. 창을 닫거나 상세를 새로고침하세요.',
      )
      return
    }
    const body = parseBody()
    if (!body) return
    // Existing policy: block no-op PUT. Create (policy=null): allow defaults
    // so a disabled unrestricted row can be inserted.
    if (
      !creatingRef.current &&
      baselineRef.current &&
      runtimePolicyBodiesEqual(body, baselineRef.current)
    ) {
      setSubmitError('변경된 내용이 없습니다. 기존 정책에 대해 저장하지 않습니다.')
      return
    }
    setPendingBody(body)
    setConsent(false)
    setSubmitError(null)
    // Never clear conflict here — only acknowledgeServerAndReReview may.
    setSaveGate(null)
    setServerSnapshot(null)
    setServerPolicyMeta(null)
    setStep('confirm')
  }

  const stillCurrentSession = (sessionGen: number, lockedClientId: string) =>
    openRef.current &&
    sessionClientIdRef.current === lockedClientId &&
    sessionGenRef.current === sessionGen

  const applyConflict = (
    latestClient: ClientApp,
    latestPolicy: ClientRuntimePolicy | null,
    message: string,
  ) => {
    setConsent(false)
    setSaveGate('conflict')
    setServerSnapshot(latestPolicy ? policyToBody(latestPolicy) : null)
    setServerPolicyMeta({
      id: latestPolicy?.id ?? null,
      updated_at: latestPolicy?.updated_at ?? null,
    })
    baselineClientRef.current = latestClient
    baselinePolicyRef.current = latestPolicy
    setSubmitError(
      `${message} 작성 중인 값은 유지했습니다. 최신 서버 정책과 작성값을 비교한 뒤 「최신 서버 확인 후 다시 검토」를 눌러 저장 검토·동의를 다시 진행하세요. 조용히 덮어쓰지 않습니다. GET→PUT은 원자적이지 않으며 Backend ETag/If-Match는 없습니다.`,
    )
  }

  /** After conflict/uncertain mismatch: adopt server as new baseline base, keep draft inputs, return to edit. */
  const acknowledgeServerAndReReview = () => {
    if (submitting || revalidating || submitLockRef.current) return
    if (saveGate !== 'conflict') return
    const latestPolicy = baselinePolicyRef.current
    creatingRef.current = latestPolicy === null
    baselineRef.current = latestPolicy
      ? policyToBody(latestPolicy)
      : emptyCreateBody()
    setConsent(false)
    setSaveGate(null)
    setSubmitError(null)
    setServerSnapshot(null)
    setServerPolicyMeta(null)
    setStep('edit')
  }

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
        setConsent(false)
        setSaveGate('reload_failed')
        setSubmitError('대상 사용 시스템이 변경되었습니다. 추가 PUT을 차단합니다.')
        return
      }
      const serverPolicy = latestPolicyResp.policy
      const pending = pendingBody
      baselineClientRef.current = latestClient
      baselinePolicyRef.current = serverPolicy
      setServerSnapshot(serverPolicy ? policyToBody(serverPolicy) : null)
      setServerPolicyMeta({
        id: serverPolicy?.id ?? null,
        updated_at: serverPolicy?.updated_at ?? null,
      })
      setConsent(false)

      if (
        pending &&
        serverPolicy &&
        runtimePolicyBodiesEqual(policyToBody(serverPolicy), pending)
      ) {
        setSaveGate('matched')
        setSubmitError(
          '최신 런타임 정책이 이미 요청값과 같습니다. 저장이 완료되었을 수 있습니다. 추가 PUT을 보내지 않습니다. 확인 후 창을 닫거나 상세를 새로고침하세요.',
        )
        return
      }

      // Still uncertain/conflict path: show diff, require explicit re-review.
      setSaveGate('conflict')
      setSubmitError(
        '최신 런타임 정책을 다시 읽었습니다. 작성 중인 값은 유지했습니다. 서버값과 작성값이 다릅니다. 「최신 서버 확인 후 다시 검토」로 저장 검토·동의를 다시 진행하세요. 자동 PUT 재시도는 하지 않습니다. GET→PUT은 원자적이지 않습니다.',
      )
    } catch (err) {
      if (!stillCurrentSession(sessionGen, lockedClientId)) return
      setConsent(false)
      setSaveGate('reload_failed')
      setSubmitError(
        `${policyErrorMessage(err)} 최신 런타임 정책 조회에 실패하여 추가 PUT을 계속 차단합니다.`,
      )
    } finally {
      if (sessionGenRef.current === sessionGen) {
        setRevalidating(false)
      }
    }
  }

  const executePut = async () => {
    if (submitLockRef.current || submitting || revalidating) return
    if (!consent || !pendingBody) return
    if (putBlocked) {
      setSubmitError(
        '저장이 차단된 상태입니다. 최신 런타임 정책을 확인하고 다시 검토·동의한 뒤에만 PUT할 수 있습니다.',
      )
      return
    }
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
        applyConflict(
          latestClient,
          latestPolicyResp.policy,
          '대상 사용 시스템이 변경되어 저장을 중단했습니다.',
        )
        return
      }
      if (
        latestClient.is_active !== lockedActive ||
        latestClient.updated_at !== lockedClientUpdatedAt
      ) {
        applyConflict(
          latestClient,
          latestPolicyResp.policy,
          `ClientApp 상태가 변경되어 저장을 중단했습니다(is_active=${String(latestClient.is_active)}, updated_at=${latestClient.updated_at}).`,
        )
        return
      }
      const latestPolicy = latestPolicyResp.policy
      if (lockedPolicy === null) {
        if (latestPolicy !== null) {
          applyConflict(
            latestClient,
            latestPolicy,
            `다른 변경으로 정책 행이 이미 생겼습니다(policy id=${latestPolicy.id}, updated_at=${latestPolicy.updated_at}). PUT을 보내지 않았습니다.`,
          )
          return
        }
      } else {
        if (latestPolicy === null) {
          applyConflict(
            latestClient,
            null,
            '정책 행이 없어졌습니다. PUT을 보내지 않았습니다.',
          )
          return
        }
        if (
          latestPolicy.id !== lockedPolicy.id ||
          latestPolicy.updated_at !== lockedPolicy.updated_at
        ) {
          applyConflict(
            latestClient,
            latestPolicy,
            `정책이 다른 변경으로 갱신되었습니다(서버 updated_at=${latestPolicy.updated_at}). PUT을 보내지 않았습니다.`,
          )
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
      setConsent(false)
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        // Confirmed client/API outcome (400/404/409/422…) — not uncertain.
        setSaveGate(null)
        setSubmitError(policyErrorMessage(err))
      } else {
        // Network errors and HTTP 5xx: outcome may be ambiguous; no auto re-PUT.
        setSaveGate('uncertain')
        setSubmitError(
          err instanceof ApiError && err.status >= 500
            ? `서버 오류(${err.status}): ${err.message} 응답이 불확실할 수 있습니다. 자동으로 다시 저장하지 않습니다. 「최신 값 다시 확인」으로 최신 런타임 정책 반영 여부를 확인하세요.`
            : policyErrorMessage(err),
        )
      }
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
  const canConsentPut = Boolean(consent) && !putBlocked && !submitting && !revalidating

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
          {creating
            ? ' 정책 행이 없을 때 기본값(사용 안함·제한 null)을 그대로 저장하면 새 행이 생성됩니다.'
            : null}
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

        {saveGate === 'uncertain' || saveGate === 'reload_failed' ? (
          <div
            className="panel-hint panel-hint--warn"
            data-testid="policy-uncertain-banner"
          >
            <p>
              {saveGate === 'uncertain'
                ? '응답이 불확실합니다. 자동으로 PUT을 다시 보내지 않습니다. 「최신 값 다시 확인」으로 최신 런타임 정책을 조회하세요.'
                : '최신 런타임 정책 조회에 실패했습니다. 추가 PUT을 차단합니다. 다시 조회해 보세요.'}
            </p>
            <button
              type="button"
              className="btn btn--ghost btn--small"
              disabled={submitting || revalidating}
              onClick={() => void reloadServerSnapshot()}
              data-testid="policy-reload-server"
            >
              최신 값 다시 확인
            </button>
          </div>
        ) : null}

        {saveGate === 'matched' ? (
          <div
            className="panel-hint panel-hint--warn"
            data-testid="policy-matched-banner"
          >
            <p>
              서버 최신 런타임 정책이 요청값과 같습니다. 추가 PUT을 보내지
              않습니다.
            </p>
          </div>
        ) : null}

        {saveGate === 'conflict' && pendingBody ? (
          <div
            className="panel-hint panel-hint--warn"
            data-testid="policy-conflict-banner"
          >
            <p>
              동시 변경이 감지되어 PUT을 중단했습니다. 아래는 최신 서버 정책과
              작성 중인 값입니다. 작성값은 유지되며, 동의 상태는 무효화되었습니다.
              {serverPolicyMeta
                ? ` (서버 policy id=${serverPolicyMeta.id ?? 'null'}, updated_at=${serverPolicyMeta.updated_at ?? 'null'})`
                : null}
            </p>
            <h3 className="detail-panel__subheading">서버 최신 ↔ 작성 중</h3>
            <PolicyCompareRows
              leftLabel="서버"
              rightLabel="작성"
              left={
                serverSnapshot ?? {
                  is_enabled: false,
                  max_input_tokens: null,
                  max_output_tokens: null,
                  max_concurrent_requests: null,
                  priority: null,
                }
              }
              right={pendingBody}
            />
            {!serverSnapshot ? (
              <p className="form-field-hint">
                서버에 정책 행이 없습니다(policy = null).
              </p>
            ) : null}
            <button
              type="button"
              className="btn btn--ghost btn--small"
              disabled={submitting || revalidating}
              onClick={acknowledgeServerAndReReview}
              data-testid="policy-ack-conflict"
            >
              최신 서버 확인 후 다시 검토
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
                disabled={submitting || saveGate === 'uncertain' || saveGate === 'reload_failed' || saveGate === 'matched'}
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
                disabled={
                  submitting ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
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
                disabled={
                  submitting ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
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
                disabled={
                  submitting ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
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
                disabled={
                  submitting ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
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
              <button
                type="submit"
                className="btn"
                disabled={
                  submitting ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
              >
                저장 검토…
              </button>
              {putBlocked ? (
                <button
                  type="button"
                  className="btn btn--ghost"
                  disabled={submitting || revalidating}
                  onClick={handleClose}
                  data-testid="policy-dialog-close"
                >
                  닫기
                </button>
              ) : null}
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
            <PolicyCompareRows
              leftLabel="기준"
              rightLabel="저장 예정"
              left={baseline}
              right={pendingBody}
            />
            {creatingRef.current &&
            runtimePolicyBodiesEqual(pendingBody, emptyCreateBody()) ? (
              <p className="panel-hint" data-testid="policy-create-defaults-hint">
                정책 행이 없는 상태에서 기본값(사용 안함·제한 null)으로 새 정책
                행을 생성합니다. 기존 정책의 무변경 저장과는 다릅니다.
              </p>
            ) : null}
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
              <li>동시 요청 상한은 Gateway 프로세스별입니다.</li>
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
                disabled={submitting || revalidating || putBlocked}
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
                disabled={
                  submitting ||
                  revalidating ||
                  saveGate === 'uncertain' ||
                  saveGate === 'reload_failed' ||
                  saveGate === 'matched'
                }
                onClick={() => {
                  // conflict: allow returning to edit, but saveGate stays conflict
                  // until「최신 서버 확인 후 다시 검토」.
                  if (putBlocked && saveGate !== 'conflict') return
                  setStep('edit')
                  setConsent(false)
                }}
              >
                뒤로
              </button>
              <button
                type="button"
                className="btn"
                disabled={!canConsentPut}
                onClick={() => void executePut()}
                data-testid="policy-confirm-put"
              >
                {submitting || revalidating ? '저장 중…' : '동의하고 저장'}
              </button>
              {putBlocked ? (
                <button
                  type="button"
                  className="btn btn--ghost"
                  disabled={submitting || revalidating}
                  onClick={handleClose}
                  data-testid="policy-dialog-close"
                >
                  {saveGate === 'matched' ? '닫기 (추가 저장 없음)' : '닫기'}
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
