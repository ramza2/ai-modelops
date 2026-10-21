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

/** P1-14 — Model Catalog / Cache beginner help (display-only). */
export const MODEL_TYPE_HELP = {
  LLM: '대화·문서 요약 등에 쓰는 텍스트 언어 모델입니다(LLM).',
  VLM: '이미지와 텍스트를 함께 다루는 비전-언어 모델입니다(VLM).',
  EMBEDDING:
    '검색·유사도 비교용으로 문장을 숫자 벡터로 바꾸는 모델입니다(Embedding).',
} as const

export const FIT_RESULT_HELP = {
  FIT: '자원 적합(FIT): 선택된 서버의 GPU별 여유 메모리·디스크가 예상 요구를 충족하는 참고 판정입니다. 배포 성공을 보장하지 않습니다.',
  TIGHT:
    '여유 부족 가능성(TIGHT): 이론상 가능하지만 안전 여유(safety margin)가 빠듯합니다. 실제 배포 전 Deploy Wizard에서 다시 확인하세요.',
  INSUFFICIENT:
    '자원 부족(INSUFFICIENT): 예상 필요량보다 GPU 메모리 또는 디스크가 부족합니다. GPU 메모리 부족과 디스크 부족은 별도로 표시됩니다.',
  UNKNOWN:
    '판단 정보 부족(UNKNOWN): 크기·VRAM 추정 또는 서버 자원 정보가 부족해 판정하지 못했습니다. 참고용이며 배포를 막지 않습니다.',
} as const

export const CATALOG_FIELD_HELP = {
  overview:
    '순서: (1) 모델 검색 (2) 대상 서버 선택 (3) GPU 적합성(Fit) 분석 — 참고용 (4) 다운로드. Fit은 실제 GPU를 고르거나 모델을 실행하지 않으며, 배포·API 게시는 이후 단계에서 진행합니다.',
  node: '다운로드와 Fit 분석이 적용될 GPU 서버입니다. 서버를 바꾸면 이전 Fit·다운로드 추적 표시가 초기화됩니다(저장된 Job은 F5 후 복원될 수 있음).',
  repository: 'Hugging Face 모델 저장소 경로(예: org/model-name)입니다.',
  revision:
    '받을 모델 버전(브랜치·태그·커밋). 비어 있으면 기본 개정을 사용합니다. 개정이 바뀌면 이전 Fit 결과는 무효화됩니다.',
  sizeVram:
    '다운로드 크기(파일 용량 추정)와 예상 GPU 메모리(VRAM)입니다. 예상값이며 실제 여유 VRAM·안전 여유와 다를 수 있습니다.',
  fit: 'GPU 적합성(Fit) 참고 판정입니다. 카탈로그 Fit은 서버의 GPU를 고르지 않으며, VRAM을 GPU끼리 합산하지 않습니다. 최종 GPU 선택·Fit은 배포 설정(Deploy Wizard)에서 합니다.',
  reason: '목록 요약 또는 Fit 분석이 남긴 판정 근거입니다. 원문 문구를 유지합니다.',
  next: 'Fit 분석·다운로드·다음 단계 링크가 있는 작업 열입니다.',
  modelType:
    '다운로드 시 등록할 모델 유형입니다. 미확정이면 LLM/VLM/EMBEDDING을 직접 고른 뒤 다운로드하세요.',
  downloadReady:
    'READY는 대상 서버에 파일 캐시가 준비된 상태입니다. 모델이 실행 중이거나 Gateway API에 게시된 상태가 아닙니다.',
  downloadPolling:
    '다운로드가 진행 중이면 상태가 자동으로 갱신됩니다. 진행률(%)은 서버가 제공할 때만 표시하며, 임의로 만들지 않습니다. 상태 조회 실패와 다운로드 실패는 별도로 안내됩니다.',
  diskVsVram:
    '디스크 부족은 파일 저장 공간이 부족한 경우이고, GPU 메모리(VRAM) 부족은 가속 카드 여유 메모리가 부족한 경우입니다. 둘을 섞어 합산하지 않습니다.',
} as const

export const CACHE_FIELD_HELP = {
  overview:
    '이 화면은 서버에 받아 둔 모델 파일(캐시)만 관리합니다. 캐시 READY ≠ 모델 실행 중 ≠ API 게시 완료입니다. READY인 항목만 배포 설정으로 이어갈 수 있습니다.',
  status:
    '캐시 상태(READY/PREPARING/FAILED 등)는 로컬 파일 준비 상태입니다. Deployment 실행 상태나 Endpoint 연결과 다릅니다.',
  progress:
    '다운로드 진행률은 활성 Job이 있고 서버가 값을 줄 때만 표시됩니다. “마지막 상태 조회”는 Gateway/관리 API가 Job을 다시 읽은 시점이며, 진행률을 추측하지 않습니다.',
  deploy:
    '캐시가 READY일 때만 배포 설정으로 이동합니다. 아직 PREPARING/FAILED이면 배포할 수 없습니다.',
  purge:
    '로컬 캐시 삭제(Purge)는 해당 서버 Node Agent의 모델 파일만 지웁니다. 모델 레지스트리(Model/Version) 삭제나 실행 중 컨테이너 중지가 아닙니다. 사용 중인 Deployment가 있으면 삭제가 거부될 수 있습니다.',
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
