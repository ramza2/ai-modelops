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

  it('keeps draft inputs when same-client refresh succeeds while edit is open', async () => {
    const user = userEvent.setup()
    const baseline = makeClient({
      display_name: 'ALZI',
      description: 'demo',
      updated_at: '2026-10-06T01:02:03Z',
    })
    vi.mocked(clientsApi.getClient).mockResolvedValue(baseline)
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 정보 수정' }),
    )
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'Draft Name')
    // Same Client id, new object identity from refresh.
    vi.mocked(clientsApi.getClient).mockResolvedValue({
      ...baseline,
      updated_at: '2026-10-06T01:02:03Z',
    })
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    await waitFor(() => {
      expect(vi.mocked(clientsApi.getClient).mock.calls.length).toBeGreaterThan(
        1,
      )
    })
    expect(within(dialog).getByLabelText(/표시 이름/)).toHaveValue('Draft Name')
    expect(within(dialog).getByLabelText(/설명/)).toHaveValue('demo')
  })

  it('keeps submit lock and single PATCH when refresh runs during save', async () => {
    const user = userEvent.setup()
    const baseline = makeClient({ display_name: 'ALZI' })
    let resolvePreflight!: (value: ClientApp) => void
    let resolvePatch!: (value: ClientApp) => void
    let getCalls = 0
    vi.mocked(clientsApi.getClient).mockImplementation(async () => {
      getCalls += 1
      if (getCalls === 1) return baseline
      // 2nd = preflight during save (hang); later = refresh during save
      if (getCalls === 2) {
        return new Promise<ClientApp>((resolve) => {
          resolvePreflight = resolve
        })
      }
      return baseline
    })
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    vi.mocked(clientsApi.updateClient).mockImplementation(
      () =>
        new Promise<ClientApp>((resolve) => {
          resolvePatch = resolve
        }),
    )
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 정보 수정' }),
    )
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'Locked')
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    expect(
      await within(dialog).findByRole('button', { name: '저장 중…' }),
    ).toBeDisabled()
    // Same-client refresh while preflight is in flight.
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    // Duplicate confirm while locked must not start another save.
    await user.click(within(dialog).getByRole('button', { name: '저장 중…' }))
    resolvePreflight(baseline)
    await waitFor(() => {
      expect(clientsApi.updateClient).toHaveBeenCalledTimes(1)
    })
    const idemKey = vi.mocked(clientsApi.updateClient).mock.calls[0]![2]
      .idempotencyKey
    expect(idemKey).toMatch(/^[0-9a-f-]{36}$/i)
    // Refresh again while PATCH is hanging — still one PATCH, same key.
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(clientsApi.updateClient).toHaveBeenCalledTimes(1)
    expect(
      vi.mocked(clientsApi.updateClient).mock.calls[0]![2].idempotencyKey,
    ).toBe(idemKey)
    resolvePatch(
      makeClient({
        display_name: 'Locked',
        updated_at: '2026-10-09T00:00:00Z',
      }),
    )
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Locked' }),
    ).toBeInTheDocument()
  })

  it('blocks PATCH on updated_at conflict and preserves draft inputs', async () => {
    const user = userEvent.setup()
    const baseline = makeClient({
      display_name: 'ALZI',
      description: 'old',
      updated_at: '2026-10-06T01:02:03Z',
    })
    vi.mocked(clientsApi.getClient)
      .mockResolvedValueOnce(baseline)
      .mockResolvedValueOnce(
        makeClient({
          display_name: 'Server Name',
          description: 'server-desc',
          updated_at: '2026-10-07T00:00:00Z',
        }),
      )
    vi.mocked(clientsApi.getClientRuntimePolicy).mockResolvedValue(makePolicy())
    renderDetail()
    await user.click(
      await screen.findByRole('button', { name: '등록 정보 수정' }),
    )
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'My Draft')
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    expect(
      await within(dialog).findByText(/다른 변경이 감지되어 저장을 중단/),
    ).toBeInTheDocument()
    expect(clientsApi.updateClient).not.toHaveBeenCalled()
    expect(within(dialog).getByLabelText(/표시 이름/)).toHaveValue('My Draft')
    expect(within(dialog).getByTestId('edit-conflict-banner')).toHaveTextContent(
      'Server Name',
    )
    expect(within(dialog).getByTestId('edit-conflict-banner')).toHaveTextContent(
      'My Draft',
    )
  })

  it('does not PATCH when Client ID changes during preflight GET', async () => {
    const user = userEvent.setup()
    let resolvePreflight!: (value: ClientApp) => void
    let getCalls = 0
    vi.mocked(clientsApi.getClient).mockImplementation(async () => {
      getCalls += 1
      if (getCalls === 1) {
        return makeClient({ id: 'c-a', client_key: 'alzi' })
      }
      if (getCalls === 2) {
        return new Promise<ClientApp>((resolve) => {
          resolvePreflight = resolve
        })
      }
      return makeClient({
        id: 'c-b',
        client_key: 'other',
        display_name: 'Other',
      })
    })
    vi.mocked(clientsApi.getClientRuntimePolicy).mockImplementation(
      async (id: string) => {
        if (id === 'c-b') {
          return makePolicy({
            client_id: 'c-b',
            client_key: 'other',
            policy: null,
          })
        }
        return makePolicy()
      },
    )
    renderNavigable('/clients/c-a')
    expect(
      await screen.findByRole('heading', { level: 2, name: 'ALZI' }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '등록 정보 수정' }))
    const dialog = await screen.findByTestId('client-edit-dialog')
    const nameInput = within(dialog).getByLabelText(/표시 이름/)
    await user.clear(nameInput)
    await user.type(nameInput, 'ShouldNotPatch')
    await user.click(within(dialog).getByRole('button', { name: '저장' }))
    await waitFor(() => {
      expect(getCalls).toBeGreaterThanOrEqual(2)
    })
    await user.click(screen.getByRole('button', { name: 'go-c-b' }))
    await waitFor(() => {
      expect(
        screen.queryByTestId('client-edit-dialog'),
      ).not.toBeInTheDocument()
    })
    resolvePreflight(makeClient({ id: 'c-a', client_key: 'alzi' }))
    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: 'Other' }),
      ).toBeInTheDocument()
    })
    expect(clientsApi.updateClient).not.toHaveBeenCalled()
  })
})
