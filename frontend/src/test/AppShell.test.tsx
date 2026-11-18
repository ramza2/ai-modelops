import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from '../components/AppShell'
import { NAV_ITEMS } from '../utils/uiLabels'

function renderShell(
  initialPath: string,
  props: Partial<{
    refreshing: boolean
    refreshDisabled: boolean
    lastUpdated: Date | null
    onRefresh: () => void
  }> = {},
) {
  const onRefresh = props.onRefresh ?? vi.fn()
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route
          path="*"
          element={
            <AppShell
              title="테스트 페이지"
              description="셸 회귀용 설명입니다."
              onRefresh={onRefresh}
              refreshing={props.refreshing ?? false}
              refreshDisabled={props.refreshDisabled}
              lastUpdated={
                props.lastUpdated === undefined
                  ? new Date('2026-11-18T04:00:00Z')
                  : props.lastUpdated
              }
            >
              <p>본문</p>
            </AppShell>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
  return { onRefresh }
}

function menuToggle() {
  return screen.getByRole('button', { name: '주 메뉴' })
}

afterEach(() => {
  cleanup()
  document.body.style.overflow = ''
})

describe('AppShell (UX20-B)', () => {
  it('renders all NAV_ITEMS with stable destinations and compact labels', () => {
    renderShell('/dashboard')
    const nav = screen.getByRole('navigation')
    for (const item of NAV_ITEMS) {
      const link = within(nav).getByRole('link', { name: new RegExp(item.label) })
      expect(link).toHaveAttribute('href', item.to)
      expect(link).toHaveAttribute('title', item.description)
      expect(link.textContent).toContain(item.label)
      expect(link.textContent).toContain(item.description)
    }
  })

  it('marks nested model routes under 모델 관리', () => {
    renderShell('/model-versions/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
    const models = screen.getByRole('link', { name: /모델 관리/ })
    expect(models).toHaveAttribute('aria-current', 'page')
    expect(models.className).toMatch(/nav-link--active/)
  })

  it('marks nested observability and client detail routes', () => {
    renderShell('/observability/deployments/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
    expect(
      screen.getByRole('link', { name: /운영 모니터링/ }),
    ).toHaveAttribute('aria-current', 'page')

    cleanup()
    renderShell('/clients/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
    expect(
      screen.getByRole('link', { name: /AI 사용 시스템/ }),
    ).toHaveAttribute('aria-current', 'page')
  })

  it('preserves refresh busy and disabled contracts', async () => {
    const user = userEvent.setup()
    const onRefresh = vi.fn()
    renderShell('/dashboard', {
      onRefresh,
      refreshing: true,
      refreshDisabled: false,
    })
    const busy = screen.getByRole('button', { name: '갱신 중…' })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('aria-busy', 'true')
    await user.click(busy)
    expect(onRefresh).not.toHaveBeenCalled()

    cleanup()
    const onRefresh2 = vi.fn()
    renderShell('/dashboard', {
      onRefresh: onRefresh2,
      refreshing: false,
      refreshDisabled: true,
    })
    const disabled = screen.getByRole('button', { name: '새로고침' })
    expect(disabled).toBeDisabled()
    await user.click(disabled)
    expect(onRefresh2).not.toHaveBeenCalled()

    cleanup()
    const onRefresh3 = vi.fn()
    renderShell('/dashboard', { onRefresh: onRefresh3 })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(onRefresh3).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/마지막 갱신/)).toBeInTheDocument()
  })

  it('opens mobile menu with aria wiring and closes via Escape', async () => {
    const user = userEvent.setup()
    renderShell('/dashboard')
    const toggle = menuToggle()
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const controlsId = toggle.getAttribute('aria-controls')
    expect(controlsId).toBeTruthy()
    expect(document.getElementById(controlsId!)).toBeTruthy()

    await user.click(toggle)
    expect(menuToggle()).toHaveAttribute('aria-expanded', 'true')
    expect(document.body.style.overflow).toBe('hidden')
    expect(screen.getByRole('button', { name: '메뉴 배경 닫기' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(menuToggle()).toHaveAttribute('aria-expanded', 'false')
    })
    expect(screen.queryByRole('button', { name: '메뉴 배경 닫기' })).not.toBeInTheDocument()
    expect(document.body.style.overflow).toBe('')
  })

  it('closes via toolbar button and backdrop', async () => {
    const user = userEvent.setup()
    renderShell('/nodes')
    const toggle = menuToggle()
    await user.click(toggle)
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!)
    expect(panel).toBeTruthy()
    await user.click(within(panel!).getByRole('button', { name: '탐색 닫기' }))
    await waitFor(() => {
      expect(menuToggle()).toHaveAttribute('aria-expanded', 'false')
    })

    await user.click(menuToggle())
    await user.click(screen.getByRole('button', { name: '메뉴 배경 닫기' }))
    await waitFor(() => {
      expect(menuToggle()).toHaveAttribute('aria-expanded', 'false')
    })
  })

  it('closes the drawer when a nav destination is chosen', async () => {
    const user = userEvent.setup()
    renderShell('/dashboard')
    await user.click(menuToggle())
    expect(menuToggle()).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByRole('link', { name: /서버 및 GPU/ }))
    await waitFor(() => {
      expect(menuToggle()).toHaveAttribute('aria-expanded', 'false')
    })
  })

  it('moves focus into the drawer on open and restores on close', async () => {
    const user = userEvent.setup()
    renderShell('/dashboard')
    const toggle = menuToggle()
    toggle.focus()
    await user.click(toggle)
    await waitFor(() => {
      const panel = document.getElementById(toggle.getAttribute('aria-controls')!)
      expect(panel?.contains(document.activeElement)).toBe(true)
    })
    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(document.activeElement).toBe(menuToggle())
    })
  })
})
