import { afterEach, describe, expect, it, vi } from 'vitest'
import * as client from '../api/client'
import {
  createDeploymentFromCache,
  previewCacheDeployFit,
  publishCacheDeployment,
} from '../api/cacheDeploy'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('cacheDeploy API', () => {
  it('posts create deployment from cache with snake_case body', async () => {
    const spy = vi.spyOn(client, 'apiPost').mockResolvedValue({
      id: 'dep-1',
      reused: false,
    })
    await createDeploymentFromCache('cache-1', {
      name: 'demo',
      containerName: 'ctr-demo',
      gpuDeviceIds: ['gpu-1'],
      servedModelName: 'org/demo',
      acknowledgeUnknownFit: true,
      runtimeConfig: { runner: 'pooling', max_num_seqs: 4 },
    })
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/model-cache/cache-1/deployment',
      expect.objectContaining({
        body: expect.objectContaining({
          container_name: 'ctr-demo',
          gpu_device_ids: ['gpu-1'],
          served_model_name: 'org/demo',
          acknowledge_unknown_fit: true,
          runtime_config: expect.objectContaining({
            runner: 'pooling',
            max_num_seqs: 4,
          }),
        }),
      }),
    )
  })

  it('posts fit preview and publish', async () => {
    const spy = vi.spyOn(client, 'apiPost').mockResolvedValue({ result: 'FIT' })
    await previewCacheDeployFit('cache-1', {
      gpuDeviceIds: ['g1', 'g2'],
      tensorParallel: 2,
    })
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/model-cache/cache-1/fit-preview',
      expect.objectContaining({
        body: expect.objectContaining({
          gpu_device_ids: ['g1', 'g2'],
          tensor_parallel: 2,
        }),
      }),
    )
    await publishCacheDeployment('dep-1', {
      alias: 'demo',
      rewriteModelName: 'org/demo',
    })
    expect(spy).toHaveBeenCalledWith(
      '/api/v1/model-cache/deployments/dep-1/publish',
      expect.objectContaining({
        body: expect.objectContaining({
          alias: 'demo',
          rewrite_model_name: 'org/demo',
        }),
      }),
    )
  })
})
