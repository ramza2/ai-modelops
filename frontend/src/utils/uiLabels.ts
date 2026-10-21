/**
 * P1-13 — beginner-facing Korean UI labels.
 *
 * Display-only. API enums, query params, and payloads must keep English codes.
 */

export type NavItem = {
  to: string
  label: string
  description: string
  /** Used only for documentation / aria; route matching stays in AppShell. */
  end?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  {
    to: '/dashboard',
    label: '대시보드',
    description: '전체 운영 현황을 한눈에 봅니다',
    end: true,
  },
  {
    to: '/nodes',
    label: '서버 및 GPU',
    description: '서버 상태와 GPU 사용량을 확인합니다',
  },
  {
    to: '/models',
    label: '모델 관리',
    description: '모델 검색·다운로드·버전을 관리합니다',
  },
  {
    to: '/deployments',
    label: '모델 배포 관리',
    description: '모델을 서버에 올리고 시작·중지합니다',
  },
  {
    to: '/endpoints',
    label: 'API 연결 관리',
    description: '업무 시스템이 호출할 API 별칭을 연결합니다',
  },
  {
    to: '/operations',
    label: '작업 이력',
    description: '배포·전환 등 비동기 작업 진행을 추적합니다',
  },
  {
    to: '/observability',
    label: '운영 모니터링',
    description: '호출량·런타임 지표를 조회합니다',
  },
  {
    to: '/clients',
    label: 'AI 사용 시스템',
    description: '모델을 쓰는 업무 서비스와 제한 정책을 봅니다',
  },
]

/** Common status display: Korean first, API code kept for operators. */
export function formatStatusDisplay(code: string | null | undefined): {
  label: string
  code: string
} {
  const normalized = (code || 'UNKNOWN').toUpperCase()
  const LABELS: Record<string, string> = {
    ACTIVE: '활성',
    INACTIVE: '비활성',
    ENABLED: '사용 중',
    DISABLED: '중지됨',
    ONLINE: '온라인',
    OFFLINE: '오프라인',
    RUNNING: '실행 중',
    STOPPED: '중지',
    HEALTHY: '정상',
    UNKNOWN: '알 수 없음',
  }
  return {
    label: LABELS[normalized] || normalized,
    code: normalized,
  }
}

export function formatNullableLimit(
  value: number | null | undefined,
): { display: string; apiHint: string } {
  if (value === null || value === undefined) {
    return {
      display: '제한 없음',
      apiHint: 'null (unrestricted)',
    }
  }
  return {
    display: String(value),
    apiHint: String(value),
  }
}

export function formatPriorityDisplay(
  value: number | null | undefined,
): { display: string; apiHint: string } {
  if (value === null || value === undefined) {
    return {
      display: '미설정',
      apiHint: 'null (unset)',
    }
  }
  return {
    display: `${value} (숫자가 작을수록 높은 우선순위 의도)`,
    apiHint: String(value),
  }
}

export const CLIENT_FIELD_HELP = {
  clientKey:
    'Gateway가 호출을 구분할 때 쓰는 식별자입니다(X-AI-Client). 직원 로그인 계정이 아니라, AI 모델을 호출하는 업무 서비스의 키입니다.',
  isActive:
    '활성(ACTIVE)은 레지스트리에서 사용 가능으로 표시된 상태입니다. 비활성(INACTIVE)은 Gateway가 Chat/Embeddings 추론 요청을 직접 차단하는 스위치가 아닙니다. 비활성이면 (1) Gateway 정책 스냅샷에 이 키의 Runtime Policy가 올라가지 않아 클라이언트별 토큰·동시성·priority 제한이 적용되지 않고, (2) 호출 로그의 client_app_id 연결도 활성 키에만 이뤄지며 raw_client_key는 그대로 남을 수 있습니다. 실제 추론 가능 여부는 Endpoint Alias·배포·traffic 상태에 따릅니다.',
  displayName: '관리 화면에서 보이는 서비스 이름입니다.',
  description: '운영자가 적어 둔 설명입니다. 비어 있을 수 있습니다.',
  createdAt:
    '이 사용 시스템이 처음 등록된 시각입니다. API 원본은 UTC(ISO-8601)이고, 이 화면의 값은 브라우저 현지 시간으로 표시합니다.',
  updatedAt:
    '메타데이터가 마지막으로 바뀐 시각입니다. API 원본은 UTC(ISO-8601)이고, 이 화면의 값은 브라우저 현지 시간으로 표시합니다.',
  runtimePolicy:
    'Gateway가 적용할 수 있는 런타임 제한입니다. 정책 행이 없으면(policy = null) 별도 제한 행이 없는 것입니다. 제한 값이 null이면 해당 항목은 제한 없음(unrestricted)입니다.',
  policyNull:
    'Runtime Policy 행이 없습니다(policy = null). 이는 “정책을 꺼 둠”과 다르며, 클라이언트별 제한 행 자체가 없는 상태입니다. 이 화면에서는 정책을 새로 만들 수 없습니다.',
  policyEnabled:
    '사용 중(enabled)이면 Gateway가 이 정책 행을 스냅샷에 올릴 수 있습니다. 중지됨(disabled)이면 이 정책 행은 적용되지 않습니다. 사용 시스템 자체의 활성(ACTIVE)과는 별개입니다.',
  maxInputTokens:
    '한 번의 Chat 요청에서 허용하는 입력(프롬프트) 토큰 상한입니다. null이면 이 정책으로 입력 토큰을 제한하지 않습니다. 실제 적용은 Gateway의 신뢰된 토큰 계산 경로에 따릅니다.',
  maxOutputTokens:
    '한 번의 Chat 요청에서 허용하는 출력 토큰 상한입니다. null이면 이 정책으로 출력 토큰을 제한하지 않습니다.',
  maxConcurrentRequests:
    '이 사용 시스템이 Gateway에서 동시에 진행할 수 있는 요청 수 상한입니다. null이면 이 정책으로 동시 요청을 제한하지 않습니다.',
  priority:
    '숫자가 작을수록 높은 우선순위 의도입니다. null이면 미설정입니다. 요청된 priority가 항상 모델 런타임으로 전달된다고 단정하지 마세요 — Gateway는 신뢰된 스케줄러 증거가 있을 때만 전달합니다.',
  readOnly:
    '이 화면은 조회 전용입니다. 사용 시스템 추가·수정·정책 변경은 현재 Admin UI에서 제공하지 않습니다(백엔드 변경 API가 Idempotency-Key를 소비하지 않아 안전하게 열지 않았습니다).',
} as const

export function userFacingLoadError(
  area: string,
  raw: string,
  options: { keptPrevious: boolean },
): { title: string; message: string; action: string } {
  if (options.keptPrevious) {
    return {
      title: `${area} 새로고침 실패`,
      message: `원인: ${raw}`,
      action:
        '이전에 불러온 내용을 그대로 보여 줍니다. 네트워크·권한을 확인한 뒤 새로고침을 다시 시도하세요.',
    }
  }
  return {
    title: `${area}를 불러오지 못했습니다`,
    message: `원인: ${raw}`,
    action: '잠시 후 새로고침을 다시 시도하세요. 문제가 계속되면 운영 담당자에게 문의하세요.',
  }
}
