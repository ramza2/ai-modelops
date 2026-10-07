import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { ClientApp, Paginated } from '../api/types'
import { ClientsPage } from '../pages/ClientsPage'
import * as clientsApi from '../api/clients'

function makeClient(
  overrides: Partial<ClientApp> & Pick<ClientApp, 'id' | 'client_key'>,
): ClientApp {
  return {
    display_name: overrides.display_name ?? overrides.client_key,
    description: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function pageResult(
  items: ClientApp[],
  opts: { page?: number; total?: number } = {},
): Paginated<ClientApp> {
  return {
    items,
    page: opts.page ?? 1,
    page_size: 20,
    total: opts.total ?? items.length,
  }
}

function LocationProbe() {
  const [params] = useSearchParams()
  return <div data-testid="location-search">{params.toString()}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/clients" element={<ClientsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ClientsPage', () => {
  beforeEach(() => {
    vi.spyOn(clientsApi, 'listClients')
    vi.spyOn(clientsApi, 'getClient')
    vi.spyOn(clientsApi, 'getClientRuntimePolicy')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders list without N+1 and remains GET-only', async () => {
    vi.mocked(clientsApi.listClients).mockResolvedValue(
      pageResult([
        makeClient({ id: 'c1', client_key: 'alzi', display_name: 'ALZI' }),
      ]),
    )
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderAt('/clients')
    expect(await screen.findByRole('link', { name: 'ALZI' })).toHaveAttribute(
      'href',
      '/clients/c1',
    )
    expect(clientsApi.listClients).toHaveBeenCalledTimes(1)
    expect(clientsApi.getClient).not.toHaveBeenCalled()
    expect(clientsApi.getClientRuntimePolicy).not.toHaveBeenCalled()
    const methods = fetchSpy.mock.calls.map(
      (c) => (c[1] as RequestInit | undefined)?.method || 'GET',
    )
    expect(methods.every((m) => m === 'GET')).toBe(true)
  })

  it.each([
    ['/clients?is_active=TRUE', 'is_active=true'],
    ['/clients?is_active=yes', ''],
    ['/clients?q=alzi&page=002', 'q=alzi&page=2'],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('searches without keystroke requests and preserves rows on refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients)
      .mockResolvedValueOnce(
        pageResult([
          makeClient({ id: 'c1', client_key: 'keep', display_name: 'Keep' }),
        ]),
      )
      .mockRejectedValueOnce(new ApiError('refresh fail', { status: 503 }))
    renderAt('/clients')
    expect(await screen.findByRole('link', { name: 'Keep' })).toBeInTheDocument()
    const before = vi.mocked(clientsApi.listClients).mock.calls.length
    await user.type(screen.getByLabelText('검색'), 'demo')
    expect(vi.mocked(clientsApi.listClients).mock.calls.length).toBe(before)
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh fail')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Keep' })).toBeInTheDocument()
  })

  it('uses scope=col', async () => {
    vi.mocked(clientsApi.listClients).mockResolvedValue(
      pageResult([makeClient({ id: 'c1', client_key: 'x', display_name: 'X' })]),
    )
    renderAt('/clients')
    await screen.findByRole('link', { name: 'X' })
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
  })
})
