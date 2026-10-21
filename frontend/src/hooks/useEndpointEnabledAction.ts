import { useCallback, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { getEndpoint, updateEndpointEnabled } from '../api/endpoints'
import type { Endpoint } from '../api/types'
import type { EndpointEnabledMode } from '../components/EndpointEnabledConfirmDialog'

type UseEndpointEnabledActionOptions = {
  /** Called after a successful PATCH with the updated endpoint. */
  onSuccess: (updated: Endpoint, mode: EndpointEnabledMode) => void | Promise<void>
  /** Extra gate: when true, open/confirm are blocked. */
  isExternallyBusy?: () => boolean
}

/**
 * Shared 사용 중지 / 재활성화 flow:
 * open → re-fetch Endpoint → confirm → PATCH is_enabled → callback.
 */
export function useEndpointEnabledAction(
  options: UseEndpointEnabledActionOptions,
) {
  const onSuccessRef = useRef(options.onSuccess)
  onSuccessRef.current = options.onSuccess
  const busyRef = useRef(options.isExternallyBusy)
  busyRef.current = options.isExternallyBusy

  const [mode, setMode] = useState<EndpointEnabledMode | null>(null)
  const [draft, setDraft] = useState<Endpoint | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [consent, setConsent] = useState(false)
  const [aliasInput, setAliasInput] = useState('')
  const [resultMessage, setResultMessage] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const submittingRef = useRef(false)
  submittingRef.current = submitting

  const close = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setMode(null)
    setDraft(null)
    setRefreshing(false)
    setSubmitting(false)
    setError(null)
    setConsent(false)
    setAliasInput('')
  }, [])

  const open = useCallback(
    async (endpointId: string, nextMode: EndpointEnabledMode) => {
      if (submittingRef.current) return
      if (busyRef.current?.()) return

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      setMode(nextMode)
      setDraft(null)
      setError(null)
      setConsent(false)
      setAliasInput('')
      setResultMessage(null)
      setOpenError(null)
      setRefreshing(true)

      try {
        const fresh = await getEndpoint(endpointId, controller.signal)
        if (controller.signal.aborted) return
        if (nextMode === 'disable' && !fresh.is_enabled) {
          setError('이미 사용 중지된 별칭입니다. 최신 상태를 확인하세요.')
          setDraft(fresh)
          return
        }
        if (nextMode === 'enable' && fresh.is_enabled) {
          setError('이미 사용 중인 별칭입니다. 최신 상태를 확인하세요.')
          setDraft(fresh)
          return
        }
        setDraft(fresh)
      } catch (err) {
        if (controller.signal.aborted) return
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Endpoint를 다시 조회하지 못했습니다.'
        setOpenError(message)
        setMode(null)
        setDraft(null)
      } finally {
        if (!controller.signal.aborted) setRefreshing(false)
      }
    },
    [],
  )

  const confirm = useCallback(async () => {
    if (!mode || !draft || submittingRef.current) return
    if (busyRef.current?.()) return
    if (!consent) {
      setError('진행하려면 동의 확인이 필요합니다.')
      return
    }
    const requireAlias =
      mode === 'disable' && Boolean(draft.active_route?.deployment_id)
    if (requireAlias && aliasInput !== draft.alias) {
      setError('입력한 Alias가 현재 Endpoint Alias와 일치하지 않습니다.')
      return
    }

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setSubmitting(true)
    setError(null)

    try {
      const latest = await getEndpoint(draft.id, controller.signal)
      if (controller.signal.aborted) return
      if (mode === 'disable' && !latest.is_enabled) {
        setError(
          '상태가 변경되어 이미 사용 중지되었습니다. 화면을 새로고침하세요.',
        )
        setDraft(latest)
        return
      }
      if (mode === 'enable' && latest.is_enabled) {
        setError('상태가 변경되어 이미 사용 중입니다. 화면을 새로고침하세요.')
        setDraft(latest)
        return
      }
      if (
        mode === 'disable' &&
        Boolean(draft.active_route?.deployment_id) !==
          Boolean(latest.active_route?.deployment_id)
      ) {
        setError(
          'ACTIVE Route 상태가 변경되었습니다. 내용을 다시 확인한 뒤 진행하세요.',
        )
        setDraft(latest)
        setConsent(false)
        setAliasInput('')
        return
      }

      const updated = await updateEndpointEnabled(
        latest.id,
        mode === 'enable',
        controller.signal,
      )
      if (controller.signal.aborted) return
      setResultMessage(
        mode === 'disable'
          ? `별칭 ${updated.alias}을(를) 사용 중지했습니다.`
          : `별칭 ${updated.alias}을(를) 다시 사용하도록 설정했습니다.`,
      )
      close()
      await onSuccessRef.current(updated, mode)
    } catch (err) {
      if (controller.signal.aborted) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : mode === 'disable'
              ? '사용 중지에 실패했습니다.'
              : '재활성화에 실패했습니다.'
      setError(
        err instanceof ApiError && err.code
          ? `${message} (${err.code})`
          : message,
      )
    } finally {
      if (!controller.signal.aborted) setSubmitting(false)
    }
  }, [mode, draft, consent, aliasInput, close])

  return {
    open,
    close,
    confirm,
    mode,
    draft,
    refreshing,
    submitting,
    error,
    setError,
    consent,
    setConsent,
    aliasInput,
    setAliasInput,
    resultMessage,
    setResultMessage,
    openError,
    setOpenError,
    isDialogOpen: mode !== null,
  }
}
