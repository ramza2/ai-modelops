import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { StatusBadge } from '../components/StatusBadge'

afterEach(() => {
  cleanup()
})

describe('StatusBadge', () => {
  it('defaults title to the API status code', () => {
    render(<StatusBadge status="TIGHT" />)
    const badge = screen.getByTitle('TIGHT')
    expect(badge).toHaveTextContent(/여유 부족 가능/)
    expect(badge).toHaveTextContent(/\(TIGHT\)/)
  })

  it('accepts a beginner-facing title without hiding the status code in text', () => {
    render(
      <StatusBadge
        status="FIT"
        title="자원 적합(FIT): 참고 판정이며 배포 성공을 보장하지 않습니다."
      />,
    )
    const badge = screen.getByTitle(
      '자원 적합(FIT): 참고 판정이며 배포 성공을 보장하지 않습니다.',
    )
    expect(badge).toHaveTextContent(/자원 적합/)
    expect(badge).toHaveTextContent(/\(FIT\)/)
    expect(screen.queryByTitle('FIT')).toBeNull()
  })
})
