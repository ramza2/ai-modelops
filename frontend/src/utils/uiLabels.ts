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

/** P1-15 — Deploy Wizard / Models registry beginner help (display-only). */
export type WizardStepKey =
  | 'cache'
  | 'runtime'
  | 'gpu'
  | 'review'
  | 'start'
  | 'publish'
  | 'done'

export const WIZARD_STEP_LABELS: Record<WizardStepKey, string> = {
  cache: '1. 다운로드 모델 확인',
  runtime: '2. 모델 실행 설정',
  gpu: '3. GPU 선택 및 자원 검사',
  review: '4. 설정 검토 및 배포 정보 생성',
  start: '5. 모델 실행 및 상태 확인',
  publish: '6. API 연결 설정',
  done: '7. API 연결 결과 확인',
}

export const WIZARD_STEP_HINTS: Record<WizardStepKey, string> = {
  cache: '다운로드가 끝난 모델 파일을 확인한 뒤, 실행 설정으로 갑니다.',
  runtime: '배포·컨테이너·런타임 옵션을 입력합니다. 미설정은 런타임 기본 동작을 따릅니다.',
  gpu: '사용할 GPU를 고르고 Fresh Fit(현재 설정으로의 자원 검사)을 실행합니다. Fit은 모델을 실행하지 않습니다.',
  review: '설정을 검토한 뒤 배포 정보만 생성합니다. 아직 모델은 실행되지 않습니다.',
  start: '실제 모델 컨테이너 실행을 요청하고 Operation·Health 상태를 확인합니다.',
  publish: '업무 시스템이 호출할 API 별칭(Alias)을 연결합니다. 서비스 라우팅에 영향을 줄 수 있습니다.',
  done: 'API 연결·Gateway 검증 결과를 확인합니다.',
}

export const WIZARD_FIELD_HELP = {
  overview:
    '캐시 READY → Runtime 설정 → GPU·Fit → 배포 정보 생성 → 모델 실행 → API 별칭 연결 → 결과 확인 순서로 진행합니다. Create ≠ Start ≠ Publish입니다.',
  create:
    '배포 정보 생성(Create Deployment)은 Management API에 Deployment 메타데이터를 등록할 뿐이며, 모델 컨테이너를 시작하지 않습니다.',
  start:
    '모델 실행(Start Deployment)은 선택한 GPU에서 Managed 컨테이너를 기동하는 비동기 Operation을 요청합니다.',
  publish:
    'API 연결(Publish)은 Endpoint Alias를 ACTIVE Route로 이 Deployment에 연결합니다. 업무 시스템의 Gateway 호출 경로에 영향을 줄 수 있습니다.',
  verify:
    'API 연결 결과 확인은 Alias·Route·Gateway 검증 상태를 보여 줍니다. 모델 파일 다운로드나 Fit과는 별개입니다.',
  freshFit:
    'Fresh Fit은 지금 선택한 GPU와 Runtime 설정으로 Node Agent가 새로 측정한 자원 적합성 검사입니다. 모델을 실행하거나 GPU를 점유하지 않으며, VRAM을 GPU끼리 합산하지 않습니다.',
  tensorParallel:
    'GPU를 여러 장 고르면 Tensor Parallel(TP) 크기가 선택 수와 같아집니다. 이는 모델 샤딩이며, 여러 GPU의 여유 VRAM을 하나의 큰 메모리로 합치는 것과 다릅니다.',
  unknownAck:
    'UNKNOWN은 정보 부족으로 적합성을 단정하지 못한 상태입니다. 계속하려면 아래 동의 체크가 필요합니다.',
  alias:
    'API 별칭(Endpoint Alias)은 업무 시스템이 Gateway에 보내는 model 이름입니다. Served model name은 실제 런타임이 인식하는 모델 id이며, Publish 시 rewrite로 연결될 수 있습니다.',
  aliasModes:
    '신규 Alias는 새 별칭을 만들고 이 Deployment에 연결합니다. 기존 Alias는 ACTIVE Route가 없는 항목만 선택 가능합니다. ACTIVE Route가 있으면 이 단계에서 임의 전환할 수 없으며 Switch를 사용하세요.',
  registryList:
    '이 목록은 레지스트리에 등록된 모델 정의·버전을 조회합니다. 지금 서버에서 실행 중인 Deployment 목록과 다릅니다.',
} as const

export const RUNTIME_FIELD_HELP = {
  deploymentName: '관리 화면에 보이는 배포 이름입니다(Deployment name).',
  containerName:
    'Node에서 Managed 컨테이너에 붙는 이름입니다(Container name). 같은 Node 안에서 고유해야 합니다.',
  servedModelName:
    '런타임이 노출하는 모델 실행 이름입니다(Served model name). Publish 시 upstream rewrite 기본값으로도 쓰입니다.',
  runtimePort: '컨테이너가 listen하는 모델 실행 포트입니다(Runtime port, 1–65535).',
  maxModelLen:
    '최대 문맥 길이입니다(max_model_len). 비우면 런타임 기본값을 사용하며, 임의 기본값을 만들지 않습니다.',
  maxNumSeqs:
    '동시 처리 시퀀스 상한입니다(max_num_seqs). 스케줄러 용량이며 동시 사용자 수 보장이 아닙니다. 비우면 기본 동작입니다.',
  gpuMemoryUtilization:
    'GPU 메모리 사용 비율입니다(gpu_memory_utilization, 0 초과–1 이하). GPU별로 적용되며 합산하지 않습니다.',
  dtype: '연산 데이터 형식입니다(dtype). 선택 사항이며 추측으로 채우지 않습니다.',
  quantization:
    '모델 양자화 방식입니다(quantization). 선택 사항이며 확인되지 않은 값을 자동 선택하지 않습니다.',
  runner:
    '생성/임베딩 실행 모드입니다(runner: generate / pooling). 아래 vLLM Model Runner(Auto/V1)와는 다른 설정입니다.',
  reasoningParser:
    '추론 내용 분리 설정입니다(Reasoning Parser, --reasoning-parser). 요청별 enable_thinking과 별개입니다.',
  autoToolChoice:
    '자동 도구 선택입니다(Auto Tool Choice). ON일 때만 --enable-auto-tool-choice와 parser를 추가합니다.',
  toolCallParser:
    '도구 호출 분석 방식입니다(Tool Call Parser). Auto Tool Choice ON일 때 필요합니다.',
  vllmModelRunner:
    '엔진 실행 방식입니다(vLLM Model Runner: Auto/V1). runner(generate/pooling)와 다릅니다.',
  unsetKeepsDefault:
    '미설정 필드는 실제 Runtime 기본 동작을 따릅니다. 임의의 기본값이나 모델별 설정을 자동 생성하지 않습니다.',
} as const

/** P1-19 — Nodes / GPUs beginner help (display-only). */
export const NODE_FIELD_HELP = {
  overview:
    '서버(Node)는 Node Agent가 설치된 GPU 호스트입니다. 목록의 상태는 Management API DB에 저장된 값이며, 새로고침은 DB 재조회만 수행합니다(Node Agent 호출 아님).',
  status:
    'ONLINE=Node Agent 연결 정상, DEGRADED=연결은 있으나 일부 저하, OFFLINE=연결 끊김, UNKNOWN=상태를 알 수 없음. ONLINE ≠ GPU 모델이 정상 실행 중.',
  heartbeat:
    '마지막 연결 확인(Last Heartbeat)은 Node Agent 생존 신호 시각입니다. Host/GPU 자원 측정 시각(Snapshot Sampled At)과 다르며, 오래된 스냅샷을 실시간 상태로 보지 마세요.',
  refreshVsCollect:
    '상단 새로고침=DB에 저장된 메타/스냅샷 재조회. 리소스 갱신=Node Agent로 새 Host/GPU 스냅샷 수집 요청. 수집 중에는 중복 요청이 차단됩니다.',
  ramVsVram:
    '시스템 메모리(RAM)는 서버 일반 메모리이고, GPU 메모리(VRAM)는 그래픽카드 전용 메모리입니다. 서로 다른 자원입니다.',
  gpuUtilVsVram:
    'GPU 연산 사용률(GPU Utilization)과 VRAM 사용량/여유는 다른 지표입니다. Memory Utilization은 GPU 메모리 컨트롤러 사용률이며 VRAM Used/Free와 동일 개념이 아닙니다.',
  vramFree:
    '남은 GPU 메모리(VRAM Free)가 모두 새 모델에 할당 가능하다는 뜻이 아닙니다. Safety Margin(GPU 안전 여유)과 다른 프로세스 점유를 고려해야 합니다.',
  safetyMargin:
    'GPU 안전 여유(Safety Margin)는 안정 실행을 위해 남겨 두는 VRAM 여유입니다. Fit/배포 판정에 반영됩니다.',
  noPoolVram:
    'GPU가 여러 장이어도 각 GPU의 VRAM을 하나의 큰 메모리로 합산하지 않습니다. 적합성은 GPU별로 판단합니다.',
  fitReminder:
    '현재 사용률이 낮다고 새 모델 배포 성공이 보장되지 않습니다. 최종 자원 적합성은 배포 Wizard의 Fresh Fit으로 확인하세요.',
  uncollected:
    '스냅샷이 없으면 미수집입니다. 0으로 대체하지 않으며, 리소스 갱신 후 값이 채워질 수 있습니다.',
} as const

/** P1-16 — Endpoints / Operations / Observability beginner help (display-only). */
export const ENDPOINT_FIELD_HELP = {
  overview:
    '이 화면은 업무 시스템이 Gateway로 호출하는 API 별칭(Endpoint Alias)과 현재 활성 연결(ACTIVE Route)을 관리합니다. Alias 등록 ≠ 요청 처리 가능 ≠ 모델 실행 중입니다.',
  alias:
    'API 별칭(Endpoint Alias)은 Gateway 요청의 model로 쓰는 이름입니다. Served Model Name은 실제 런타임이 인식하는 모델 id이며, rewrite로 연결될 수 있습니다.',
  enabled:
    '사용 설정(Enabled)은 Alias가 관리 대상으로 켜져 있는지를 나타냅니다. 실제 요청 처리는 Traffic 상태·ACTIVE Route·Deployment 실행/Health에 따릅니다.',
  traffic:
    '요청 처리 상태(Traffic): SERVING=정상 전달, DRAINING=신규 요청 차단·진행 중 요청 대기, MAINTENANCE=신규 요청 차단·upstream 전달 금지.',
  trafficDisplay:
    '사용 설정·ACTIVE Route·Traffic 설정값은 서로 다릅니다. 사용 중지(is_enabled=false)이거나 모델 미연결이면 Traffic 원본이 SERVING이어도 녹색 서빙 중으로 보이지 않습니다. 원본 traffic_state는 유지됩니다.',
  trafficServingOk:
    '사용 설정 ON + ACTIVE Route가 있을 때 Traffic SERVING은 Gateway가 요청을 넘기려는 설정입니다. 실제 처리 가능 여부는 Deployment Runtime/Health에 따릅니다.',
  activeRoute:
    '현재 활성 연결(ACTIVE Route)은 지금 Gateway가 이 Alias로 보내는 Deployment입니다. Deployment가 STOPPED/UNHEALTHY여도 Route 행 자체는 남을 수 있습니다.',
  preflight:
    'Preflight Preview는 참고용 사전 검사입니다(실제 Switch 실행 아님). 실행을 enqueue하면 Worker가 자원 상태를 다시 검증합니다. GPU VRAM은 GPU별로만 판단하며 합산하지 않습니다.',
  hotSwitch:
    'HOT Switch는 가능한 경우 짧은 전환을 시도하지만, 무중단·무영향 전환을 무조건 보장하지 않습니다. Preview 결과와 실제 Worker 판정이 다를 수 있습니다.',
  coldSwitch:
    'COLD Switch는 Source 중지·VRAM 해제·Target 기동 등이 포함될 수 있어 서비스 영향(중단·지연) 가능성이 있습니다. 운영 영향 범위를 확인한 뒤 진행하세요.',
  unpublish:
    '연결 해제(Unpublish)는 Gateway Alias의 ACTIVE Route만 제거합니다. 모델 컨테이너 중지·캐시 삭제·레지스트리 삭제와 다릅니다. Alias 재입력 확인이 필요합니다.',
  disable:
    '사용 중지(is_enabled=false)는 Alias를 Gateway 호출 대상에서 끕니다. ACTIVE Route·Deployment·캐시·레지스트리·Route/Operation 이력은 삭제하지 않습니다. Unpublish(연결 해제) · Deployment Stop · Cache Purge와 다릅니다.',
  enable:
    '다시 사용(재활성화, is_enabled=true)은 Alias 사용 설정만 켭니다. ACTIVE Route가 없으면 요청을 처리할 수 없으며, Route를 자동으로 만들거나 Deployment를 시작하지 않습니다.',
  disableVsOthers:
    '사용 중지 ≠ 연결 해제(Unpublish) ≠ 모델 중지(Deployment Stop) ≠ 캐시 삭제(Cache Purge). 사용 중지는 별칭 on/off이며 이력·모델 파일은 유지됩니다.',
  gatewayAfterEnabledChange:
    '사용 설정 변경 시 routing_state.version이 증가합니다. Gateway는 주기적으로 라우팅 스냅샷을 갱신하며, 반영 전까지 Last Known Good를 쓸 수 있습니다. 화면을 새로고침해 최신 is_enabled를 확인하세요.',
  unusedAlias:
    '활성 연결 없음: ACTIVE Route가 없습니다. Enabled여도 Gateway가 모델을 연결할 수 없는 미사용(또는 미연결) 별칭일 수 있습니다. 과거 INACTIVE Route 이력은 별도로 남습니다.',
  connectedAlias:
    '모델 연결됨: ACTIVE Route가 Deployment를 가리킵니다. 사용 중지 시 이 Alias를 호출하는 업무 시스템 요청이 실패할 수 있습니다.',
} as const

export const OPERATION_FIELD_HELP = {
  overview:
    '작업 이력(Operation)은 배포·전환 등 비동기 작업의 진행과 결과를 추적합니다. 목록의 상태는 API 원본 코드를 유지하며, 상세에서 Safe Cancel / Explicit Retry를 제공합니다.',
  status:
    '진행 상태 예: QUEUED=대기, RUNNING=실행 중, SUCCEEDED=성공, FAILED=실패, CANCELLED/CANCELED=취소됨, ROLLING_BACK=롤백 중, ROLLED_BACK=롤백 완료, MANUAL_INTERVENTION_REQUIRED=수동 개입 필요.',
  type: '작업 유형(Type)은 SWITCH·DEPLOYMENT_START 등 Operation이 수행하는 작업 종류입니다.',
  strategy: '전환 방식(Strategy)은 HOT/COLD 등 Switch 전략입니다. 목록·상세의 원본 코드를 확인하세요.',
  sourceTarget:
    '기존 대상(Source)은 전환 전 Deployment, 변경 대상(Target)은 전환 후 Deployment입니다.',
  safeCancel:
    'Safe Cancel은 지원되는 Switch 상태에서 취소 의도를 보냅니다. 즉시 작업 중단을 보장하지 않으며, Source 중지 이후에는 단순 취소가 아니라 rollback intent로 처리될 수 있습니다.',
  explicitRetry:
    'Explicit Retry는 새 Operation을 생성합니다(기존 Operation을 덮어쓰지 않음). Backend가 상태·중복을 다시 검증하며, UI gate만으로 재시도 가능을 단정하지 마세요.',
} as const

export const OBSERVABILITY_FIELD_HELP = {
  overview:
    '운영 모니터링은 DB에 저장된 집계·스냅샷을 보여 줍니다. 실시간 scrape/측정 화면이 아니며, 최근 샘플 시각과 화면 새로고침 시각을 구분하세요.',
  invocations:
    'AI 요청 사용 현황(Invocation Summary)은 선택한 구간의 Gateway 호출 집계입니다. Token coverage는 토큰이 기록된 행의 비율이며 전체 요청이 모두 토큰화되었다고 보지 마세요.',
  tokens:
    '입력/출력 토큰 percentile·평균은 토큰이 있는 행만 사용합니다. 누적 카운터와 구간별 사용량을 혼동하지 마세요.',
  latency:
    'Latency p95 등은 응답시간 분포 요약입니다. Runtime analytics의 P50/P95는 classic histogram bucket 추정값이며 exact request percentile이 아닙니다.',
  runtimeLatest:
    '최근 모델 실행 상태(Runtime Latest)는 저장된 마지막 metric snapshot입니다. 샘플 시각이 2분을 넘으면 STALE로 표시합니다(실시간 Health와 다름).',
  capacity:
    '모델 자원 사용 정보(Capacity Profile)는 요청 설정과 관측값·analytics를 비교합니다. 런타임 기본값을 합성하지 않으며 GPU VRAM은 GPU별로만 봅니다.',
  runningWaiting:
    '처리 중(Running)·대기 중(Waiting)은 런타임 스케줄러 게이지입니다. 요청 성공률이나 Deployment HEALTHY와 같은 의미가 아닙니다.',
  missingVsZero:
    '측정값 없음(—/null)과 실제 0은 다릅니다. 없는 값을 0으로 채우지 않습니다.',
  gpuVram:
    'GPU별 VRAM을 여러 장의 합산 자원처럼 해석하지 마세요. 카드마다 독립적으로 봅니다.',
} as const

/** P1-17 — Model / Model Version detail beginner help (display-only). */
export const MODEL_DETAIL_FIELD_HELP = {
  overview:
    '이 화면은 레지스트리에 등록된 모델 정의와 버전 이력입니다. 모델 등록 상태(Active) ≠ 실제 배포·실행 중(Deployment) ≠ API 연결(Endpoint)입니다.',
  modelVsVersion:
    '모델(Model)은 논리 단위이고, 모델 버전(Model Version)은 실행·다운로드에 쓰이는 구체 스냅샷입니다. 버전마다 Runtime·예상 자원·Artifact가 다를 수 있습니다.',
  registration:
    '등록 상태(Active)는 레지스트리에서 이 모델이 관리 대상으로 켜져 있는지를 나타냅니다. 서버에서 컨테이너가 떠 있거나 Gateway로 요청이 가는 상태가 아닙니다.',
  archived:
    '버전 보관(Archived)은 이력에서 기본 목록에 덜 보이게 하는 표시입니다. 모델 삭제·Runtime 중지·캐시 삭제를 의미하지 않으며, 이미 실행 중인 Deployment와도 별개입니다.',
  expectedResources:
    '예상 Peak VRAM·문맥 길이 등은 Version에 저장된 예상 정의값입니다. 실제 GPU 측정값·현재 가용량·Preflight 판정이 아닙니다.',
  versions:
    '모델 버전 이력입니다. 「보관된 버전 포함」을 켜면 보관된 버전도 함께 볼 수 있습니다. 필터·페이지는 URL에 유지됩니다.',
} as const

export const MODEL_VERSION_DETAIL_FIELD_HELP = {
  overview:
    '이 화면은 Model Version에 저장된 Registry 정의값입니다. Deployment override·실제 컨테이너 observed 값과 다를 수 있으며, 관측 설정은 Deployment Capacity Profile에서 확인합니다.',
  runtimeConfig:
    'Version runtime_config는 레지스트리 정의값입니다. Deployment의 deployment_config가 일부를 덮어쓸 수 있고, 실행 중 관측값과도 다를 수 있습니다.',
  runtimeConfigEmpty:
    '레지스트리에 별도 실행 설정이 등록되지 않았습니다. Hugging Face 자동 등록 시 runtime_config가 비어 있을 수 있으며, 이는 다운로드·배포 오류가 아닙니다. 실제 실행·관측 설정은 Deployment Capacity Profile에서 확인하세요.',
  expectedResources:
    '예상 Idle/Peak VRAM·기본 문맥 길이는 예상 정의값입니다. 실제 사용량이나 현재 GPU 가용량을 보장하지 않습니다.',
  expectedResourcesUnset:
    '미등록은 Registry에 예상 자원값이 저장되지 않았다는 뜻입니다(null). Hugging Face 자동 등록 시 예상 VRAM·문맥 길이가 비어 있을 수 있으며, 다운로드·배포 오류가 아닙니다. 0은 유효한 숫자로 표시합니다. 실행 중인 Deployment 값으로 Registry를 자동 보정하지 않습니다.',
  artifacts:
    'Artifact는 레지스트리에 등록된 모델 파일 메타데이터입니다. 서버 Node의 다운로드 캐시 READY 상태와는 별개입니다.',
  archived:
    '버전이 Archived여도 이미 연결된 Deployment가 실행 중일 수 있습니다. 보관 표시 ≠ 실행 중지입니다.',
  identifiers:
    'SHA·Digest·Checksum·URI·Served Model Name 등 원본 식별값은 번역하거나 변형하지 않습니다.',
  parentModel:
    '부모 모델 링크는 Version이 속한 Model입니다. 부모 모델 조회 실패와 Version 자체 조회 실패는 별도로 안내합니다.',
} as const

/** P1-18 — Dashboard beginner help (display-only). */
export const DASHBOARD_FIELD_HELP = {
  overview:
    '대시보드는 관리 시스템·배포·API 연결·작업·최근 24시간 호출 집계를 한눈에 보여 줍니다. 읽기 전용이며 실시간 scrape가 아닙니다.',
  healthVsReady:
    'Health는 Management API 프로세스 생존(응답 가능)이고, Ready는 의존성(DB 등)까지 준비되어 요청을 받을 수 있는 상태입니다. Health 정상 ≠ Ready 준비됨.',
  deploymentStates:
    '활성 모델 배포(Active)=퇴역하지 않은 Deployment 수, 실행 중(Running)=runtime_status=RUNNING, 정상(Healthy)=health_status=HEALTHY입니다. 모델 레지스트리 등록과 다릅니다.',
  endpointTraffic:
    '사용 설정(Enabled)은 Alias가 켜져 있는지이고, SERVING은 요청 처리 상태입니다. Enabled여도 Traffic이 DRAINING/MAINTENANCE이면 신규 요청이 차단될 수 있습니다.',
  invocations:
    '최근 24시간 요청·오류·성공률은 Gateway Invocation 집계입니다. 요청 성공률 ≠ Runtime HEALTHY입니다. Token coverage는 토큰이 기록된 요청 비율입니다.',
  p95:
    'P95 Latency·입력 토큰은 Deployment 단위 95백분위입니다(전역 P95가 아님). 상위 표는 요청 수 기준 상위 Deployment입니다.',
  missingCounts:
    '0=실제로 해당 건수가 없음, 미수집=값을 받지 못함(null), 확인 불가=해당 구역 API 오류입니다.',
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
