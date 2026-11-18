import type { OperationStep } from '../api/types'

/** The operation API returns Step errors directly. These are NOT container logs. */
export function stepHasDiagnosticError(step: OperationStep): boolean {
  return (
    step.status.toUpperCase() === 'FAILED' ||
    Boolean(step.error?.code?.trim() || step.error?.message?.trim())
  )
}

export function filterDiagnosticSteps(
  steps: OperationStep[],
  query: string,
  errorsOnly: boolean,
): OperationStep[] {
  const term = query.trim().toLocaleLowerCase()
  return steps
    .filter((step) => !errorsOnly || stepHasDiagnosticError(step))
    .filter((step) =>
      !term ||
      [
        step.step_code,
        step.status,
        step.error?.code ?? '',
        step.error?.message ?? '',
      ].some((value) => value.toLocaleLowerCase().includes(term)),
    )
    .sort((a, b) => a.sequence_no - b.sequence_no)
}

/** Guidance stays observational: never promises that an action is safe or available. */
export function operationDiagnosticGuidance(status: string): string {
  switch (status) {
    case 'MANUAL_INTERVENTION_REQUIRED':
      return '수동 확인이 필요한 상태입니다. 작업 오류와 단계 기록을 확인한 후 담당자가 조치를 판단하세요.'
    case 'FAILED':
    case 'ROLLED_BACK':
      return '실패 또는 롤백 상태입니다. 오류가 기록된 단계와 관련 배포의 측정 정보를 확인하세요.'
    case 'QUEUED':
    case 'RUNNING':
    case 'ROLLING_BACK':
      return '진행 중인 작업입니다. 단계 상태와 최신 조회 시각을 확인하세요. 주기적인 상태 조회로 단계 기록이 달라질 수 있습니다.'
    case 'SUCCEEDED':
      return '작업이 완료된 상태입니다. 실제 서비스 상태는 관련 배포·API 화면에서 별도로 확인하세요.'
    default:
      return '서버가 반환한 상태입니다. 단계 기록과 관련 대상 정보를 확인하세요.'
  }
}
