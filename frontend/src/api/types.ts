export type ModelSummary = {
  id: string
  slug: string
  name: string
  model_type: string
  provider: string | null
  source_type: string
  license_name: string | null
  description: string | null
  is_active: boolean
  created_at: string
  updated_at: string
}

export type ModelDetail = ModelSummary

export type ModelVersion = {
  id: string
  model_id: string
  version_label: string
  source_repository: string | null
  source_revision: string | null
  quantization: string | null
  dtype: string | null
  runtime_type: string
  runtime_image: string
  runtime_image_digest: string | null
  served_model_name: string
  expected_idle_vram_mb: number | null
  expected_peak_vram_mb: number | null
  default_max_model_len: number | null
  runtime_config: Record<string, unknown> | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

export type ModelArtifact = {
  id: string
  model_version_id: string
  artifact_type: string
  source_uri: string
  revision: string | null
  checksum: string | null
  size_bytes: number | null
  created_at: string
}

export type Paginated<T> = {
  items: T[]
  page: number
  page_size: number
  total: number
}

export type NodeSummary = {
  id: string
  name: string
  hostname: string
  agent_base_url: string
  environment: string
  region: string | null
  status: string
  last_heartbeat_at: string | null
  cpu_model: string | null
  ram_total_mb: number | null
  disk_total_mb: number | null
  labels_json: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export type GPUDevice = {
  id: string
  node_id: string
  gpu_uuid: string
  device_index: number
  model_name: string
  vram_total_mb: number
  compute_capability: string | null
  safety_margin_mb: number
  status: string
  last_seen_at: string | null
  created_at: string
  updated_at: string
}

export type NodeDetail = NodeSummary & {
  gpus: GPUDevice[]
}

export type HostResourceSnapshot = {
  sampled_at: string
  cpu_utilization_pct: number | null
  ram_total_mb: number | null
  ram_used_mb: number | null
  ram_free_mb: number | null
  disk_total_mb: number | null
  disk_used_mb: number | null
  disk_free_mb: number | null
}

export type GPUResourceSnapshot = {
  sampled_at: string
  vram_total_mb: number | null
  vram_used_mb: number | null
  vram_free_mb: number | null
  gpu_utilization_pct: number | null
  memory_utilization_pct: number | null
  temperature_c: number | null
  power_w: number | null
}

export type NodeGpuResource = {
  gpu: GPUDevice
  snapshot: GPUResourceSnapshot | null
}

export type NodeResourcesLatest = {
  node_id: string
  host: HostResourceSnapshot | null
  gpus: NodeGpuResource[]
}

export type DeploymentGpuAssignment = {
  gpu_device_id: string
  device_order: number
  expected_vram_mb: number | null
  created_at: string
}

/** Full Deployment payload from Management API list/detail. */
export type Deployment = {
  id: string
  name: string
  model_version_id: string
  node_id: string | null
  deployment_type: string
  desired_state: string
  runtime_status: string
  health_status: string
  container_id: string | null
  container_name: string | null
  upstream_base_url: string | null
  runtime_port: number | null
  deployment_config: Record<string, unknown> | null
  gpu_assignments: DeploymentGpuAssignment[]
  last_started_at: string | null
  last_stopped_at: string | null
  last_health_at: string | null
  status_reason: string | null
  created_at: string
  updated_at: string
  retired_at: string | null
}

/** Dashboard subset; list responses are full Deployment objects. */
export type DeploymentSummary = {
  id: string
  name: string
  runtime_status: string
  health_status: string
}

/** Lifecycle enqueue response (202 Accepted). */
export type LifecycleOperation = {
  id: string
  operation_type: string
  status: string
  target_deployment_id: string | null
  current_step: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: { code: string | null; message: string | null } | null
}

export type EndpointActiveRouteDeployment = {
  id: string
  name: string
  runtime_status: string
  health_status: string
  upstream_base_url: string | null
  retired_at: string | null
}

export type EndpointActiveRoute = {
  route_id: string
  deployment_id: string
  rewrite_model_name: string | null
  activated_at: string | null
  deployment: EndpointActiveRouteDeployment | null
}

/** Full Endpoint Alias payload from Management API list/detail. */
export type Endpoint = {
  id: string
  alias: string
  display_name: string
  api_type: string
  description: string | null
  is_enabled: boolean
  traffic_state: string
  created_at: string
  updated_at: string
  active_route?: EndpointActiveRoute | null
}

export type EndpointRoute = {
  id: string
  endpoint_alias_id: string
  deployment_id: string
  status: string
  rewrite_model_name: string | null
  operation_id: string | null
  activated_at: string | null
  deactivated_at: string | null
  created_at: string
}

export type PreflightGpuResult = {
  gpu_device_id: string
  free_vram_mb: number | null
  reclaimable_vram_mb: number | null
  required_vram_mb: number | null
  available_hot_vram_mb: number | null
  available_after_reclaim_mb: number | null
  effective_available_mb: number | null
  result: string
  safety_margin_mb: number | null
}

export type PreflightPreview = {
  id: string
  operation_id: string | null
  endpoint_id: string | null
  node_id: string
  target_model_version_id: string
  source_deployment_id: string | null
  target_deployment_id: string | null
  result: string
  required_peak_vram_mb: number | null
  available_hot_vram_mb: number | null
  reclaimable_vram_mb: number | null
  available_after_reclaim_mb: number | null
  safety_margin_mb: number | null
  gpu_results: PreflightGpuResult[]
  evaluated_at: string | null
  preview_only: boolean
  worker_must_revalidate: boolean
}

export type SwitchOperation = {
  id: string
  operation_id: string
  operation_type: string
  switch_strategy: string | null
  status: string
  endpoint_alias_id: string | null
  source_deployment_id: string | null
  target_deployment_id: string | null
  current_step: string | null
  created_at: string
  error: { code: string | null; message: string | null } | null
}

/** Dashboard subset. */
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

export type OperationStep = {
  id: string
  sequence_no: number
  step_code: string
  status: string
  attempt_no: number
  started_at: string | null
  finished_at: string | null
  error: { code: string | null; message: string | null } | null
  created_at: string
}

/** Full Operation detail from GET /operations/{id} (includes ordered steps). */
export type OperationDetail = {
  id: string
  operation_type: string
  status: string
  switch_strategy: string | null
  endpoint_alias_id: string | null
  source_deployment_id: string | null
  target_deployment_id: string | null
  current_step: string | null
  cancel_requested_at: string | null
  retry_of_operation_id: string | null
  requested_by: string | null
  request_reason: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: { code: string | null; message: string | null } | null
  steps: OperationStep[]
}

/** Retry response creates a NEW Operation (may include steps). */
export type RetryOperationResponse = {
  id: string
  operation_id?: string
  operation_type: string
  status: string
  switch_strategy: string | null
  endpoint_alias_id: string | null
  source_deployment_id: string | null
  target_deployment_id: string | null
  retry_of_operation_id: string | null
  current_step: string | null
  created_at: string
  error: { code: string | null; message: string | null } | null
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
