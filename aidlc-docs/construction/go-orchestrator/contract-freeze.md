# Frozen HTTP Contract — Go Orchestrator Parity Oracle

**Status**: Phase 0 artifact. The live Python orchestrator is the parity oracle.
The Go re-implementation MUST reproduce every endpoint below byte-for-byte
(JSON shapes, status codes, headers). Captured 2026-06-02 from
`office_convert/server.py`.

## Endpoint inventory (14 routes)

| Method | Path | Response | Notes |
| ------ | ---- | -------- | ----- |
| GET    | `/health` | JSON | readiness, license_days_remaining, worker binary presence, max_jobs |
| GET    | `/` | HTML | landing page (served via `embed.FS` in Go) |
| POST   | `/v1/convert` | streaming PDF or error JSON | multipart upload OR S3 source; streams merged PDF; `X-*` result headers |
| GET    | `/v1/jobs/{request_id}/heartbeats` | JSON | per-pool heartbeat trail |
| GET    | `/v1/jobs/{request_id}/timings` | JSON | per-phase timing trail |
| GET    | `/v1/jobs/{request_id}/progress` | JSON | weighted % progress for one job |
| GET    | `/v1/jobs/active` | JSON | **cross-service fallback signal — UI depends on this** |
| GET    | `/v1/stats` | JSON | live counters |
| GET    | `/v1/workers` | JSON | worker pool status |
| GET    | `/v1/conversions` | JSON | recent ring buffer, cursor-paginated |
| GET    | `/v1/conversions/stats` | JSON | per-format aggregates from the ring buffer |
| GET    | `/v1/dashboard` | HTML | embedded dashboard (served via `embed.FS`) |
| DELETE | `/v1/cache` | JSON | cache purge |
| GET    | `/v1/downloads/presign` | JSON | S3 presigned URL |

## Load-bearing contracts that break the UI silently if violated

1. **`PUBLIC_API_URL` vs `API_URL`** — browser-facing iframe/links use the
   public URL; server-side calls use the internal URL. (UI concern, but Go
   must serve the dashboard/landing HTML with the same env-var substitution.)
2. **`/v1/jobs/active` + `/v1/conversions?limit=1` must stay populated for
   cross-service jobs** (classification-service conversions). See the
   `feedback-ui-vs-api-state` memory.
3. **`/v1/conversions` cursor** — base64-JSON cursor wire format must be
   identical so existing pagination keeps working.

## Canonical enums (from types.py — must match string values exactly)

### FailureClass (returned in error body `failure_class`)
`unsupported_format`, `missing_file`, `input_too_large`, `input_unprocessable`,
`render_failed`, `subdivision_floor_exceeded`, `merge_failed`, `license_expired`,
`busy`, `rate_limited`, `input_source_conflict`, `s3_disabled`, `s3_invalid_url`,
`s3_input_not_found`, `s3_input_forbidden`, `s3_output_forbidden`,
`s3_output_upload_failed`

### LicenseState
`permanent`, `healthy`, `warn`, `critical`, `expiring_today`, `expired`

### Error body shape (Diagnostic)
```json
{ "request_id": "...", "failure_class": "...", "detail": { } }
```

## Golden fixtures (TODO — Phase 0 completion)

Capture live JSON responses from a running Python instance into
`internal/server/testdata/golden/` for each GET endpoint, plus a sample
`/v1/convert` success-header set and one error body per `FailureClass`.
These drive the Phase 6 parity diff. Requires a running service +
representative inputs; deferred to the parity-testing phase setup.
