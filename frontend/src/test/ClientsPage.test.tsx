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
        <Route
          path="/clients/:clientId"
          element={<div data-testid="client-detail-route">detail</div>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ClientsPage', () => {
  beforeEach(() => {
    vi.spyOn(clientsApi, 'listClients')
    vi.spyOn(clientsApi, 'getClient')
    vi.spyOn(clientsApi, 'getClientRuntimePolicy')
    vi.spyOn(clientsApi, 'createClient')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders list without N+1 detail fetches on load', async () => {
    vi.mocked(clientsApi.listClients).mockResolvedValue(
      pageResult([
        makeClient({ id: 'c1', client_key: 'alzi', display_name: 'ALZI' }),
      ]),
    )
    renderAt('/clients')
    expect(await screen.findByRole('link', { name: 'ALZI' })).toHaveAttribute(
      'href',
      '/clients/c1',
    )
    expect(clientsApi.listClients).toHaveBeenCalledTimes(1)
    expect(clientsApi.getClient).not.toHaveBeenCalled()
    expect(clientsApi.getClientRuntimePolicy).not.toHaveBeenCalled()
    expect(
      screen.getByRole('button', { name: '신규 시스템 등록' }),
    ).toBeInTheDocument()
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
    expect(await screen.findByText(/refresh fail/)).toBeInTheDocument()
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

  it('creates a client with Idempotency-Key and navigates to detail', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient).mockResolvedValue(
      makeClient({
        id: 'c-new',
        client_key: 'AlZi',
        display_name: '사내 지식 AI 검색',
        description: 'RAG',
      }),
    )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    await user.type(within(dialog).getByLabelText(/서비스 키/), 'AlZi')
    await user.type(within(dialog).getByLabelText(/표시 이름/), '사내 지식 AI 검색')
    await user.type(within(dialog).getByLabelText(/설명/), 'RAG')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(1)
    })
    const [body, opts] = vi.mocked(clientsApi.createClient).mock.calls[0]!
    expect(body).toEqual({
      client_key: 'AlZi',
      display_name: '사내 지식 AI 검색',
      description: 'RAG',
    })
    expect(opts.idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    )
    expect(await screen.findByTestId('client-detail-route')).toBeInTheDocument()
  })

  it('validates required fields and preserves input on CLIENT_KEY_CONFLICT', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient).mockRejectedValue(
      new ApiError('client_key already exists.', {
        status: 409,
        code: 'CLIENT_KEY_CONFLICT',
      }),
    )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(await within(dialog).findByText(/서비스 키는 필수/)).toBeInTheDocument()
    expect(clientsApi.createClient).not.toHaveBeenCalled()

    await user.type(within(dialog).getByLabelText(/서비스 키/), 'dup-key')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'Dup')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByText(/CLIENT_KEY_CONFLICT/),
    ).toBeInTheDocument()
    expect(within(dialog).getByLabelText(/서비스 키/)).toHaveValue('dup-key')
    expect(within(dialog).getByLabelText(/표시 이름/)).toHaveValue('Dup')
  })

  it('reuses Idempotency-Key on uncertain retry with same payload', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient)
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(
        makeClient({
          id: 'c-retry',
          client_key: 'retry-key',
          display_name: 'Retry',
        }),
      )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    await user.type(within(dialog).getByLabelText(/서비스 키/), 'retry-key')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'Retry')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(await within(dialog).findByText(/network down/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(2)
    })
    const key1 = vi.mocked(clientsApi.createClient).mock.calls[0]![1]
      .idempotencyKey
    const key2 = vi.mocked(clientsApi.createClient).mock.calls[1]![1]
      .idempotencyKey
    expect(key1).toBe(key2)
  })
})
