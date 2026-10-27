import { describe, expect, it } from 'vitest'
import {
  CACHE_FIELD_HELP,
  CATALOG_FIELD_HELP,
  CLIENT_FIELD_HELP,
  FIT_RESULT_HELP,
  MODEL_TYPE_HELP,
  NAV_ITEMS,
  OBSERVABILITY_FIELD_HELP,
  OPERATION_TYPE_LABELS,
  PAGE_INTRO,
  RUNTIME_FIELD_HELP,
  WIZARD_FIELD_HELP,
  WIZARD_START_STATUS,
  WIZARD_STEP_LABELS,
  WIZARD_STEP_PURPOSE,
  formatApiTypeDisplay,
  formatNullableLimit,
  formatOperationTypeDisplay,
  formatPreflightMemoryMb,
  formatPriorityDisplay,
  formatStatusDisplay,
  formatTrafficStateDisplay,
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
    expect(CLIENT_FIELD_HELP.registryEditable).toMatch(/등록·표시 이름/)
    expect(CLIENT_FIELD_HELP.clientKey).toMatch(/API 인증키·비밀번호가 아닙니다/)
    expect(CLIENT_FIELD_HELP.clientKeyIntegration).toMatch(/MODELOPS_CLIENT_KEY/)
    expect(CLIENT_FIELD_HELP.clientKeyIntegration).toMatch(/자동으로 붙지 않습니다/)
    expect(CLIENT_FIELD_HELP.policyEditable).toMatch(/SSO\/RBAC/)
    expect(CLIENT_FIELD_HELP.policyNull).toMatch(/새 정책 만들기/)
    expect(CLIENT_FIELD_HELP.priority).toMatch(/null=미설정/)
    expect(CLIENT_FIELD_HELP.deactivateImpact).toMatch(/직접 차단하지 않습니다/)
    expect(CLIENT_FIELD_HELP.deactivateImpact).toMatch(/토큰·동시 요청 제한/)
  })
})

describe('uiLabels (P1-26A page intros)', () => {
  it('keeps short beginner intros and P95 non-max wording', () => {
    expect(PAGE_INTRO.dashboard).toMatch(/한눈에/)
    expect(PAGE_INTRO.observability).toMatch(/저장된 데이터/)
    expect(PAGE_INTRO.clients).toMatch(/업무 서비스/)
    expect(PAGE_INTRO.runtimePolicyDialog).toMatch(/비워두면/)
    expect(OBSERVABILITY_FIELD_HELP.latency).toMatch(/최대값/)
    expect(OBSERVABILITY_FIELD_HELP.lastCalledAt).toMatch(/조회 기간/)
    expect(OBSERVABILITY_FIELD_HELP.usageStatus).toMatch(/집계 확인 불가/)
  })
})

describe('uiLabels (P1-26D display helpers)', () => {
  it('maps known operation types and leaves unknown enums raw', () => {
    expect(OPERATION_TYPE_LABELS.SWITCH).toBe('모델 전환')
    expect(formatOperationTypeDisplay('START')).toBe('모델 실행 (START)')
    expect(formatOperationTypeDisplay('DELETE')).toBe(
      '모델 컨테이너 제거 (DELETE)',
    )
    expect(formatOperationTypeDisplay('WEIRD_CUSTOM')).toBe('WEIRD_CUSTOM')
    expect(formatOperationTypeDisplay(null)).toBe('—')
    expect(formatOperationTypeDisplay('')).toBe('—')
  })

  it('formats API type and traffic with Korean labels and codes', () => {
    expect(formatApiTypeDisplay('CHAT')).toBe('대화형 API (CHAT)')
    expect(formatApiTypeDisplay('EMBEDDING')).toBe('임베딩 API (EMBEDDING)')
    expect(formatApiTypeDisplay('OTHER')).toBe('OTHER')
    expect(formatTrafficStateDisplay('SERVING')).toBe(
      '요청 처리 중 (SERVING)',
    )
    expect(formatTrafficStateDisplay('DRAINING')).toBe(
      '요청 종료 중 (DRAINING)',
    )
    expect(formatTrafficStateDisplay('MAINTENANCE')).toBe(
      '유지보수 (MAINTENANCE)',
    )
  })

  it('shows 정보 없음 for missing preflight memory without inventing 0', () => {
    expect(formatPreflightMemoryMb(null)).toBe('정보 없음')
    expect(formatPreflightMemoryMb(undefined)).toBe('정보 없음')
    expect(formatPreflightMemoryMb(Number.NaN)).toBe('정보 없음')
    expect(formatPreflightMemoryMb(0)).toBe('0 MB')
    expect(formatPreflightMemoryMb(512)).toBe('512 MB')
    expect(formatPreflightMemoryMb(16384)).toBe('16.0 GiB')
  })
})

describe('uiLabels (P1-14 catalog/cache)', () => {
  it('explains model types and fit results in beginner Korean', () => {
    expect(MODEL_TYPE_HELP.LLM).toMatch(/언어 모델/)
    expect(MODEL_TYPE_HELP.VLM).toMatch(/비전/)
    expect(MODEL_TYPE_HELP.EMBEDDING).toMatch(/벡터/)
    expect(FIT_RESULT_HELP.FIT).toMatch(/배포 성공을 보장하지 않습니다/)
    expect(FIT_RESULT_HELP.TIGHT).toMatch(/여유 부족/)
    expect(FIT_RESULT_HELP.INSUFFICIENT).toMatch(/자원 부족/)
    expect(FIT_RESULT_HELP.UNKNOWN).toMatch(/판단 정보 부족/)
  })

  it('keeps catalog/cache advisory boundaries clear', () => {
    expect(CATALOG_FIELD_HELP.fit).toMatch(/합산하지 않습니다/)
    expect(CATALOG_FIELD_HELP.fit).toMatch(/Deploy Wizard/)
    expect(CATALOG_FIELD_HELP.downloadReady).toMatch(/모델 파일 준비 완료/)
    expect(CATALOG_FIELD_HELP.downloadReady).toMatch(
      /파일 다운로드 완료 ≠ 모델 실행 중 ≠ API 연결 완료/,
    )
    expect(CACHE_FIELD_HELP.purge).toMatch(/로컬/)
    expect(CACHE_FIELD_HELP.purge).toMatch(/레지스트리/)
    expect(CACHE_FIELD_HELP.overview).toMatch(/파일 준비 완료/)
    expect(CACHE_FIELD_HELP.overview).toMatch(
      /파일 준비 완료 ≠ 모델 실행 중 ≠ API 연결 완료/,
    )
  })
})

describe('uiLabels (P1-15 wizard/models)', () => {
  it('keeps wizard step keys mapped to Korean display labels', () => {
    expect(WIZARD_STEP_LABELS.cache).toMatch(/다운로드 모델 확인/)
    expect(WIZARD_STEP_LABELS.runtime).toMatch(/모델 실행 설정/)
    expect(WIZARD_STEP_LABELS.gpu).toMatch(/GPU 선택/)
    expect(WIZARD_STEP_LABELS.review).toMatch(/배포 정보 생성/)
    expect(WIZARD_STEP_LABELS.start).toMatch(/모델 실행/)
    expect(WIZARD_STEP_LABELS.publish).toMatch(/API 연결/)
    expect(WIZARD_STEP_LABELS.done).toMatch(/결과 확인/)
  })

  it('separates Create / Start / Publish impacts', () => {
    expect(WIZARD_FIELD_HELP.create).toMatch(/컨테이너를 시작하지 않습니다/)
    expect(WIZARD_FIELD_HELP.start).toMatch(/기동/)
    expect(WIZARD_FIELD_HELP.publish).toMatch(/Alias/)
    expect(WIZARD_FIELD_HELP.publish).toMatch(
      /업무 시스템이 사용하는 모델 호출 경로/,
    )
    expect(WIZARD_FIELD_HELP.freshFit).toMatch(/실행하거나 GPU를 점유하지 않/)
    expect(WIZARD_FIELD_HELP.tensorParallel).toMatch(/단순 합산/)
    expect(WIZARD_FIELD_HELP.gpuShareCaution).toMatch(/안정적인 동시 실행/)
    expect(RUNTIME_FIELD_HELP.unsetKeepsDefault).toMatch(/자동 생성하지 않습니다/)
    expect(RUNTIME_FIELD_HELP.maxModelLen).toMatch(/모델 자체의 최대 한계가 아니/)
    expect(RUNTIME_FIELD_HELP.maxNumSeqs).toMatch(/동시 사용자 수를 보장하지/)
    expect(WIZARD_FIELD_HELP.registryList).toMatch(/실행 중인 Deployment/)
    expect(WIZARD_STEP_PURPOSE.gpu).toMatch(/Fit은 모델을 실행/)
    expect(WIZARD_START_STATUS.healthWait).toMatch(/RUNNING\+HEALTHY/)
    expect(WIZARD_FIELD_HELP.modelTypeUnknown).toMatch(
      /모델 유형에 대한 안내가 없습니다/,
    )
    expect(WIZARD_FIELD_HELP.modelTypeUnknown).not.toMatch(/텍스트 생성/)
  })
})
