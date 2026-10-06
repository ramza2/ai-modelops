import type { ModelOpsErrorBody } from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly body: unknown

  constructor(
    message: string,
    opts: { status: number; code?: string | null; body?: unknown },
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code ?? null
    this.body = opts.body
  }
}

function normalizeBase(base: string | undefined): string {
  if (!base || base.trim() === '') {
    return ''
  }
  return base.replace(/\/+$/, '')
}

const API_BASE = normalizeBase(import.meta.env.VITE_MODELOPS_API_BASE_URL)

function buildUrl(
  path: string,
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  const url = new URL(`${API_BASE}${normalizedPath}`, 'http://local.invalid')
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue
      url.searchParams.set(key, String(value))
    }
  }
  // Same-origin relative path (drop fake origin).
  return `${url.pathname}${url.search}`
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return text
  }
}

function messageFromBody(body: unknown, fallback: string): {
  message: string
  code: string | null
} {
  if (body && typeof body === 'object') {
    const envelope = body as ModelOpsErrorBody
    const err = envelope.error
    if (err && typeof err === 'object') {
      return {
        message: err.message || fallback,
        code: err.code ?? null,
      }
    }
  }
  if (typeof body === 'string' && body.trim()) {
    return { message: body.slice(0, 200), code: null }
  }
  return { message: fallback, code: null }
}

export type ApiFetchOptions = {
  query?: Record<string, string | number | boolean | null | undefined>
  signal?: AbortSignal
}

export async function apiGet<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const url = buildUrl(path, options.query)
  const response = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: options.signal,
  })
  const body = await parseBody(response)
  if (!response.ok) {
    const { message, code } = messageFromBody(
      body,
      `Request failed (${response.status})`,
    )
    throw new ApiError(message, {
      status: response.status,
      code,
      body,
    })
  }
  return body as T
}

/** Exposed for tests — builds relative URL with query encoding. */
export function __testBuildUrl(
  path: string,
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  return buildUrl(path, query)
}
