import type { ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { formatClock } from '../utils/date'
import { NAV_ITEMS } from '../utils/uiLabels'

type AppShellProps = {
  title: string
  description: string
  children: ReactNode
  onRefresh: () => void
  refreshing: boolean
  lastUpdated: Date | null
  refreshDisabled?: boolean
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
  const modelsActive =
    location.pathname === '/models' ||
    location.pathname.startsWith('/models/') ||
    location.pathname.startsWith('/model-versions/')
  const deploymentsActive =
    location.pathname === '/deployments' ||
    location.pathname.startsWith('/deployments/')
  const endpointsActive =
    location.pathname === '/endpoints' ||
    location.pathname.startsWith('/endpoints/')
  const operationsActive =
    location.pathname === '/operations' ||
    location.pathname.startsWith('/operations/')
  const observabilityActive =
    location.pathname === '/observability' ||
    location.pathname.startsWith('/observability/')
  const clientsActive =
    location.pathname === '/clients' ||
    location.pathname.startsWith('/clients/')

  const activeByPath: Record<string, boolean> = {
    '/models': modelsActive,
    '/deployments': deploymentsActive,
    '/endpoints': endpointsActive,
    '/operations': operationsActive,
    '/observability': observabilityActive,
    '/clients': clientsActive,
  }

  return (
    <div className="shell">
      <header className="shell__top">
        <div className="shell__brand">
          <span className="shell__product">ModelOps</span>
          <span className="shell__tag">Admin</span>
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
      <div className="shell__body">
        <aside className="shell__nav" aria-label="주 메뉴">
          <nav>
            {NAV_ITEMS.map((item) => {
              const forcedActive = activeByPath[item.to]
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => {
                    const active =
                      forcedActive !== undefined ? forcedActive : isActive
                    return active ? 'nav-link nav-link--active' : 'nav-link'
                  }}
                >
                  <span className="nav-link__label">{item.label}</span>
                  <span className="nav-link__desc">{item.description}</span>
                </NavLink>
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
