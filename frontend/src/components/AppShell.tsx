import type { ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { formatClock } from '../utils/date'

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
            <NavLink
              to="/dashboard"
              className={({ isActive }) =>
                isActive ? 'nav-link nav-link--active' : 'nav-link'
              }
              end
            >
              Dashboard
            </NavLink>
            <NavLink
              to="/nodes"
              className={({ isActive }) =>
                isActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Nodes / GPUs
            </NavLink>
            <NavLink
              to="/models"
              className={() =>
                modelsActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Models / Versions
            </NavLink>
            <NavLink
              to="/deployments"
              className={() =>
                deploymentsActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Deployments
            </NavLink>
            <NavLink
              to="/endpoints"
              className={() =>
                endpointsActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Endpoints
            </NavLink>
            <NavLink
              to="/operations"
              className={() =>
                operationsActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Operations
            </NavLink>
            <NavLink
              to="/observability"
              className={() =>
                observabilityActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Observability
            </NavLink>
            <NavLink
              to="/clients"
              className={() =>
                clientsActive ? 'nav-link nav-link--active' : 'nav-link'
              }
            >
              Clients
            </NavLink>
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
