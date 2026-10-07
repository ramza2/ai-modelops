import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { ModelSummary, Paginated } from '../api/types'
import { ModelsPage } from '../pages/ModelsPage'
import * as modelsApi from '../api/models'

function makeModel(
  overrides: Partial<ModelSummary> & Pick<ModelSummary, 'id' | 'name' | 'slug'>,
): ModelSummary {
  return {
    model_type: 'LLM',
    provider: 'example',
    source_type: 'HF',
    license_name: 'Apache-2.0',
    description: 'desc',
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function pageResult(
  items: ModelSummary[],
  opts: { page?: number; total?: number } = {},
): Paginated<ModelSummary> {
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
        <Route path="/models" element={<ModelsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ModelsPage', () => {
  beforeEach(() => {
    vi.spyOn(modelsApi, 'listModels')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders successful list and links to detail', async () => {
    vi.mocked(modelsApi.listModels).mockResolvedValue(
      pageResult([
        makeModel({ id: 'm1', name: 'Chat A', slug: 'chat-a' }),
        makeModel({
          id: 'm2',
          name: 'Embed B',
          slug: 'embed-b',
          model_type: 'EMBEDDING',
          is_active: false,
        }),
      ]),
    )
    renderAt('/models')
    expect(await screen.findByRole('link', { name: 'Chat A' })).toBeInTheDocument()
    expect(screen.getByText('chat-a')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'EMBEDDING' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Chat A' })).toHaveAttribute(
      'href',
      '/models/m1',
    )
    expect(screen.queryByText('2026-10-06T01:02:03Z')).not.toBeInTheDocument()
  })

  it('does not call per-row version/detail endpoints', async () => {
    const getModel = vi.spyOn(modelsApi, 'getModel')
    const listVersions = vi.spyOn(modelsApi, 'listModelVersions')
    vi.mocked(modelsApi.listModels).mockResolvedValue(
      pageResult([makeModel({ id: 'm1', name: 'A', slug: 'a' })]),
    )
    renderAt('/models')
    await screen.findByText('A')
    expect(modelsApi.listModels).toHaveBeenCalledTimes(1)
    expect(getModel).not.toHaveBeenCalled()
    expect(listVersions).not.toHaveBeenCalled()
  })

  it('applies model_type and active filters and resets page', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.listModels).mockResolvedValue(pageResult([]))
    renderAt('/models?page=2')
    await screen.findByLabelText('Type')
    await user.selectOptions(screen.getByLabelText('Type'), 'LLM')
    await waitFor(() => {
      const last = vi.mocked(modelsApi.listModels).mock.calls.at(-1)?.[0]
      expect(last?.modelType).toBe('LLM')
      expect(last?.page).toBe(1)
    })
    await user.selectOptions(screen.getByLabelText('Active'), 'INACTIVE')
    await waitFor(() => {
      const last = vi.mocked(modelsApi.listModels).mock.calls.at(-1)?.[0]
      expect(last?.isActive).toBe(false)
      expect(last?.page).toBe(1)
    })
  })

  it('submits q and provider search without keystroke requests', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.listModels).mockResolvedValue(pageResult([]))
    renderAt('/models')
    await screen.findByLabelText('검색')
    const callsBefore = vi.mocked(modelsApi.listModels).mock.calls.length
    await user.type(screen.getByLabelText('검색'), 'qwen chat')
    await user.type(screen.getByLabelText('Provider'), 'acme')
    expect(vi.mocked(modelsApi.listModels).mock.calls.length).toBe(callsBefore)
    await user.click(screen.getByRole('button', { name: '검색' }))
    await waitFor(() => {
      const last = vi.mocked(modelsApi.listModels).mock.calls.at(-1)?.[0]
      expect(last?.q).toBe('qwen chat')
      expect(last?.provider).toBe('acme')
      expect(last?.page).toBe(1)
    })
  })

  it('restores URL state', async () => {
    vi.mocked(modelsApi.listModels).mockResolvedValue(pageResult([]))
    renderAt('/models?model_type=VLM&active=true&q=demo&page=2')
    await waitFor(() => {
      expect(modelsApi.listModels).toHaveBeenCalledWith(
        expect.objectContaining({
          modelType: 'VLM',
          isActive: true,
          q: 'demo',
          page: 2,
        }),
      )
    })
  })

  it.each([
    ['/models?page=2abc', ''],
    ['/models?model_type=OTHER&page=1', ''],
    ['/models?model_type=ALL', ''],
    ['/models?model_type=llm', 'model_type=LLM'],
    ['/models?model_type=VlM', 'model_type=VLM'],
    ['/models?active=TRUE', 'active=true'],
    ['/models?active=yes', ''],
    ['/models?model_type=llm&active=false&page=002', 'model_type=LLM&active=false&page=2'],
    [
      '/models?model_type=llm&active=true&q=qwen&page=2',
      'model_type=LLM&active=true&q=qwen&page=2',
    ],
  ])('canonicalizes URL %s', async (path, expected) => {
    vi.mocked(modelsApi.listModels).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(expected)
    })
  })

  it('paginates and normalizes total=0 out-of-range page', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.listModels).mockImplementation(async (params) => {
      const page = params?.page ?? 1
      return pageResult(
        page === 1
          ? [makeModel({ id: 'm1', name: 'p1', slug: 'p1' })]
          : [makeModel({ id: 'm2', name: 'p2', slug: 'p2' })],
        { page, total: 25 },
      )
    })
    renderAt('/models')
    expect(await screen.findByRole('link', { name: 'p1' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '다음' }))
    expect(await screen.findByRole('link', { name: 'p2' })).toBeInTheDocument()

    vi.mocked(modelsApi.listModels).mockResolvedValue(
      pageResult([], { page: 5, total: 0 }),
    )
    cleanup()
    renderAt('/models?page=5')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent('')
    })
  })

  it('shows empty and initial error; preserves rows on refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(modelsApi.listModels).mockResolvedValueOnce(pageResult([]))
    renderAt('/models')
    expect(await screen.findByText('등록된 Model이 없습니다.')).toBeInTheDocument()
    cleanup()

    vi.mocked(modelsApi.listModels).mockRejectedValueOnce(
      new ApiError('boom', { status: 500 }),
    )
    renderAt('/models')
    expect(await screen.findByText('boom')).toBeInTheDocument()
    cleanup()

    vi.mocked(modelsApi.listModels)
      .mockResolvedValueOnce(
        pageResult([makeModel({ id: 'm1', name: 'Keep Model', slug: 'keep-slug' })]),
      )
      .mockRejectedValueOnce(new ApiError('refresh fail', { status: 503 }))
    renderAt('/models')
    expect(await screen.findByRole('link', { name: 'Keep Model' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh fail')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Keep Model' })).toBeInTheDocument()
  })

  it('renders unknown model type safely and uses scope=col', async () => {
    vi.mocked(modelsApi.listModels).mockResolvedValue(
      pageResult([
        makeModel({
          id: 'm1',
          name: 'X',
          slug: 'x',
          model_type: 'FUTURE_TYPE',
        }),
      ]),
    )
    renderAt('/models')
    expect(await screen.findByText('FUTURE_TYPE')).toBeInTheDocument()
    for (const th of within(screen.getByRole('table')).getAllByRole(
      'columnheader',
    )) {
      expect(th).toHaveAttribute('scope', 'col')
    }
  })
})
