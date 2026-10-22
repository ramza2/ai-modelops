import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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
    vi.spyOn(clientsApi, 'updateClient')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders client + policy and keeps Runtime Policy read-only', async () => {
    vi.mocked(clientsApi.getClient).mockResolvedValue(makeClient())
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    renderDetail()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/제한 없음/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/null \(unrestricted\)/).length).toBeGreaterThan(
      0,
    )
    expect(
      screen.getByRole('button', { name: '등록 정보 수정' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '등록 비활성화' }),
    ).toBeInTheDocument()
    expect(
      screen.getAllByText(/런타임 정책\(Runtime Policy\) 편집 UI는 아직/).length,
    ).toBeGreaterThan(0)
    expect(clientsApi.listClients).not.toHaveBeenCalled()
    expect(clientsApi.updateClient).not.toHaveBeenCalled()
  })

  it('shows null policy as no policy row', async () => {
    vi.mocked(clientsApi.getClient).mockResolvedValue(makeClient())
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(
      makePolicy({ policy: null }),
    )
    renderDetail()
    expect(
      await screen.findByText(/Runtime Policy 행이 없습니다|정책 행이 없습니다/),
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
    expect(screen.getAllByText(/c-b boom/).length).toBeGreaterThan(0)
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
      await screen.findByText(/사용 시스템을 찾을 수 없습니다/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 2, name: 'ALZI' }),
    ).not.toBeInTheDocument()
  })

  it('edits display_name/description with preflight GET and null description', async () => {
    const user = userEvent.setup()
    const baseline = makeClient({ description: 'demo' })
    vi.mocked(clientsApi.getClient).mockResolvedValue(baseline)
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    vi.mocked(clientsApi.updateClient).mockResolvedValue(
      makeClient({
        display_name: 'Renamed',
        description: null,
        updated_at: '2026-10-09T00:00:00Z',
      }),
    )
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 정보 수정' }),
    )
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'Renamed')
    await user.clear(within(dialog).getByLabelText(/설명/))
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    await waitFor(() => {
      expect(clientsApi.updateClient).toHaveBeenCalledTimes(1)
    })
    // preflight GET + initial load GETs
    expect(
      vi.mocked(clientsApi.getClient).mock.calls.length,
    ).toBeGreaterThanOrEqual(2)
    expect(vi.mocked(clientsApi.updateClient).mock.calls[0]![1]).toEqual({
      display_name: 'Renamed',
      description: null,
    })
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Renamed' }),
    ).toBeInTheDocument()
  })

  it('blocks PATCH when preflight GET fails', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.getClient)
      .mockResolvedValueOnce(makeClient())
      .mockRejectedValueOnce(new ApiError('preflight fail', { status: 500 }))
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 정보 수정' }),
    )
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'Changed')
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    expect(await within(dialog).findByText(/preflight fail/)).toBeInTheDocument()
    expect(clientsApi.updateClient).not.toHaveBeenCalled()
  })

  it('requires consent and explains deactivate impact before is_active=false', async () => {
    const user = userEvent.setup()
    const baseline = makeClient({ is_active: true })
    vi.mocked(clientsApi.getClient).mockResolvedValue(baseline)
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    vi.mocked(clientsApi.updateClient).mockResolvedValue(
      makeClient({ is_active: false, updated_at: '2026-10-09T00:00:00Z' }),
    )
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 비활성화' }),
    )
    const dialog = await screen.findByTestId('client-active-confirm-dialog')
    expect(within(dialog).getByText(/직접 차단하지 않습니다/)).toBeInTheDocument()
    expect(
      within(dialog).getByText(/토큰·동시 요청 제한이 적용되지 않을 수 있습니다/),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole('button', { name: '비활성화 실행' }),
    ).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(
      within(dialog).getByRole('button', { name: '비활성화 실행' }),
    )
    await waitFor(() => {
      expect(clientsApi.updateClient).toHaveBeenCalledWith(
        'c-a',
        { is_active: false },
        expect.objectContaining({
          idempotencyKey: expect.stringMatching(
            /^[0-9a-f-]{36}$/i,
          ),
        }),
      )
    })
  })

  it('reactivates inactive client with is_active=true', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.getClient).mockResolvedValue(
      makeClient({ is_active: false }),
    )
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(
      makePolicy({ policy: null }),
    )
    vi.mocked(clientsApi.updateClient).mockResolvedValue(
      makeClient({ is_active: true, updated_at: '2026-10-09T00:00:00Z' }),
    )
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 재활성화' }),
    )
    const dialog = await screen.findByTestId('client-active-confirm-dialog')
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(
      within(dialog).getByRole('button', { name: '재활성화 실행' }),
    )
    await waitFor(() => {
      expect(clientsApi.updateClient).toHaveBeenCalledWith(
        'c-a',
        { is_active: true },
        expect.objectContaining({ idempotencyKey: expect.any(String) }),
      )
    })
  })

  it('ignores stale edit save for a different client id', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.getClient)
      .mockResolvedValueOnce(makeClient({ id: 'c-a', client_key: 'alzi' }))
      .mockResolvedValueOnce(makeClient({ id: 'c-a', client_key: 'alzi' }))
      .mockResolvedValue(makeClient({ id: 'c-b', client_key: 'other', display_name: 'Other' }))
    vi.mocked(clientsApi.getClientRuntimePolicy)
      .mockResolvedValueOnce(makePolicy())
      .mockResolvedValue(
        makePolicy({
          client_id: 'c-b',
          client_key: 'other',
          policy: null,
        }),
      )
    let resolveUpdate!: (value: ClientApp) => void
    vi.mocked(clientsApi.updateClient).mockImplementation(
      () =>
        new Promise<ClientApp>((resolve) => {
          resolveUpdate = resolve
        }),
    )
    renderNavigable('/clients/c-a')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '등록 정보 수정' }))
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'Stale')
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    await waitFor(() => {
      expect(clientsApi.updateClient).toHaveBeenCalled()
    })
    await user.click(screen.getByRole('button', { name: 'go-c-b' }))
    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { level: 2, name: 'ALZI' }),
      ).not.toBeInTheDocument()
    })
    resolveUpdate(
      makeClient({
        id: 'c-a',
        display_name: 'Stale',
        updated_at: '2026-10-10T00:00:00Z',
      }),
    )
    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { level: 2, name: 'Stale' }),
      ).not.toBeInTheDocument()
    })
  })
})
