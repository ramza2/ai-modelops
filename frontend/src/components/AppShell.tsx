import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { Link, useLocation } from 'react-router-dom'
import { formatClock } from '../utils/date'
import { NAV_ITEMS, type NavItem } from '../utils/uiLabels'

type AppShellProps = {
  title: string
  /** Short page purpose (1–2 sentences). Prefer Korean; fold details in page body. */
  description: ReactNode
  children: ReactNode
  onRefresh: () => void
  refreshing: boolean
  lastUpdated: Date | null
  refreshDisabled?: boolean
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function routeActiveFlags(pathname: string): Record<string, boolean> {
  const modelsActive =
    pathname === '/models' ||
    pathname.startsWith('/models/') ||
    pathname.startsWith('/model-versions/')
  const deploymentsActive =
    pathname === '/deployments' || pathname.startsWith('/deployments/')
  const endpointsActive =
    pathname === '/endpoints' || pathname.startsWith('/endpoints/')
  const operationsActive =
    pathname === '/operations' || pathname.startsWith('/operations/')
  const observabilityActive =
    pathname === '/observability' || pathname.startsWith('/observability/')
  const clientsActive =
    pathname === '/clients' || pathname.startsWith('/clients/')

  return {
    '/models': modelsActive,
    '/deployments': deploymentsActive,
    '/endpoints': endpointsActive,
    '/operations': operationsActive,
    '/observability': observabilityActive,
    '/clients': clientsActive,
  }
}

function isNavItemActive(
  item: NavItem,
  pathname: string,
  activeByPath: Record<string, boolean>,
): boolean {
  const forced = activeByPath[item.to]
  if (forced !== undefined) return forced
  if (item.end) return pathname === item.to
  return pathname === item.to || pathname.startsWith(`${item.to}/`)
}

export function AppShell({
  title,
  description,
  children,
  onRefresh,
  refreshing,
  lastUpdated,
  refreshDisabled = false,
}: AppShellProps) {
  const busy = refreshing || refreshDisabled
  const location = useLocation()
  const activeByPath = routeActiveFlags(location.pathname)
  const navId = useId()
  const [navOpen, setNavOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const navPanelRef = useRef<HTMLElement>(null)
  const previouslyFocused = useRef<HTMLElement | null>(null)
  const hadNavOpen = useRef(false)

  const closeNav = useCallback(() => {
    setNavOpen(false)
  }, [])

  const openNav = useCallback(() => {
    previouslyFocused.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    setNavOpen(true)
  }, [])

  const toggleNav = useCallback(() => {
    if (navOpen) {
      closeNav()
    } else {
      openNav()
    }
  }, [navOpen, closeNav, openNav])

  // Close drawer on route change (menu item navigation).
  useEffect(() => {
    setNavOpen(false)
  }, [location.pathname])

  // Body scroll lock while the mobile drawer is open.
  useEffect(() => {
    if (!navOpen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [navOpen])

  // Focus first control when opening; restore focus only after a real close.
  useEffect(() => {
    if (navOpen) {
      hadNavOpen.current = true
      const panel = navPanelRef.current
      const focusables = panel?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
      const target = focusables?.[0]
      window.requestAnimationFrame(() => {
        target?.focus()
      })
      return
    }
    if (!hadNavOpen.current) return
    hadNavOpen.current = false
    const restore = previouslyFocused.current ?? menuButtonRef.current
    if (restore && document.contains(restore)) {
      restore.focus()
    }
    previouslyFocused.current = null
  }, [navOpen])

  useEffect(() => {
    if (!navOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeNav()
        return
      }
      if (event.key !== 'Tab') return
      const panel = navPanelRef.current
      if (!panel) return
      const focusables = [
        ...panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ].filter((el) => !el.hasAttribute('disabled') && el.tabIndex !== -1)
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement
      if (event.shiftKey && active === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [navOpen, closeNav])

  const onNavKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeNav()
    }
  }

  return (
    <div className="shell">
      <header className="shell__top">
        <div className="shell__top-start">
          <button
            ref={menuButtonRef}
            type="button"
            className="shell__menu-toggle"
            aria-expanded={navOpen}
            aria-controls={navId}
            aria-label="주 메뉴"
            onClick={toggleNav}
          >
            <span className="shell__menu-toggle-bars" aria-hidden="true" />
            <span className="shell__menu-toggle-text">메뉴</span>
          </button>
          <div className="shell__brand">
            <span className="shell__product">ModelOps</span>
            <span className="shell__tag">관리자</span>
          </div>
        </div>
        <div className="shell__actions">
          <span className="shell__updated" aria-live="polite">
            마지막 갱신 {formatClock(lastUpdated)}
          </span>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onRefresh}
            disabled={busy}
            aria-busy={refreshing}
          >
            {refreshing ? '갱신 중…' : '새로고침'}
          </button>
        </div>
      </header>

      {navOpen ? (
        <button
          type="button"
          className="shell__backdrop"
          aria-label="메뉴 배경 닫기"
          onClick={closeNav}
        />
      ) : null}

      <div className="shell__body">
        <aside
          ref={navPanelRef}
          id={navId}
          className={
            navOpen ? 'shell__nav shell__nav--open' : 'shell__nav'
          }
          aria-label="주 메뉴"
          onKeyDown={onNavKeyDown}
        >
          <div className="shell__nav-toolbar">
            <p className="shell__nav-title">탐색</p>
            <button
              type="button"
              className="shell__nav-close"
              aria-label="탐색 닫기"
              onClick={closeNav}
            >
              닫기
            </button>
          </div>
          <nav>
            {NAV_ITEMS.map((item) => {
              const active = isNavItemActive(
                item,
                location.pathname,
                activeByPath,
              )
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  title={item.description}
                  className={active ? 'nav-link nav-link--active' : 'nav-link'}
                  aria-current={active ? 'page' : undefined}
                  onClick={closeNav}
                >
                  <span className="nav-link__label">{item.label}</span>
                  <span className="nav-link__desc">{item.description}</span>
                </Link>
              )
            })}
          </nav>
        </aside>
        <main className="shell__main">
          <header className="page-header">
            <h1>{title}</h1>
            <p>{description}</p>
          </header>
          {children}
        </main>
      </div>
    </div>
  )
}
