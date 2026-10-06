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

export function normalizeApiBase(base: string | undefined | null): string {
  if (!base || base.trim() === '') {
    return ''
  }
  return base.trim().replace(/\/+$/, '')
}

/**
 * Build a fetch URL from an optional API base + path + query.
 *
 * - empty base → same-origin relative (`/api/...`)
 * - absolute `http(s)://...` → full absolute URL
 * - relative prefix (`/admin-api`) → prefixed relative path
 */
export function buildApiUrl(
  base: string | undefined | null,
  path: string,
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  const normalizedBase = normalizeApiBase(base)
  const normalizedPath = path.startsWith('/') ? path : `/${path}`

  const applyQuery = (url: URL): void => {
    if (!query) return
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue
      url.searchParams.set(key, String(value))
    }
  }

  if (!normalizedBase) {
    const url = new URL(normalizedPath, 'http://local.invalid')
    applyQuery(url)
    return `${url.pathname}${url.search}`
  }

  if (/^https?:\/\//i.test(normalizedBase)) {
    const url = new URL(`${normalizedBase}${normalizedPath}`)
    applyQuery(url)
    return url.toString()
  }

  const prefix = normalizedBase.startsWith('/')
    ? normalizedBase
    : `/${normalizedBase}`
  const url = new URL(`${prefix}${normalizedPath}`, 'http://local.invalid')
  applyQuery(url)
  return `${url.pathname}${url.search}`
}

const API_BASE = normalizeApiBase(import.meta.env.VITE_MODELOPS_API_BASE_URL)

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
  const url = buildApiUrl(API_BASE, path, options.query)
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

/** @deprecated use buildApiUrl('', ...) — kept for older tests */
export function __testBuildUrl(
  path: string,
  query?: Record<string, string | number | boolean | null | undefined>,
): string {
  return buildApiUrl('', path, query)
}
