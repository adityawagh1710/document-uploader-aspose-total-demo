# Business Logic Model — unit `html-conversion`

**Scope**: Go orchestrator only. Two engine-specific HTML→PDF flows, both using the bypass
pattern (no probe→plan→render→merge pipeline).

## Flow A — `POST /v1/convert/html/gotenberg`

```
1. Shared pre-processing (see below) → validated HTML bytes on scratch disk
2. Parse optional wait fields: waitDelay (≤30s), waitForExpression (≤1024 chars)
3. Build multipart request to Gotenberg:
     POST {GotenbergURL}/forms/chromium/convert/html
     files          = HTML bytes, filename FIXED as "index.html"  (Chromium contract)
     paperWidth     = 8.5   paperHeight = 11        (inches, Letter — BR-7)
     marginTop/Bottom/Left/Right = 0.5              (inches)
     waitDelay        (only if provided)
     waitForExpression (only if provided)
4. Execute with bounded client timeout (default 120s)
     transport/connect error or connect timeout → EngineUnavailable (503)
     HTTP 2xx → response body is the PDF
     HTTP 4xx → InputUnprocessable (422)          [Chromium rejected the page]
     HTTP 5xx → RenderFailed (500)
5. Validate first 5 bytes == "%PDF-" → else RenderFailed
6. Stream PDF to client (deferred-status streamWriter, as existing paths)
7. Record ConversionRecord{engine:"gotenberg", format:"html", duration, size, status}
```

## Flow B — `POST /v1/convert/html/aspose`

```
1. Shared pre-processing → validated HTML bytes on scratch disk
2. Wait fields present? → reject 400 (no JS engine; D4)
3. Invoke existing worker binary ONE-SHOT (no pool, no chunking):
     prlimit --as=<RAM_BYTES> -- office-convert-worker-docx
       --mode render --input <path> --format html
       --license-path <lic> --output <out.pdf>
   Worker side (designed here, implemented in worker_cpp):
     - format guard accepts "html" in worker-docx
     - Aspose::Words::Document loads via LoadFormat auto-detection
     - IResourceLoadingCallback enforces the deny policy (BR-4) on every
       external resource load (images/CSS); denied → skip resource, continue render
     - PageSetup forced to Letter + 0.5in margins post-load (BR-7)
     - full-document save to PDF (no --page-range)
4. Exit-code mapping (existing contract): 0 ok · 1 RenderFailed · 2 LicenseError ·
   3 InputUnprocessable · 137 OOM→RenderFailed (no subdivision on this path)
5. Validate "%PDF-" magic → stream → record ConversionRecord{engine:"aspose", ...}
```

## Shared pre-processing (both endpoints)

```
1. Rate limit + concurrency semaphore + license pre-check  (existing middleware, unchanged)
2. Read multipart "file"; enforce HTMLMaxBytes (10 MB) → InputTooLarge (400)
3. Validate content IS html (BR-1 sniff; extension fallback) → else InputUnprocessable (422)
4. Write to per-request scratch dir (existing pattern); cleanup in stream-finish
```

## Generic endpoint interaction (D1)

`detect_format` learns to recognize HTML (DispatchFormat "html") so that the GENERIC
`/v1/convert` returns a precise `unsupported_format` Diagnostic whose detail directs callers to
`/v1/convert/html/{gotenberg|aspose}`. The generic endpoint never silently picks an engine.

## Telemetry & comparison data

- `ConversionRecord.engine` ∈ {"", "gotenberg", "aspose"} (empty = pre-existing office paths).
- `/v1/conversions` entries expose `engine`; `/v1/conversions/stats` adds
  `per_engine_html: {gotenberg: {count, avg_ms, p95_ms}, aspose: {...}}` alongside `per_format`.
- Structured log events: `html_convert_start/complete` with engine, duration_ms, failure_class,
  denied_fetch_hosts (hostnames only — SSRF audit, NFR-4).

## Integration points

| Peer | Direction | Contract |
|---|---|---|
| Gotenberg service | out, HTTP | `POST /forms/chromium/convert/html` multipart (fields above); health: `GET /health` |
| worker-docx binary | out, subprocess | existing argv contract + `--format html` (new accepted value) |
| Streamlit UI | in, HTTP | the two new endpoints + `engine` in conversions/stats payloads |
| Compose | deploy | service `gotenberg` (gotenberg/gotenberg:8) with `--chromium-deny-list` flag from BR-4 |

## Testable Properties (PBT-01)

| Component | Property | Category | PBT? |
|---|---|---|---|
| HTML sniffer (`internal/probe`) | Any input of (optional UTF-8 BOM) + arbitrary ASCII whitespace + `<!doctype html` or `<html` (any case) detects as html | Invariant | Yes — rapid generator over prefix permutations |
| HTML sniffer | Random byte strings NOT containing an HTML prefix never detect as html (no false positives vs existing formats: PDF/ZIP/OLE2 magics win first) | Invariant / Oracle (detection-order oracle) | Yes — rapid |
| Deny-policy matcher (`internal/gotenberg` or `internal/netpolicy`) | Every URL whose host is in {loopback, RFC1918, 169.254/16, fd00::/8, fe80::/10, single-label hostname} is denied; generated public IPv4 outside those CIDRs is allowed | Invariant | Yes — rapid generator over IPs/hostnames |
| Deny-policy matcher | Matcher is pure & deterministic: same URL → same verdict (idempotence) | Idempotence | Folded into the invariant test |
| waitDelay validation | parse(format(d)) = d for valid durations ≤ 30s; durations > 30s always rejected | Round-trip + Invariant | Yes — small rapid test |
| Gotenberg response→FailureClass mapping | Total function over enumerable domain {transport-err, 2xx, 4xx, 5xx} | — | No PBT — enumerable; example-based table test |
| Worker exit-code mapping | Already covered by existing tests | — | No PBT — existing coverage |
| UI panel | Visual/interaction | — | No PBT — manual + existing UI conventions |
