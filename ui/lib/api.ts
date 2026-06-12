// Typed fetch helpers. Every browser call goes through the /api/* proxy
// (next.config rewrites → Go API) — BR-UI-1 single origin. Non-200 responses
// are parsed into the Go Diagnostic envelope where possible.

import type {
  ConversionsPage,
  ConversionsStats,
  ContainerStats,
  Diagnostic,
  Engine,
  EngineRunResult,
  Health,
  Presigned,
  WorkerProc,
} from './types';

const API = '/api';

// BR-UI-3 client-side mirrors of server caps (server stays authoritative).
export const HTML_MAX_BYTES = 10 * 1024 * 1024; // OFFICE_CONVERT_HTML_MAX_BYTES default
export const WAIT_DELAY_PATTERN = /^([0-9]+(\.[0-9]+)?)(ms|s)$/;
export const WAIT_DELAY_MAX_SECONDS = 30;

export class ApiError extends Error {
  readonly diagnostic: Diagnostic | null;
  readonly status: number;

  constructor(status: number, diagnostic: Diagnostic | null, fallback: string) {
    super(diagnostic ? diagnostic.failure_class : fallback);
    this.status = status;
    this.diagnostic = diagnostic;
  }
}

async function parseDiagnostic(res: Response): Promise<Diagnostic | null> {
  try {
    const body: unknown = await res.json();
    if (
      typeof body === 'object' &&
      body !== null &&
      'failure_class' in body &&
      'request_id' in body
    ) {
      return body as Diagnostic;
    }
  } catch {
    // non-JSON error body — fall through
  }
  return null;
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: 'no-store' });
  if (!res.ok) {
    throw new ApiError(res.status, await parseDiagnostic(res), `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

// SWR fetcher — thrown ApiError surfaces in the hook's `error` slot.
export const fetcher = <T>(path: string): Promise<T> => getJSON<T>(path);

export const getHealth = () => getJSON<Health>('/health');
export const getContainerStats = () => getJSON<ContainerStats>('/v1/stats');
export const getWorkers = () => getJSON<{ workers: WorkerProc[] }>('/v1/workers');
export const getConversionsStats = () => getJSON<ConversionsStats>('/v1/conversions/stats');

export function conversionsPath(cursor: string | null, limit: number, filter: string): string {
  const q = new URLSearchParams({ limit: String(limit), filter });
  if (cursor) q.set('cursor', cursor);
  return `/v1/conversions?${q.toString()}`;
}

export const getConversions = (cursor: string | null, limit = 20, filter = 'all') =>
  getJSON<ConversionsPage>(conversionsPath(cursor, limit, filter));

export const presign = (bucket: string, key: string) =>
  getJSON<Presigned>(
    `/v1/downloads/presign?bucket=${encodeURIComponent(bucket)}&key=${encodeURIComponent(key)}`,
  );

export interface ConvertOptions {
  s3Output?: string; // bucket or bucket/prefix — server-side semantics
}

export interface ConvertSuccess {
  blob: Blob;
  requestId: string;
  s3Bucket: string | null;
  s3Key: string | null;
}

export async function convertFile(file: File, opts: ConvertOptions = {}): Promise<ConvertSuccess> {
  const form = new FormData();
  form.append('file', file, file.name);
  if (opts.s3Output) form.append('s3_output', opts.s3Output);
  const res = await fetch(`${API}/v1/convert`, { method: 'POST', body: form });
  if (!res.ok) {
    throw new ApiError(res.status, await parseDiagnostic(res), `HTTP ${res.status}`);
  }
  return {
    blob: await res.blob(),
    requestId: res.headers.get('X-Request-ID') ?? '',
    s3Bucket: res.headers.get('X-S3-Output-Bucket'),
    s3Key: res.headers.get('X-S3-Output-Key'),
  };
}

export interface WaitFields {
  waitDelay?: string;
  waitForExpression?: string;
}

// One engine run for the comparison panel. Never throws — failures land in
// the result object so Promise.allSettled-style independence holds even for
// network-level errors (BR-UI: one engine failing never hides the other).
export async function convertHTML(
  engine: Engine,
  file: File,
  wait: WaitFields,
): Promise<EngineRunResult> {
  const form = new FormData();
  form.append('file', file, file.name);
  // D4: wait fields are Gotenberg-only; the server 422s them on aspose.
  if (engine === 'gotenberg') {
    if (wait.waitDelay) form.append('waitDelay', wait.waitDelay);
    if (wait.waitForExpression) form.append('waitForExpression', wait.waitForExpression);
  }
  const started = performance.now();
  try {
    const res = await fetch(`${API}/v1/convert/html/${engine}`, { method: 'POST', body: form });
    const latencyMs = performance.now() - started;
    const requestId = res.headers.get('X-Request-ID') ?? '';
    if (!res.ok) {
      const diag = await parseDiagnostic(res);
      return { engine, ok: false, latencyMs, requestId, error: diag ?? `HTTP ${res.status}` };
    }
    const blob = await res.blob();
    return { engine, ok: true, latencyMs, sizeBytes: blob.size, blob, requestId };
  } catch (err) {
    return {
      engine,
      ok: false,
      latencyMs: performance.now() - started,
      requestId: '',
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
