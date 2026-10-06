import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { formatClock } from '../utils/date'

type AppShellProps = {
  title: string
  description: string
  children: ReactNode
  onRefresh: () => void
  refreshing: boolean
  lastUpdated: Date | null
}

export function AppShell({
  title,
  description,
  children,
  onRefresh,
  refreshing,
  lastUpdated,
}: AppShellProps) {
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
            disabled={refreshing}
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
          </nav>
          <div className="nav-section" aria-label="향후 메뉴">
            <p className="nav-section__label">Model Ops</p>
            <ul className="nav-future">
              <li>Nodes / GPUs</li>
              <li>Models / Versions</li>
              <li>Deployments</li>
              <li>Endpoints</li>
              <li>Operations</li>
              <li>Observability</li>
            </ul>
          </div>
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
