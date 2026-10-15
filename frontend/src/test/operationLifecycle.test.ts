import { beforeEach, describe, expect, it } from 'vitest'
import {
  createDetailFetchGate,
  formatOperationError,
  healthWaitElapsedMsFromFinishedAt,
  isActiveOperationStatus,
  isDeploymentReadyForPublish,
  isDeploymentStoppedForSync,
  isFailedOperationStatus,
  OPERATION_POLL_MS,
  operationPollConfig,
  resolveDetailLifecyclePhase,
  resolveWizardLifecyclePhase,
  shouldPollDetailLifecycle,
  shouldPollWizardLifecycle,
} from '../utils/operationLifecycle'

describe('operationLifecycle', () => {
  beforeEach(() => {
    operationPollConfig.ms = 4000
    operationPollConfig.healthWaitMaxMs = 5 * 60 * 1000
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

  it('classifies failed Operation statuses', () => {
    expect(isFailedOperationStatus('FAILED')).toBe(true)
    expect(isFailedOperationStatus('CANCELLED')).toBe(true)
    expect(isFailedOperationStatus('ROLLED_BACK')).toBe(true)
    expect(isFailedOperationStatus('MANUAL_INTERVENTION_REQUIRED')).toBe(true)
    expect(isFailedOperationStatus('SUCCEEDED')).toBe(false)
    expect(isFailedOperationStatus('RUNNING')).toBe(false)
    expect(isFailedOperationStatus(null)).toBe(false)
  })

  it('requires RUNNING desired/runtime and HEALTHY for Publish readiness', () => {
    expect(
      isDeploymentReadyForPublish({
        desired_state: 'RUNNING',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
      }),
    ).toBe(true)
    expect(
      isDeploymentReadyForPublish({
        desired_state: 'RUNNING',
        runtime_status: 'RUNNING',
        health_status: 'UNKNOWN',
      }),
    ).toBe(false)
    expect(
      isDeploymentReadyForPublish({
        desired_state: 'RUNNING',
        runtime_status: 'STARTING',
        health_status: 'HEALTHY',
      }),
    ).toBe(false)
    expect(isDeploymentReadyForPublish(null)).toBe(false)
  })

  it('computes health-wait elapsed from finished_at without resetting on bad timestamps', () => {
    const now = Date.parse('2026-10-13T06:00:00.000Z')
    expect(
      healthWaitElapsedMsFromFinishedAt('2026-10-13T05:59:00.000Z', now),
    ).toBe(60_000)
    expect(healthWaitElapsedMsFromFinishedAt(null, now)).toBeNull()
    expect(healthWaitElapsedMsFromFinishedAt(undefined, now)).toBeNull()
    expect(healthWaitElapsedMsFromFinishedAt('', now)).toBeNull()
    expect(healthWaitElapsedMsFromFinishedAt('   ', now)).toBeNull()
    expect(healthWaitElapsedMsFromFinishedAt('not-a-date', now)).toBeNull()
    expect(
      healthWaitElapsedMsFromFinishedAt('2026-10-13T06:01:00.000Z', now),
    ).toBe(0)
    // F5-safe: same finished_at yields the same elapsed for a given now.
    const finished = '2026-10-13T05:55:00.000Z'
    expect(healthWaitElapsedMsFromFinishedAt(finished, now)).toBe(5 * 60_000)
    expect(healthWaitElapsedMsFromFinishedAt(finished, now)).toBe(
      operationPollConfig.healthWaitMaxMs,
    )
  })

  it('resolves Wizard Start lifecycle phases and poll eligibility', () => {
    expect(
      resolveWizardLifecyclePhase({
        step: 'review',
        hasOperationId: true,
        operationStatus: 'RUNNING',
        deploymentReady: false,
        healthWaitElapsedMs: null,
      }),
    ).toBe('idle')
    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: false,
        operationStatus: null,
        deploymentReady: false,
        healthWaitElapsedMs: null,
      }),
    ).toBe('idle')

    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: true,
        operationStatus: 'RUNNING',
        deploymentReady: false,
        healthWaitElapsedMs: null,
      }),
    ).toBe('operation_active')
    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: true,
        operationStatus: 'FAILED',
        deploymentReady: false,
        healthWaitElapsedMs: null,
      }),
    ).toBe('failed')
    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: true,
        operationStatus: 'SUCCEEDED',
        deploymentReady: true,
        healthWaitElapsedMs: null,
      }),
    ).toBe('ready')
    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: true,
        operationStatus: 'SUCCEEDED',
        deploymentReady: false,
        healthWaitElapsedMs: 1000,
      }),
    ).toBe('health_wait')
    expect(
      resolveWizardLifecyclePhase({
        step: 'start',
        hasOperationId: true,
        operationStatus: 'SUCCEEDED',
        deploymentReady: false,
        healthWaitElapsedMs: operationPollConfig.healthWaitMaxMs,
      }),
    ).toBe('health_timeout')

    expect(shouldPollWizardLifecycle('operation_active')).toBe(true)
    expect(shouldPollWizardLifecycle('health_wait')).toBe(true)
    expect(shouldPollWizardLifecycle('ready')).toBe(false)
    expect(shouldPollWizardLifecycle('failed')).toBe(false)
    expect(shouldPollWizardLifecycle('health_timeout')).toBe(false)
    expect(shouldPollWizardLifecycle('idle')).toBe(false)
  })

  it('resolves Detail STOP lifecycle phases without inventing health', () => {
    expect(isDeploymentStoppedForSync({ runtime_status: 'STOPPED' })).toBe(true)
    expect(isDeploymentStoppedForSync({ runtime_status: 'RUNNING' })).toBe(false)
    expect(isDeploymentStoppedForSync({ runtime_status: 'HEALTHY' })).toBe(false)
    expect(isDeploymentStoppedForSync(null)).toBe(false)

    expect(
      resolveDetailLifecyclePhase({
        operationType: 'START',
        operationStatus: 'RUNNING',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: null,
      }),
    ).toBe('operation_active')
    expect(
      resolveDetailLifecyclePhase({
        operationType: 'START',
        operationStatus: 'SUCCEEDED',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: null,
      }),
    ).toBe('idle')

    expect(
      resolveDetailLifecyclePhase({
        operationType: 'STOP',
        operationStatus: 'FAILED',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: null,
      }),
    ).toBe('failed')
    expect(
      resolveDetailLifecyclePhase({
        operationType: 'STOP',
        operationStatus: 'CANCELLED',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: null,
      }),
    ).toBe('failed')
    expect(
      resolveDetailLifecyclePhase({
        operationType: 'STOP',
        operationStatus: 'SUCCEEDED',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: 1000,
      }),
    ).toBe('stop_wait')
    expect(
      resolveDetailLifecyclePhase({
        operationType: 'STOP',
        operationStatus: 'SUCCEEDED',
        hasOperationId: true,
        deploymentStopped: true,
        stopWaitElapsedMs: 1000,
      }),
    ).toBe('stopped')
    expect(
      resolveDetailLifecyclePhase({
        operationType: 'STOP',
        operationStatus: 'SUCCEEDED',
        hasOperationId: true,
        deploymentStopped: false,
        stopWaitElapsedMs: operationPollConfig.healthWaitMaxMs,
      }),
    ).toBe('stop_timeout')

    expect(shouldPollDetailLifecycle('operation_active')).toBe(true)
    expect(shouldPollDetailLifecycle('stop_wait')).toBe(true)
    expect(shouldPollDetailLifecycle('stopped')).toBe(false)
    expect(shouldPollDetailLifecycle('failed')).toBe(false)
    expect(shouldPollDetailLifecycle('stop_timeout')).toBe(false)
    expect(shouldPollDetailLifecycle('idle')).toBe(false)
  })

  it('formats Operation errors without inventing text', () => {
    expect(formatOperationError(null)).toBe('—')
    expect(formatOperationError({ code: 'X', message: 'boom' })).toBe('X: boom')
    expect(formatOperationError({ code: 'X', message: null })).toBe('X')
    expect(formatOperationError({ code: null, message: 'boom' })).toBe('boom')
    expect(formatOperationError({ code: null, message: null })).toBe('—')
  })

  it('invalidates the in-flight epoch on abort/unmount, even for late responses', () => {
    const gate = createDetailFetchGate()
    const pending = gate.begin('load')
    expect(gate.isCurrent(pending.gen)).toBe(true)
    gate.abort()
    expect(pending.signal.aborted).toBe(true)
    expect(gate.isCurrent(pending.gen)).toBe(false)
    expect(gate.currentKind()).toBeNull()

    const next = gate.begin('poll')
    expect(gate.isCurrent(next.gen)).toBe(true)
    expect(next.gen).toBeGreaterThan(pending.gen)
  })

  it('invalidates older fetch epochs when a newer load or poll begins', () => {
    const gate = createDetailFetchGate()
    const first = gate.begin('poll')
    expect(gate.isCurrent(first.gen)).toBe(true)
    expect(first.signal.aborted).toBe(false)

    const second = gate.begin('load')
    expect(gate.isCurrent(first.gen)).toBe(false)
    expect(first.signal.aborted).toBe(true)
    expect(gate.isCurrent(second.gen)).toBe(true)
    expect(gate.currentKind()).toBe('load')

    const third = gate.begin('poll')
    expect(gate.isCurrent(second.gen)).toBe(false)
    expect(second.signal.aborted).toBe(true)
    expect(gate.isCurrent(third.gen)).toBe(true)
    expect(gate.currentKind()).toBe('poll')
  })
})
