import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { HelpHint, TermLabel } from '../components/HelpHint'

afterEach(() => {
  cleanup()
})

describe('HelpHint / TermLabel markup', () => {
  it('uses details/summary and expands for keyboard users', async () => {
    const user = userEvent.setup()
    render(<HelpHint label="등록 상태">도움말 본문</HelpHint>)
    const summary = screen.getByText('등록 상태 도움말').closest('summary')
    expect(summary).toBeTruthy()
    expect(screen.queryByText('도움말 본문')).not.toBeVisible()
    await user.click(summary!)
    expect(screen.getByText('도움말 본문')).toBeVisible()
  })

  it('renders TermLabel as a div host (not span>details)', () => {
    const { container } = render(
      <table>
        <thead>
          <tr>
            <th scope="col">
              <TermLabel text="서비스 키" apiTerm="client_key" help="키 설명" />
            </th>
          </tr>
        </thead>
      </table>,
    )
    const host = container.querySelector('.term-label')
    expect(host?.tagName).toBe('DIV')
    expect(host?.querySelector('details.help-hint')).toBeTruthy()
    expect(container.querySelector('span.term-label')).toBeNull()
  })

  it('keeps section heading free of nested details', () => {
    const { container } = render(
      <div className="detail-panel__heading">
        <h2>런타임 정책</h2>
        <HelpHint label="런타임 정책">정책 설명</HelpHint>
      </div>,
    )
    expect(container.querySelector('h2 details')).toBeNull()
    expect(container.querySelector('h2')?.textContent).toBe('런타임 정책')
    expect(container.querySelector('.detail-panel__heading details')).toBeTruthy()
  })
})
