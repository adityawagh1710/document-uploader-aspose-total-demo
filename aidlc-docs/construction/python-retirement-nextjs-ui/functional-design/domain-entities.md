# Domain Entities — unit `python-retirement-nextjs-ui`

No backend entity changes (the Go wire contract is untouched). The "entities" here are the
TypeScript mirrors of existing wire shapes, plus the env/config surface.

## TypeScript wire types (`ui/lib/types.ts`)

```ts
export type FailureClass =
  | 'unsupported_format' | 'missing_file' | 'input_too_large' | 'input_unprocessable'
  | 'render_failed' | 'subdivision_floor_exceeded' | 'merge_failed' | 'license_expired'
  | 'busy' | 'rate_limited' | 'engine_unavailable'
  | 'input_source_conflict' | 's3_disabled' | 's3_invalid_url' | 's3_input_not_found'
  | 's3_input_forbidden' | 's3_output_forbidden' | 's3_output_upload_failed';

export interface Diagnostic { request_id: string; failure_class: FailureClass; detail: Record<string, unknown>; }

export interface ConversionRecord {
  request_id: string; completion_ts: number; source: 'ui' | 'cross';
  input_filename: string | null; format: string; page_count: number | null;
  duration_ms: number; status: 'success' | 'failed'; error_code: string | null;
  output_s3_uri: string | null; output_size_bytes: number | null;
  engine?: 'gotenberg' | 'aspose';            // present on HTML conversions only
}

export interface ConversionsPage {
  entries: ConversionRecord[]; next_cursor: string | null;
  has_more: boolean; stale_cursor: boolean; buffer_size: number;
}

export interface StatsTriple { count: number; avg_ms: number; p95_ms: number; }
export interface ConversionsStats {
  per_format: Record<string, StatsTriple>;
  per_engine_html?: Record<'gotenberg' | 'aspose', StatsTriple>;
  totals: { count: number; successes: number; failures: number };
}

export interface Health {
  ready: boolean; license_days_remaining: number | null;
  active_jobs: number; max_jobs: number; problems: string[];
}

export interface ContainerStats { cpu_usage_usec: number; mem_bytes: number; mem_max_bytes: number; pids_current: number; sampled_at: number; cgroup_version: string; }
export interface WorkerProc { pid: number; cmdline: string; cpu_usage_usec: number; rss_bytes: number; etime_sec: number; sampled_at: number; }
export interface Presigned { download_url: string; bucket: string; key: string; expires_in_seconds: number; expires_at: string; }

export interface EngineRunResult {                 // client-side comparison card state
  engine: 'gotenberg' | 'aspose'; ok: boolean; latencyMs: number;
  sizeBytes?: number; blob?: Blob; requestId: string; error?: Diagnostic | string;
}
```

These are mirrors, not a second source of truth: any field added to the Go wire contract is a
contract change first (NFR-4), then mirrored here.

## Env/config surface

| Var | Consumer | Default | Purpose |
|---|---|---|---|
| `API_URL` | Next server (rewrites destination) | `http://office-convert:8080` | Go API target (server-side only) |
| `NEXT_PUBLIC_DASHBOARD_URL` | browser | `http://localhost:8080/v1/dashboard` | iframe src (replaces PUBLIC_API_URL usage) |
| `NEXT_PUBLIC_S3_ENABLED` | browser | `false` | show the S3-output checkbox |
| `PORT` | Next server | `3000` | container port (host-mapped to 8501, D6) |

## Deleted entities
All Python types (`office_convert/types.py` etc.) and the Streamlit session-state shapes go
with the sweep — their authoritative counterparts already live in `internal/types` (Go).
