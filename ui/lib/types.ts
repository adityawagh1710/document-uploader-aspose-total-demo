// TypeScript mirrors of the Go orchestrator's wire contract (internal/types,
// internal/oerrors, internal/obs). These are MIRRORS, not a second source of
// truth: any field added to the Go contract is a contract change first, then
// mirrored here. See aidlc-docs/.../functional-design/domain-entities.md.

export type FailureClass =
  | 'unsupported_format'
  | 'missing_file'
  | 'input_too_large'
  | 'input_unprocessable'
  | 'render_failed'
  | 'subdivision_floor_exceeded'
  | 'merge_failed'
  | 'license_expired'
  | 'busy'
  | 'rate_limited'
  | 'engine_unavailable'
  | 'input_source_conflict'
  | 's3_disabled'
  | 's3_invalid_url'
  | 's3_input_not_found'
  | 's3_input_forbidden'
  | 's3_output_forbidden'
  | 's3_output_upload_failed';

export interface Diagnostic {
  request_id: string;
  failure_class: FailureClass;
  detail: Record<string, unknown>;
}

export type Engine = 'gotenberg' | 'aspose';

export interface ConversionRecord {
  request_id: string;
  completion_ts: number;
  source: 'ui' | 'cross';
  input_filename: string | null;
  format: string;
  page_count: number | null;
  duration_ms: number;
  status: 'success' | 'failed';
  error_code: string | null;
  output_s3_uri: string | null;
  output_size_bytes: number | null;
  engine?: Engine; // present on HTML conversions only
}

export interface ConversionsPage {
  entries: ConversionRecord[];
  next_cursor: string | null;
  has_more: boolean;
  stale_cursor: boolean;
  buffer_size: number;
}

export interface StatsTriple {
  count: number;
  avg_ms: number;
  p95_ms: number;
}

export interface ConversionsStats {
  per_format: Record<string, StatsTriple>;
  per_engine_html?: Partial<Record<Engine, StatsTriple>>;
  totals: { count: number; successes: number; failures: number };
}

export interface Health {
  ready: boolean;
  license_days_remaining: number | null;
  active_jobs: number;
  max_jobs: number;
  problems: string[];
}

export interface ContainerStats {
  cpu_usage_usec: number;
  mem_bytes: number;
  mem_max_bytes: number;
  pids_current: number;
  sampled_at: number;
  cgroup_version: string;
}

export interface WorkerProc {
  pid: number;
  cmdline: string;
  cpu_usage_usec: number;
  rss_bytes: number;
  etime_sec: number;
  sampled_at: number;
}

export interface Presigned {
  download_url: string;
  bucket: string;
  key: string;
  expires_in_seconds: number;
  expires_at: string;
}

// Client-side state of one engine run in the comparison panel.
export interface EngineRunResult {
  engine: Engine;
  ok: boolean;
  latencyMs: number;
  sizeBytes?: number;
  blob?: Blob;
  requestId: string;
  error?: Diagnostic | string;
}
