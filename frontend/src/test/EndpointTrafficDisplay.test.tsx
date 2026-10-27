import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { EndpointTrafficDisplay } from '../components/EndpointTrafficDisplay'

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
})
