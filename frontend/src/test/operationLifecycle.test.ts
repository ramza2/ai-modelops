import { beforeEach, describe, expect, it } from 'vitest'
import {
  formatOperationError,
  isActiveOperationStatus,
  OPERATION_POLL_MS,
  operationPollConfig,
} from '../utils/operationLifecycle'

describe('operationLifecycle', () => {
  beforeEach(() => {
    operationPollConfig.ms = 4000
  })

  it('defaults to a 4s poll interval', () => {
    expect(OPERATION_POLL_MS).toBe(4000)
    expect(operationPollConfig.ms).toBe(4000)
  })

  it('classifies in-flight Operation statuses', () => {
    expect(isActiveOperationStatus('QUEUED')).toBe(true)
    expect(isActiveOperationStatus('RUNNING')).toBe(true)
    expect(isActiveOperationStatus('ROLLING_BACK')).toBe(true)
    expect(isActiveOperationStatus('queued')).toBe(true)
    expect(isActiveOperationStatus('SUCCEEDED')).toBe(false)
    expect(isActiveOperationStatus('FAILED')).toBe(false)
    expect(isActiveOperationStatus('CANCELLED')).toBe(false)
    expect(isActiveOperationStatus('ROLLED_BACK')).toBe(false)
    expect(isActiveOperationStatus('MANUAL_INTERVENTION_REQUIRED')).toBe(false)
    expect(isActiveOperationStatus(null)).toBe(false)
  })

  it('formats Operation errors without inventing text', () => {
    expect(formatOperationError(null)).toBe('—')
    expect(formatOperationError({ code: 'X', message: 'boom' })).toBe('X: boom')
    expect(formatOperationError({ code: 'X', message: null })).toBe('X')
    expect(formatOperationError({ code: null, message: 'boom' })).toBe('boom')
    expect(formatOperationError({ code: null, message: null })).toBe('—')
  })
})
