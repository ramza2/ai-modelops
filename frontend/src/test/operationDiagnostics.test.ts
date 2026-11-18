import { describe, expect, it } from 'vitest'
import type { OperationStep } from '../api/types'
import {
  filterDiagnosticSteps,
  operationDiagnosticGuidance,
  stepHasDiagnosticError,
} from '../utils/operationDiagnostics'

const step = (
  sequence_no: number,
  status: string,
  code: string | null = null,
  message: string | null = null,
): OperationStep => ({
  id: 's-' + sequence_no,
  sequence_no,
  step_code: sequence_no === 1 ? 'VALIDATE' : 'START_TARGET',
  status,
  attempt_no: 1,
  started_at: null,
  finished_at: null,
  created_at: '2026-11-18T00:00:00Z',
  error: code || message ? { code, message } : null,
})

describe('Operation persisted diagnostic filters', () => {
  const records = [
    step(3, 'SUCCEEDED'),
    step(2, 'FAILED', 'TARGET_START_FAILED', 'GPU out of memory'),
    step(1, 'SUCCEEDED', 'WARNING_DETAIL', 'Check again'),
  ]

  it('sorts by step sequence without mutating original API array', () => {
    expect(filterDiagnosticSteps(records, '', false).map((item) => item.sequence_no)).toEqual([1, 2, 3])
    expect(records.map((item) => item.sequence_no)).toEqual([3, 2, 1])
  })

  it('finds error step from failure state OR persisted error fields', () => {
    expect(stepHasDiagnosticError(records[0])).toBe(false)
    expect(stepHasDiagnosticError(records[1])).toBe(true)
    expect(stepHasDiagnosticError(records[2])).toBe(true)
    expect(stepHasDiagnosticError(step(4, 'SUCCEEDED'))).toBe(false)
    expect(filterDiagnosticSteps(records, '', true).map((item) => item.sequence_no)).toEqual([1, 2])
  })

  it('matches local code, status, error code and message without server calls', () => {
    expect(filterDiagnosticSteps(records, 'memory', false).map((item) => item.sequence_no)).toEqual([2])
    expect(filterDiagnosticSteps(records, 'target_start_failed', true).map((item) => item.sequence_no)).toEqual([2])
    expect(filterDiagnosticSteps(records, 'validate', false).map((item) => item.sequence_no)).toEqual([1])
    expect(filterDiagnosticSteps(records, 'suCCEEDED', true).map((item) => item.sequence_no)).toEqual([1])
    expect(filterDiagnosticSteps(records, 'not found', false)).toEqual([])
  })

  it('does not imply success from absence of step error', () => {
    expect(filterDiagnosticSteps([step(1, 'RUNNING')], '', true)).toEqual([])
    expect(operationDiagnosticGuidance('MANUAL_INTERVENTION_REQUIRED')).toContain('수동 확인')
    expect(operationDiagnosticGuidance('FAILED')).toContain('오류')
    expect(operationDiagnosticGuidance('RUNNING')).toContain('진행 중')
  })
})
