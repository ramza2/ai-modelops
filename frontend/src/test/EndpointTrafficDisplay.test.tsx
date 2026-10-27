import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { EndpointTrafficDisplay } from '../components/EndpointTrafficDisplay'
import { ENDPOINT_FIELD_HELP } from '../utils/uiLabels'

function liveBadgeTitle(): string | null {
  const root = screen.getByTestId('endpoint-traffic-display')
  const badge = root.querySelector('.status-badge')
  return badge?.getAttribute('title') ?? null
}

describe('EndpointTrafficDisplay', () => {
  afterEach(() => {
    cleanup()
  })

  it('shows neutral 사용 중지 for disabled alias even when Traffic is SERVING', () => {
    render(
      <EndpointTrafficDisplay
        isEnabled={false}
        trafficState="SERVING"
        hasActiveRoute={false}
      />,
    )
    const el = screen.getByTestId('endpoint-traffic-display')
    expect(el).toHaveTextContent('사용 중지')
    expect(el).toHaveTextContent('Traffic: SERVING')
    expect(el.className).toContain('status-badge--muted')
    expect(el).not.toHaveTextContent('서빙')
  })

  it('shows 모델 미연결 when enabled but no ACTIVE Route', () => {
    render(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="SERVING"
        hasActiveRoute={false}
      />,
    )
    const el = screen.getByTestId('endpoint-traffic-display')
    expect(el).toHaveTextContent('모델 미연결')
    expect(el).toHaveTextContent('Traffic: SERVING')
    expect(el.className).toContain('status-badge--muted')
    expect(el).not.toHaveTextContent(/^서빙/)
  })

  it('keeps green SERVING badge when enabled with ACTIVE Route', () => {
    render(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="SERVING"
        hasActiveRoute={true}
      />,
    )
    const el = screen.getByTestId('endpoint-traffic-display')
    expect(el).toHaveTextContent(/요청 처리 중/)
    expect(el).toHaveTextContent('(SERVING)')
    expect(el).not.toHaveTextContent(/서빙/)
    expect(el.querySelector('.status-badge--ok')).toBeTruthy()
  })

  it('labels DRAINING and MAINTENANCE in Korean without changing codes', () => {
    const { rerender } = render(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="DRAINING"
        hasActiveRoute={true}
      />,
    )
    expect(screen.getByTestId('endpoint-traffic-display')).toHaveTextContent(
      /요청 종료 중/,
    )
    expect(screen.getByTestId('endpoint-traffic-display')).toHaveTextContent(
      '(DRAINING)',
    )
    rerender(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="MAINTENANCE"
        hasActiveRoute={true}
      />,
    )
    expect(screen.getByTestId('endpoint-traffic-display')).toHaveTextContent(
      /유지보수/,
    )
    expect(screen.getByTestId('endpoint-traffic-display')).toHaveTextContent(
      '(MAINTENANCE)',
    )
  })

  it('uses state-specific tooltips for live Traffic badges', () => {
    const { rerender } = render(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="SERVING"
        hasActiveRoute={true}
      />,
    )
    expect(liveBadgeTitle()).toBe(ENDPOINT_FIELD_HELP.trafficServingOk)

    rerender(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="DRAINING"
        hasActiveRoute={true}
      />,
    )
    expect(liveBadgeTitle()).toMatch(/DRAINING/)
    expect(liveBadgeTitle()).toMatch(/신규 요청 차단/)
    expect(liveBadgeTitle()).toMatch(/진행 중 요청 대기/)
    expect(liveBadgeTitle()).not.toBe(ENDPOINT_FIELD_HELP.trafficServingOk)

    rerender(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="MAINTENANCE"
        hasActiveRoute={true}
      />,
    )
    expect(liveBadgeTitle()).toMatch(/MAINTENANCE/)
    expect(liveBadgeTitle()).toMatch(/신규 요청 차단/)
    expect(liveBadgeTitle()).toMatch(/upstream 전달 금지/)
    expect(liveBadgeTitle()).not.toBe(ENDPOINT_FIELD_HELP.trafficServingOk)

    rerender(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="UNKNOWN_STATE"
        hasActiveRoute={true}
      />,
    )
    expect(liveBadgeTitle()).toBe(ENDPOINT_FIELD_HELP.traffic)
  })

  it('keeps disabled/unrouted tooltip precedence over Traffic state titles', () => {
    const { rerender } = render(
      <EndpointTrafficDisplay
        isEnabled={false}
        trafficState="DRAINING"
        hasActiveRoute={true}
      />,
    )
    const disabledTitle =
      screen.getByTestId('endpoint-traffic-display').getAttribute('title') || ''
    expect(disabledTitle).toContain(ENDPOINT_FIELD_HELP.trafficDisplay)
    expect(disabledTitle).toContain('DRAINING')
    expect(disabledTitle).not.toMatch(/진행 중 요청 대기/)

    rerender(
      <EndpointTrafficDisplay
        isEnabled={true}
        trafficState="MAINTENANCE"
        hasActiveRoute={false}
      />,
    )
    const unroutedTitle =
      screen.getByTestId('endpoint-traffic-display').getAttribute('title') || ''
    expect(unroutedTitle).toContain(ENDPOINT_FIELD_HELP.unusedAlias)
    expect(unroutedTitle).toContain('MAINTENANCE')
    expect(unroutedTitle).not.toMatch(/upstream 전달 금지/)
  })
})
