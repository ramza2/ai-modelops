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
    vi.unstubAllGlobals()
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

  it('auto-generates svc- key on open and posts it with Idempotency-Key', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient).mockImplementation(async (body) =>
      makeClient({
        id: 'c-new',
        client_key: body.client_key,
        display_name: body.display_name,
        description: body.description ?? null,
      }),
    )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    const keyInput = within(dialog).getByLabelText(/서비스 키/)
    const autoKey = (keyInput as HTMLInputElement).value
    expect(autoKey).toMatch(/^svc-[0-9a-f]{32}$/)
    expect(
      within(dialog).getAllByText(/MODELOPS_CLIENT_KEY/).length,
    ).toBeGreaterThan(0)
    await user.type(within(dialog).getByLabelText(/표시 이름/), '사내 지식 AI 검색')
    await user.type(within(dialog).getByLabelText(/설명/), 'RAG')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(1)
    })
    const [body, opts] = vi.mocked(clientsApi.createClient).mock.calls[0]!
    expect(body).toEqual({
      client_key: autoKey,
      display_name: '사내 지식 AI 검색',
      description: 'RAG',
    })
    expect(opts.idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    )
    expect(await screen.findByTestId('client-detail-route')).toBeInTheDocument()
  })

  it('allows regenerate and manual override; keeps custom key on CLIENT_KEY_CONFLICT', async () => {
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
    const keyInput = within(dialog).getByLabelText(/서비스 키/)
    const firstKey = (keyInput as HTMLInputElement).value
    await user.click(within(dialog).getByTestId('client-key-regenerate'))
    const secondKey = (keyInput as HTMLInputElement).value
    expect(secondKey).toMatch(/^svc-[0-9a-f]{32}$/)
    expect(secondKey).not.toBe(firstKey)

    await user.clear(keyInput)
    await user.type(keyInput, 'dup-key')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByText(/표시 이름은 필수/),
    ).toBeInTheDocument()
    expect(clientsApi.createClient).not.toHaveBeenCalled()

    await user.type(within(dialog).getByLabelText(/표시 이름/), 'Dup')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByText(/CLIENT_KEY_CONFLICT/),
    ).toBeInTheDocument()
    expect(within(dialog).getByText(/자동으로 다른 키를 만들어 재등록하지/)).toBeInTheDocument()
    expect(within(dialog).getByLabelText(/서비스 키/)).toHaveValue('dup-key')
    expect(within(dialog).getByLabelText(/표시 이름/)).toHaveValue('Dup')
    // Conflict must not auto-regenerate.
    expect(within(dialog).getByLabelText(/서비스 키/)).toHaveValue('dup-key')
  })

  it('locks all fields on uncertain response and reuses Idempotency-Key on same payload retry', async () => {
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
    const keyInput = within(dialog).getByLabelText(/서비스 키/)
    await user.clear(keyInput)
    await user.type(keyInput, 'retry-key')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'Retry')
    await user.type(within(dialog).getByLabelText(/설명/), 'desc')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(await within(dialog).findByText(/network down/)).toBeInTheDocument()
    expect(
      within(dialog).getByTestId('create-uncertain-banner'),
    ).toBeInTheDocument()
    expect(within(dialog).getByLabelText(/서비스 키/)).toBeDisabled()
    expect(within(dialog).getByLabelText(/표시 이름/)).toBeDisabled()
    expect(within(dialog).getByLabelText(/설명/)).toBeDisabled()
    expect(
      within(dialog).getByTestId('client-key-regenerate'),
    ).toBeDisabled()
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(2)
    })
    const call1 = vi.mocked(clientsApi.createClient).mock.calls[0]!
    const call2 = vi.mocked(clientsApi.createClient).mock.calls[1]!
    expect(call1[1].idempotencyKey).toBe(call2[1].idempotencyKey)
    expect(call1[0]).toEqual(call2[0])
    expect(call1[0].client_key).toBe('retry-key')
  })

  it('unlocks only via explicit acknowledge and mints new service + Idempotency keys', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient)
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(
        makeClient({
          id: 'c-new',
          client_key: 'svc-placeholder',
          display_name: 'Retry',
        }),
      )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    const keyInput = within(dialog).getByLabelText(/서비스 키/)
    await user.clear(keyInput)
    await user.type(keyInput, 'retry-key')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'Retry')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByTestId('create-uncertain-banner'),
    ).toBeInTheDocument()
    const idem1 = vi.mocked(clientsApi.createClient).mock.calls[0]![1]
      .idempotencyKey
    await user.click(
      within(dialog).getByRole('button', { name: '확인했고 새 키로 다시 작성' }),
    )
    expect(
      within(dialog).queryByTestId('create-uncertain-banner'),
    ).not.toBeInTheDocument()
    expect(within(dialog).getByLabelText(/서비스 키/)).not.toBeDisabled()
    expect(within(dialog).getByLabelText(/표시 이름/)).not.toBeDisabled()
    expect(within(dialog).getByTestId('client-key-regenerate')).not.toBeDisabled()
    const keyAfter = (
      within(dialog).getByLabelText(/서비스 키/) as HTMLInputElement
    ).value
    expect(keyAfter).toMatch(/^svc-[0-9a-f]{32}$/)
    expect(keyAfter).not.toBe('retry-key')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(2)
    })
    const call2 = vi.mocked(clientsApi.createClient).mock.calls[1]!
    expect(call2[0].client_key).toBe(keyAfter)
    expect(call2[1].idempotencyKey).not.toBe(idem1)
  })

  it('warns about duplicate risk when closing while response is uncertain', async () => {
    const user = userEvent.setup()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient).mockRejectedValue(
      new TypeError('network down'),
    )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'CloseMe')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByTestId('create-uncertain-banner'),
    ).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '취소' }))
    expect(confirmSpy).toHaveBeenCalled()
    expect(String(confirmSpy.mock.calls[0]![0])).toMatch(/중복 등록/)
    expect(screen.getByTestId('client-create-dialog')).toBeInTheDocument()
  })

  it('mints a new Idempotency-Key after explicit regenerate changes payload', async () => {
    const user = userEvent.setup()
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    vi.mocked(clientsApi.createClient)
      .mockRejectedValueOnce(new ApiError('client_key already exists.', {
        status: 409,
        code: 'CLIENT_KEY_CONFLICT',
      }))
      .mockResolvedValueOnce(
        makeClient({ id: 'c2', client_key: 'later', display_name: 'N' }),
      )
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    await user.type(within(dialog).getByLabelText(/표시 이름/), 'N')
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    expect(
      await within(dialog).findByText(/CLIENT_KEY_CONFLICT/),
    ).toBeInTheDocument()
    const idem1 = vi.mocked(clientsApi.createClient).mock.calls[0]![1]
      .idempotencyKey
    const keyBefore = (within(dialog).getByLabelText(/서비스 키/) as HTMLInputElement)
      .value
    await user.click(within(dialog).getByTestId('client-key-regenerate'))
    const keyAfter = (within(dialog).getByLabelText(/서비스 키/) as HTMLInputElement)
      .value
    expect(keyAfter).not.toBe(keyBefore)
    await user.click(within(dialog).getByRole('button', { name: '등록' }))
    await waitFor(() => {
      expect(clientsApi.createClient).toHaveBeenCalledTimes(2)
    })
    const idem2 = vi.mocked(clientsApi.createClient).mock.calls[1]![1]
      .idempotencyKey
    expect(idem2).not.toBe(idem1)
    expect(vi.mocked(clientsApi.createClient).mock.calls[1]![0].client_key).toBe(
      keyAfter,
    )
  })

  it('copies service key and X-AI-Client header from create dialog', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText },
    })
    vi.mocked(clientsApi.listClients).mockResolvedValue(pageResult([]))
    renderAt('/clients')
    await user.click(
      await screen.findByRole('button', { name: '신규 시스템 등록' }),
    )
    const dialog = await screen.findByTestId('client-create-dialog')
    const autoKey = (
      within(dialog).getByLabelText(/서비스 키/) as HTMLInputElement
    ).value
    await user.click(
      within(dialog).getByRole('button', { name: '서비스 키 복사' }),
    )
    expect(writeText).toHaveBeenCalledWith(autoKey)
    expect(
      await within(dialog).findByText(/서비스 키를 복사했습니다/),
    ).toBeInTheDocument()
    await user.click(
      within(dialog).getByRole('button', { name: 'X-AI-Client 헤더 복사' }),
    )
    expect(writeText).toHaveBeenCalledWith(`X-AI-Client: ${autoKey}`)
  })
})
