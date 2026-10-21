import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Endpoint } from '../api/types'
import { EndpointEnabledConfirmDialog } from '../components/EndpointEnabledConfirmDialog'
import {
  activeRouteSnapshot,
  activeRouteSnapshotsEqual,
  useEndpointEnabledAction,
} from '../hooks/useEndpointEnabledAction'
import * as endpointsApi from '../api/endpoints'

function makeEndpoint(overrides: Partial<Endpoint> = {}): Endpoint {
  return {
    id: 'e1',
    alias: 'chat-main',
    display_name: 'Chat Main',
    api_type: 'CHAT',
    description: null,
    is_enabled: true,
    traffic_state: 'SERVING',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    active_route: {
      route_id: 'route-a',
      deployment_id: 'dep-a',
      rewrite_model_name: null,
      activated_at: '2026-10-01T00:00:00Z',
      deployment: {
        id: 'dep-a',
        name: 'dep-a-name',
        runtime_status: 'RUNNING',
        health_status: 'HEALTHY',
        upstream_base_url: null,
        retired_at: null,
      },
    },
    ...overrides,
  }
}

function Harness({
  onSuccess = async () => {},
}: {
  onSuccess?: (ep: Endpoint) => void | Promise<void>
}) {
  const action = useEndpointEnabledAction({ onSuccess })
  return (
    <div>
      <button
        type="button"
        data-testid="open-disable"
        onClick={() => void action.open('e1', 'disable')}
      >
        open-disable
      </button>
      <button
        type="button"
        data-testid="open-enable"
        onClick={() => void action.open('e1', 'enable')}
      >
        open-enable
      </button>
      {action.resultMessage ? (
        <div data-testid="result">{action.resultMessage}</div>
      ) : null}
      {action.isDialogOpen && action.mode ? (
        <EndpointEnabledConfirmDialog
          mode={action.mode}
          endpoint={action.draft}
          refreshing={action.refreshing}
          submitting={action.submitting}
          error={action.error}
          consent={action.consent}
          onConsentChange={action.setConsent}
          aliasInput={action.aliasInput}
          onAliasInputChange={action.setAliasInput}
          onCancel={action.close}
          onConfirm={() => void action.confirm()}
        />
      ) : null}
    </div>
  )
}

describe('activeRouteSnapshot helpers', () => {
  it('compares route_id and deployment_id, including nulls', () => {
    const withRoute = makeEndpoint()
    const otherDep = makeEndpoint({
      active_route: {
        route_id: 'route-a',
        deployment_id: 'dep-b',
        rewrite_model_name: null,
        activated_at: null,
        deployment: null,
      },
    })
    const otherRoute = makeEndpoint({
      active_route: {
        route_id: 'route-b',
        deployment_id: 'dep-a',
        rewrite_model_name: null,
        activated_at: null,
        deployment: null,
      },
    })
    const none = makeEndpoint({ active_route: null })
    expect(
      activeRouteSnapshotsEqual(
        activeRouteSnapshot(withRoute),
        activeRouteSnapshot(withRoute),
      ),
    ).toBe(true)
    expect(
      activeRouteSnapshotsEqual(
        activeRouteSnapshot(withRoute),
        activeRouteSnapshot(otherDep),
      ),
    ).toBe(false)
    expect(
      activeRouteSnapshotsEqual(
        activeRouteSnapshot(withRoute),
        activeRouteSnapshot(otherRoute),
      ),
    ).toBe(false)
    expect(
      activeRouteSnapshotsEqual(
        activeRouteSnapshot(withRoute),
        activeRouteSnapshot(none),
      ),
    ).toBe(false)
    expect(
      activeRouteSnapshotsEqual(
        activeRouteSnapshot(none),
        activeRouteSnapshot(makeEndpoint({ active_route: null })),
      ),
    ).toBe(true)
  })
})

describe('useEndpointEnabledAction route drift', () => {
  beforeEach(() => {
    vi.spyOn(endpointsApi, 'getEndpoint')
    vi.spyOn(endpointsApi, 'updateEndpointEnabled')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('does not PATCH when ACTIVE Route deployment changes A → B', async () => {
    const user = userEvent.setup()
    const routeA = makeEndpoint()
    const routeB = makeEndpoint({
      active_route: {
        route_id: 'route-b',
        deployment_id: 'dep-b',
        rewrite_model_name: null,
        activated_at: '2026-10-02T00:00:00Z',
        deployment: {
          id: 'dep-b',
          name: 'dep-b-name',
          runtime_status: 'RUNNING',
          health_status: 'HEALTHY',
          upstream_base_url: null,
          retired_at: null,
        },
      },
    })
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(routeA)
      .mockResolvedValueOnce(routeB)
    render(<Harness />)
    await user.click(screen.getByTestId('open-disable'))
    expect(await screen.findByTestId('endpoint-enabled-dialog')).toBeInTheDocument()
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.type(screen.getByTestId('endpoint-enabled-alias-confirm'), 'chat-main')
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(screen.getByText(/확인 내용이 변경되었습니다/)).toBeInTheDocument()
    })
    expect(endpointsApi.updateEndpointEnabled).not.toHaveBeenCalled()
    expect(screen.getByTestId('endpoint-enabled-consent')).not.toBeChecked()
    expect(screen.getByTestId('endpoint-enabled-alias-confirm')).toHaveValue('')
  })

  it('does not PATCH when ACTIVE Route appears or disappears', async () => {
    const user = userEvent.setup()
    const none = makeEndpoint({ active_route: null })
    const withRoute = makeEndpoint()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(none)
      .mockResolvedValueOnce(withRoute)
    render(<Harness />)
    await user.click(screen.getByTestId('open-disable'))
    await screen.findByTestId('endpoint-enabled-dialog')
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(screen.getByText(/확인 내용이 변경되었습니다/)).toBeInTheDocument()
    })
    expect(endpointsApi.updateEndpointEnabled).not.toHaveBeenCalled()

    cleanup()
    vi.mocked(endpointsApi.getEndpoint).mockReset()
    vi.mocked(endpointsApi.updateEndpointEnabled).mockReset()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(withRoute)
      .mockResolvedValueOnce(none)
    render(<Harness />)
    await user.click(screen.getByTestId('open-disable'))
    await screen.findByTestId('endpoint-enabled-dialog')
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.type(screen.getByTestId('endpoint-enabled-alias-confirm'), 'chat-main')
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(screen.getByText(/확인 내용이 변경되었습니다/)).toBeInTheDocument()
    })
    expect(endpointsApi.updateEndpointEnabled).not.toHaveBeenCalled()
  })

  it('PATCHes disable when the same ACTIVE Route is unchanged', async () => {
    const user = userEvent.setup()
    const routeA = makeEndpoint()
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(routeA)
      .mockResolvedValueOnce(routeA)
    vi.mocked(endpointsApi.updateEndpointEnabled).mockResolvedValue(
      makeEndpoint({ is_enabled: false }),
    )
    render(<Harness />)
    await user.click(screen.getByTestId('open-disable'))
    await screen.findByTestId('endpoint-enabled-dialog')
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.type(screen.getByTestId('endpoint-enabled-alias-confirm'), 'chat-main')
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(endpointsApi.updateEndpointEnabled).toHaveBeenCalledWith(
        'e1',
        false,
        expect.anything(),
      )
    })
    expect(await screen.findByTestId('result')).toHaveTextContent(/사용 중지/)
  })

  it('re-checks ACTIVE Route drift on re-enable as well', async () => {
    const user = userEvent.setup()
    const disabledNone = makeEndpoint({
      is_enabled: false,
      active_route: null,
    })
    const disabledWithRoute = makeEndpoint({ is_enabled: false })
    vi.mocked(endpointsApi.getEndpoint)
      .mockResolvedValueOnce(disabledNone)
      .mockResolvedValueOnce(disabledWithRoute)
    render(<Harness />)
    await user.click(screen.getByTestId('open-enable'))
    await screen.findByTestId('endpoint-enabled-dialog')
    await user.click(screen.getByTestId('endpoint-enabled-consent'))
    await user.click(screen.getByTestId('endpoint-enabled-confirm'))
    await waitFor(() => {
      expect(screen.getByText(/확인 내용이 변경되었습니다/)).toBeInTheDocument()
    })
    expect(endpointsApi.updateEndpointEnabled).not.toHaveBeenCalled()
  })
})
