import { afterEach, describe, expect, it, vi } from 'vitest'
import * as client from '../api/client'
import {
  archiveModel,
  archiveModelVersion,
  getDecommissionStatus,
  removeDeployment,
  retireDeployment,
  unpublishEndpoint,
} from '../api/decommission'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('decommission API', () => {
  it('gets decommission-status', async () => {
    const spy = vi.spyOn(client, 'apiGet').mockResolvedValue({
      deployment_id: 'd1',
      can_unpublish: true,
      blockers: [],
    })
    await getDecommissionStatus('d1')
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/deployments/d1/decommission-status',
      expect.objectContaining({ signal: undefined }),
    )
  })

  it('posts unpublish with expected deployment and verify flag', async () => {
    const spy = vi.spyOn(client, 'apiPost').mockResolvedValue({
      changed: true,
      routing_version: 3,
    })
    await unpublishEndpoint('ep-1', {
      expectedDeploymentId: 'dep-1',
      reason: 'teardown',
      verifyGateway: true,
    })
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/endpoints/ep-1/unpublish',
      expect.objectContaining({
        body: {
          expected_deployment_id: 'dep-1',
          reason: 'teardown',
          verify_gateway: true,
        },
      }),
    )
  })

  it('posts remove / retire / archive', async () => {
    const spy = vi.spyOn(client, 'apiPost').mockResolvedValue({ id: 'x' })
    await removeDeployment('d1')
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/deployments/d1/remove',
      expect.objectContaining({
        headers: expect.objectContaining({
          'Idempotency-Key': expect.any(String),
        }),
      }),
    )
    await retireDeployment('d1')
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/deployments/d1/retire',
      expect.any(Object),
    )
    await archiveModelVersion('v1')
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/model-versions/v1/archive',
      expect.any(Object),
    )
    await archiveModel('m1')
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/models/m1/archive',
      expect.any(Object),
    )
  })
})
