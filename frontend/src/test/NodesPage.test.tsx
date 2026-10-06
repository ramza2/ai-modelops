import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { NodeSummary, Paginated } from '../api/types'
import { NodesPage, __testParsePage } from '../pages/NodesPage'
import * as nodesApi from '../api/nodes'

function makeNode(
  overrides: Partial<NodeSummary> & Pick<NodeSummary, 'id' | 'name'>,
): NodeSummary {
  return {
    hostname: 'host-a.example.test',
    agent_base_url: 'http://node-agent.example.test:9100',
    environment: 'dev',
    region: 'local',
    status: 'ONLINE',
    last_heartbeat_at: '2026-10-06T01:02:03Z',
    cpu_model: 'Test CPU',
    ram_total_mb: 16384,
    disk_total_mb: 512000,
    labels_json: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-06T01:02:03Z',
    ...overrides,
  }
}

function pageResult(
  items: NodeSummary[],
  opts: { page?: number; total?: number } = {},
): Paginated<NodeSummary> {
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
        <Route path="/nodes" element={<NodesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('parsePage strictness', () => {
  it.each([
    [null, 1],
    ['1', 1],
    ['2', 2],
    ['002', 2],
    ['0', 1],
    ['-1', 1],
    ['2.5', 1],
    ['2abc', 1],
    ['abc', 1],
    [String(Number.MAX_SAFE_INTEGER + 1), 1],
  ] as const)('parses %s → %s', (raw, expected) => {
    expect(__testParsePage(raw)).toBe(expected)
  })
})

describe('NodesPage', () => {
  beforeEach(() => {
    vi.spyOn(nodesApi, 'listNodes')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders successful Node list with status and heartbeat', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult([
        makeNode({ id: 'node-1', name: 'gpu-node-a', status: 'ONLINE' }),
        makeNode({
          id: 'node-2',
          name: 'gpu-node-b',
          status: 'DEGRADED',
          hostname: 'host-b.example.test',
          environment: 'staging',
        }),
      ]),
    )

    renderAt('/nodes')

    expect(await screen.findByText('gpu-node-a')).toBeInTheDocument()
    expect(screen.getByText('gpu-node-b')).toBeInTheDocument()
    expect(screen.getByText(/온라인/)).toBeInTheDocument()
    expect(screen.getByText(/저하/)).toBeInTheDocument()
    expect(screen.getByText('host-a.example.test')).toBeInTheDocument()
    expect(screen.getByText('staging')).toBeInTheDocument()
    // local timezone formatted — not raw ISO slice
    expect(screen.queryByText('2026-10-06T01:02:03Z')).not.toBeInTheDocument()
  })

  it('updates status filter in URL and resets page', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.listNodes).mockResolvedValue(pageResult([]))

    renderAt('/nodes?page=2')
    await screen.findByLabelText('상태')

    await user.selectOptions(screen.getByLabelText('상태'), 'ONLINE')

    await waitFor(() => {
      const last = vi.mocked(nodesApi.listNodes).mock.calls.at(-1)?.[0]
      expect(last?.status).toBe('ONLINE')
      expect(last?.page).toBe(1)
    })
  })

  it('sends page query correctly', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult(
        [makeNode({ id: 'node-1', name: 'gpu-node-a' })],
        { page: 2, total: 25 },
      ),
    )

    renderAt('/nodes?page=2')
    await screen.findByText('gpu-node-a')

    expect(nodesApi.listNodes).toHaveBeenCalledWith(
      expect.objectContaining({ page: 2, pageSize: 20 }),
    )
  })

  it('supports next/previous pagination', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.listNodes).mockImplementation(async (params) => {
      const page = params?.page ?? 1
      return pageResult(
        [makeNode({ id: `node-${page}`, name: `node-p${page}` })],
        { page, total: 25 },
      )
    })

    renderAt('/nodes')
    expect(await screen.findByText('node-p1')).toBeInTheDocument()
    expect(screen.getByText('1–20 / 25')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '다음' }))
    expect(await screen.findByText('node-p2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '이전' }))
    expect(await screen.findByText('node-p1')).toBeInTheDocument()
  })

  it('shows empty result state', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(pageResult([]))
    renderAt('/nodes')
    expect(
      await screen.findByText('등록된 Node가 없습니다.'),
    ).toBeInTheDocument()
  })

  it('shows initial API failure', async () => {
    vi.mocked(nodesApi.listNodes).mockRejectedValue(
      new ApiError('boom', { status: 500, code: 'INTERNAL' }),
    )
    renderAt('/nodes')
    expect(await screen.findByText('boom')).toBeInTheDocument()
  })

  it('preserves previous rows on refresh failure', async () => {
    const user = userEvent.setup()
    vi.mocked(nodesApi.listNodes)
      .mockResolvedValueOnce(
        pageResult([makeNode({ id: 'node-1', name: 'keep-me' })]),
      )
      .mockRejectedValueOnce(
        new ApiError('refresh failed', { status: 503, code: 'UNAVAILABLE' }),
      )

    renderAt('/nodes')
    expect(await screen.findByText('keep-me')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '새로고침' }))
    expect(await screen.findByText('refresh failed')).toBeInTheDocument()
    expect(screen.getByText('keep-me')).toBeInTheDocument()
  })

  it('does not call per-row resource or detail endpoints', async () => {
    const getNode = vi.spyOn(nodesApi, 'getNode')
    const getResources = vi.spyOn(nodesApi, 'getNodeResources')
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult([
        makeNode({ id: 'node-1', name: 'a' }),
        makeNode({ id: 'node-2', name: 'b' }),
      ]),
    )

    renderAt('/nodes')
    await screen.findByText('a')

    expect(nodesApi.listNodes).toHaveBeenCalledTimes(1)
    expect(getNode).not.toHaveBeenCalled()
    expect(getResources).not.toHaveBeenCalled()
  })

  it('links node names to detail', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult([makeNode({ id: 'node-1', name: 'link-me' })]),
    )
    renderAt('/nodes')
    const link = await screen.findByRole('link', { name: 'link-me' })
    expect(link).toHaveAttribute('href', '/nodes/node-1')
  })

  it('shows filtered empty state', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(pageResult([]))
    renderAt('/nodes?status=OFFLINE')
    expect(
      await screen.findByText('선택한 상태에 해당하는 Node가 없습니다.'),
    ).toBeInTheDocument()
  })

  it('table headers use scope=col', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult([makeNode({ id: 'node-1', name: 'a' })]),
    )
    renderAt('/nodes')
    await screen.findByText('a')
    const headers = within(screen.getByRole('table')).getAllByRole('columnheader')
    for (const th of headers) {
      expect(th).toHaveAttribute('scope', 'col')
    }
  })

  it.each([
    ['/nodes?page=2.5', ''],
    ['/nodes?page=2abc', ''],
    ['/nodes?page=0', ''],
    ['/nodes?page=-1', ''],
    ['/nodes?status=ONLINE&page=2abc', 'status=ONLINE'],
    ['/nodes?page=002', 'page=2'],
  ])('canonicalizes malformed page URL %s → %s', async (path, expectedSearch) => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(pageResult([]))
    renderAt(path)
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        expectedSearch,
      )
    })
  })

  it('normalizes out-of-range page when total=0 to page 1', async () => {
    vi.mocked(nodesApi.listNodes).mockImplementation(async (params) => {
      return pageResult([], { page: params?.page ?? 1, total: 0 })
    })
    renderAt('/nodes?page=5')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent('')
    })
    expect(
      await screen.findByText('등록된 Node가 없습니다.'),
    ).toBeInTheDocument()
  })

  it('preserves status when normalizing filtered empty out-of-range page', async () => {
    vi.mocked(nodesApi.listNodes).mockResolvedValue(
      pageResult([], { page: 4, total: 0 }),
    )
    renderAt('/nodes?status=OFFLINE&page=4')
    await waitFor(() => {
      expect(screen.getByTestId('location-search')).toHaveTextContent(
        'status=OFFLINE',
      )
    })
    expect(
      await screen.findByText('선택한 상태에 해당하는 Node가 없습니다.'),
    ).toBeInTheDocument()
  })
})
