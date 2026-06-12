// Deterministic mock payloads for the Playwright E2E suite. Shapes mirror
// ui/lib/types.ts (the Go API wire contract). These let the mocked E2E run
// without the Go backend / Aspose license / Gotenberg — only the Next.js
// server is needed (CI-friendly). The live smoke (E2E_LIVE=1) bypasses these
// and hits the real stack.

import type {
  ConversionRecord,
  ConversionsPage,
  ConversionsStats,
  ContainerStats,
  Health,
  Presigned,
  WorkerProc,
} from '../lib/types';

export const health: Health = {
  ready: true,
  license_days_remaining: 23,
  active_jobs: 0,
  max_jobs: 2,
  problems: [],
};

// Not-ready: /health returns this with HTTP 503 (e.g. expired license). The UI
// must show NOT READY / EXPIRED — NOT "API DOWN" (which is reserved for a real
// network failure). Mirrors the live expired-license state.
export const expiredHealth: Health = {
  ready: false,
  license_days_remaining: -4,
  active_jobs: 0,
  max_jobs: 2,
  problems: ['license_expired'],
};

export const containerStats: ContainerStats = {
  cpu_usage_usec: 1_200_000,
  mem_bytes: 512 * 1024 * 1024,
  mem_max_bytes: 4 * 1024 * 1024 * 1024,
  pids_current: 12,
  sampled_at: 1_781_000_000,
  cgroup_version: 'v2',
};

export const workers: { workers: WorkerProc[] } = {
  workers: [
    {
      pid: 101,
      cmdline: 'office-convert-worker-docx',
      cpu_usage_usec: 50_000,
      rss_bytes: 80 * 1024 * 1024,
      etime_sec: 12,
      sampled_at: 1_781_000_000,
    },
  ],
};

const gotenbergRow: ConversionRecord = {
  request_id: 'req_gotenberg_1',
  completion_ts: 1_781_000_100,
  source: 'ui',
  input_filename: 'sample.html',
  format: 'html',
  page_count: null,
  duration_ms: 136,
  status: 'success',
  error_code: null,
  output_s3_uri: null,
  output_size_bytes: 27_688,
  engine: 'gotenberg',
};

const asposeFailRow: ConversionRecord = {
  request_id: 'req_aspose_1',
  completion_ts: 1_781_000_090,
  source: 'ui',
  input_filename: 'sample.html',
  format: 'html',
  page_count: null,
  duration_ms: 41,
  status: 'failed',
  error_code: 'license_expired',
  output_s3_uri: null,
  output_size_bytes: null,
  engine: 'aspose',
};

const docxRow: ConversionRecord = {
  request_id: 'req_docx_1',
  completion_ts: 1_781_000_050,
  source: 'cross',
  input_filename: 'report.docx',
  format: 'docx',
  page_count: 12,
  duration_ms: 2_400,
  status: 'success',
  error_code: null,
  output_s3_uri: 's3://office-convert-out/pdf/req_docx_1.pdf',
  output_size_bytes: 220_000,
  engine: undefined,
};

export const conversionsPage: ConversionsPage = {
  entries: [gotenbergRow, asposeFailRow, docxRow],
  next_cursor: 'cursor_page2',
  has_more: true,
  stale_cursor: false,
  buffer_size: 3,
};

// A page whose stale_cursor=true drives the HistoryPanel reset + amber note.
export const stalePage: ConversionsPage = {
  ...conversionsPage,
  stale_cursor: true,
};

export const conversionsStats: ConversionsStats = {
  per_format: { html: { count: 2, avg_ms: 2864, p95_ms: 5593 } },
  per_engine_html: {
    gotenberg: { count: 2, avg_ms: 2864, p95_ms: 5593 },
    aspose: { count: 1, avg_ms: 41, p95_ms: 41 },
  },
  totals: { count: 4, successes: 2, failures: 2 },
};

export const presigned: Presigned = {
  download_url: 'https://example.test/presigned?sig=abc',
  bucket: 'office-convert-out',
  key: 'pdf/req_docx_1.pdf',
  expires_in_seconds: 900,
  expires_at: '2026-06-12T21:00:00Z',
};

// Minimal valid-enough PDF bytes for the Gotenberg success blob.
export const fakePdf = '%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n';

// Aspose endpoint failure body (expired real license — matches live behavior).
export const licenseExpiredDiagnostic = {
  request_id: 'req_aspose_live',
  failure_class: 'license_expired',
  detail: { expired_on: null },
};
