import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { ClientApp, ClientRuntimePolicyResponse } from '../api/types'
import { ClientDetailPage } from '../pages/ClientDetailPage'
import * as clientsApi from '../api/clients'

function makeClient(overrides: Partial<ClientApp> = {}): ClientApp {
  return {
    id: 'c-a',
    client_key: 'alzi',
    display_name: 'ALZI',
    description: 'demo',
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function makePolicy(
  overrides: Partial<ClientRuntimePolicyResponse> = {},
): ClientRuntimePolicyResponse {
  return {
    client_id: 'c-a',
    client_key: 'alzi',
    policy: {
      id: 'p1',
      client_app_id: 'c-a',
      client_key: 'alzi',
      is_enabled: true,
      max_input_tokens: 8192,
      max_output_tokens: null,
      max_concurrent_requests: 4,
      priority: 0,
      created_at: '2026-10-01T00:00:00Z',
      updated_at: '2026-10-06T01:02:03Z',
    },
    ...overrides,
  }
}

function NavTo({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(to)}>
      {label}
    </button>
  )
}

function renderDetail(path = '/clients/c-a') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/clients/:clientId" element={<ClientDetailPage />} />
        <Route path="/clients" element={<div>Clients list</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderNavigable(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <NavTo to="/clients/c-b" label="go-c-b" />
      <Routes>
        <Route path="/clients/:clientId" element={<ClientDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ClientDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(clientsApi, 'getClient')
    vi.spyOn(clientsApi, 'getClientRuntimePolicy')
    vi.spyOn(clientsApi, 'listClients')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders client + policy and stays read-only', async () => {
    vi.mocked(clientsApi.getClient).mockResolvedValue(makeClient())
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    expect(screen.getByText('unrestricted')).toBeInTheDocument()
    expect(
      screen.getByText(/smaller = higher priority intent/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/항상 Gateway에서 전달된다고 단정하지/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /create|save|update|put/i })).toBeNull()
    const methods = fetchSpy.mock.calls.map(
      (c) => (c[1] as RequestInit | undefined)?.method || 'GET',
    )
    expect(methods.every((m) => m === 'GET')).toBe(true)
    expect(clientsApi.listClients).not.toHaveBeenCalled()
  })

  it('shows null policy as no policy row', async () => {
    vi.mocked(clientsApi.getClient).mockResolvedValue(makeClient())
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(
      makePolicy({ policy: null }),
    )
    renderDetail()
    expect(
      await screen.findByText(/Runtime Policy 행이 없습니다/),
    ).toBeInTheDocument()
  })

  it('keeps same-id non-404 refresh and clears on cross-id failure', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.getClient)
      .mockResolvedValueOnce(makeClient())
      .mockRejectedValueOnce(new ApiError('client boom', { status: 500 }))
      .mockRejectedValueOnce(new ApiError('c-b boom', { status: 500 }))
    vi.mocked(clientsApi.getClientRuntimePolicy)
      .mockResolvedValueOnce(makePolicy())
      .mockRejectedValueOnce(new ApiError('policy boom', { status: 500 }))
      .mockRejectedValueOnce(new ApiError('c-b boom', { status: 500 }))
    renderNavigable('/clients/c-a')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText(/client boom/)).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'go-c-b' }))
    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { level: 2, name: 'ALZI' }),
      ).not.toBeInTheDocument()
    })
    expect(screen.getByText(/c-b boom/)).toBeInTheDocument()
  })

  it('clears on authoritative client 404', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.getClient)
      .mockResolvedValueOnce(makeClient())
      .mockRejectedValueOnce(new ApiError('gone', { status: 404 }))
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(
      await screen.findByText('Client를 찾을 수 없습니다.'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'ALZI' }),
    ).not.toBeInTheDocument()
  })
})
