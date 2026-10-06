export type Paginated<T> = {
  items: T[]
  page: number
  page_size: number
  total: number
}

export type NodeSummary = {
  id: string
  name: string
  status: string
}

export type DeploymentSummary = {
  id: string
  name: string
  runtime_status: string
  health_status: string
}

export type EndpointSummary = {
  id: string
  alias: string
  is_enabled: boolean
  traffic_state: string
}

export type OperationSummary = {
  id: string
  operation_type: string
  status: string
  switch_strategy: string | null
  endpoint_alias_id: string | null
  source_deployment_id: string | null
  target_deployment_id: string | null
  requested_by: string | null
  request_reason: string | null
  error_code: string | null
  error_message: string | null
  cancel_requested_at: string | null
  retry_of_operation_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type InvocationSummaryItem = {
  group_key: string
  deployment_name?: string | null
  request_count: number
  success_count: number
  error_count: number
  tokenized_request_count: number
  latency_ms_p95?: number | null
  input_tokens_p95?: number | null
}

export type InvocationSummaryResponse = {
  hours: number
  group_by: string
  items: InvocationSummaryItem[]
}

export type HealthResponse = {
  status?: string
  [key: string]: unknown
}

export type ReadyResponse = {
  status?: string
  [key: string]: unknown
}

export type ModelOpsErrorBody = {
  error?: {
    message?: string
    type?: string
    param?: string | null
    code?: string
  }
}
