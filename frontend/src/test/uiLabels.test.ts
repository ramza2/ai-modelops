import { describe, expect, it } from 'vitest'
import {
  CLIENT_FIELD_HELP,
  NAV_ITEMS,
  formatNullableLimit,
  formatPriorityDisplay,
  formatStatusDisplay,
  userFacingLoadError,
} from '../utils/uiLabels'

describe('uiLabels (P1-13)', () => {
  it('keeps nav routes stable while using Korean labels', () => {
    expect(NAV_ITEMS.map((n) => n.to)).toEqual([
      '/dashboard',
      '/nodes',
      '/models',
      '/deployments',
      '/endpoints',
      '/operations',
      '/observability',
      '/clients',
    ])
    expect(NAV_ITEMS.find((n) => n.to === '/clients')?.label).toBe(
      'AI 사용 시스템',
    )
    for (const item of NAV_ITEMS) {
      expect(item.description.trim().length).toBeGreaterThan(0)
    }
  })

  it('formats null limits and priority with API hints', () => {
    expect(formatNullableLimit(null)).toEqual({
      display: '제한 없음',
      apiHint: 'null (unrestricted)',
    })
    expect(formatNullableLimit(8192).display).toBe('8192')
    expect(formatPriorityDisplay(null).display).toBe('미설정')
    expect(formatPriorityDisplay(0).display).toMatch(/높은 우선순위/)
  })

  it('keeps status API codes while showing Korean labels', () => {
    expect(formatStatusDisplay('ACTIVE')).toEqual({
      label: '활성',
      code: 'ACTIVE',
    })
  })

  it('separates error cause and action', () => {
    const kept = userFacingLoadError('목록', 'timeout', { keptPrevious: true })
    expect(kept.message).toMatch(/^원인:/)
    expect(kept.action).toMatch(/이전에 불러온/)
    const fresh = userFacingLoadError('목록', 'boom', { keptPrevious: false })
    expect(fresh.action).toMatch(/다시 시도/)
  })

  it('documents local datetime display and inactive client semantics', () => {
    expect(CLIENT_FIELD_HELP.createdAt).toMatch(/브라우저 현지 시간/)
    expect(CLIENT_FIELD_HELP.createdAt).toMatch(/UTC/)
    expect(CLIENT_FIELD_HELP.updatedAt).toMatch(/브라우저 현지 시간/)
    expect(CLIENT_FIELD_HELP.isActive).toMatch(/직접 차단하는 스위치가 아닙니다/)
    expect(CLIENT_FIELD_HELP.isActive).toMatch(/정책 스냅샷/)
    expect(CLIENT_FIELD_HELP.isActive).toMatch(/client_app_id/)
    expect(CLIENT_FIELD_HELP.isActive).toMatch(/raw_client_key/)
  })
})
