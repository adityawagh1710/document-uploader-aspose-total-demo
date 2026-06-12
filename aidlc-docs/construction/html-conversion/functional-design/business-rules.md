# Business Rules — unit `html-conversion`

## BR-1 HTML detection
1. Sniff window: first 1024 bytes of the upload.
2. Strip, in order: UTF-8 BOM (`EF BB BF`) if present, then ASCII whitespace (space, tab, CR, LF).
3. Case-insensitive prefix match: `<!doctype html` **or** `<html` ⇒ HTML.
4. Fallback: if sniff fails but the filename extension is `.html`/`.htm` (case-insensitive) AND
   none of the existing binary magics (PDF/ZIP/OLE2/RTF/image/EML) matched ⇒ HTML.
5. Detection ORDER: existing binary magics are checked first; HTML sniff runs after the image
   check and before the EML check (a ZIP that contains HTML is still a ZIP).

## BR-2 Endpoint-level input validation
- `file` multipart field required; exactly one file. Missing ⇒ `missing_file` (400).
- Size > `HTMLMaxBytes` (default 10 MiB) ⇒ `input_too_large` (400) with size/ceiling detail.
- Content fails BR-1 ⇒ `input_unprocessable` (422) — the engine endpoints convert ONLY HTML.
- Generic `/v1/convert` receiving HTML ⇒ `unsupported_format` (400), detail:
  `"use /v1/convert/html/gotenberg or /v1/convert/html/aspose"` (D1).

## BR-3 Wait controls (Gotenberg endpoint only)
- `waitDelay`: Go-style/Gotenberg duration string (e.g. `2s`, `1500ms`). Bound: **≤ 30 s** (D3).
  Invalid syntax or > 30 s ⇒ 422 `input_unprocessable` with field-level detail.
- `waitForExpression`: opaque string, max 1024 chars; forwarded verbatim (Chromium evaluates it).
  Longer ⇒ 422.
- Either field sent to the **Aspose** endpoint ⇒ 422 `input_unprocessable`,
  detail `"wait controls are not supported by the aspose engine (no JavaScript)"` (D4).
  (Status corrected from the draft's "400": `input_unprocessable` canonically maps to 422.)

## BR-4 External-resource deny policy (CANONICAL — single source, two enforcement points)
A resource fetch initiated by the rendering engine is **denied** iff its URL matches ANY of:

| Rule | Match |
|---|---|
| Scheme | anything other than `http` / `https` (i.e. `file:`, `ftp:`, `data:` is ALLOWED as it involves no fetch) |
| Loopback | host `localhost`, `*.localhost`, IPv4 in `127.0.0.0/8`, `0.0.0.0`, IPv6 `::1` |
| RFC1918 | IPv4 in `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` |
| Link-local / metadata | IPv4 in `169.254.0.0/16` (includes `169.254.169.254`) |
| IPv6 private | `fd00::/8` (ULA), `fe80::/10` (link-local) |
| In-cluster names | any single-label hostname (no `.`) — catches `localstack`, `gotenberg`, `office-convert`, K8s service names |

Everything else (public DNS names, public IPs) is **allowed** (Q2:A). Denied fetches do NOT fail
the conversion — the resource is skipped and the page renders without it; denied hostnames are
logged (NFR-4).

**Enforcement point 1 — Gotenberg**: container flag `--chromium-deny-list` with the regex
equivalent of the table (URL-pattern based; documented caveat: regex-over-URL cannot resolve
DNS, so a public hostname pointing at a private IP is out of scope for this demo).
**Enforcement point 2 — Aspose worker**: `IResourceLoadingCallback` in `worker-docx` parses the
resource URI and applies the same table (can check literal IPs and single-label names; same DNS
caveat). The table above is the normative definition; both implementations cite it.

## BR-5 Failure classification

| Condition | FailureClass | HTTP |
|---|---|---|
| Gotenberg unreachable / connect refused / connect timeout | `engine_unavailable` (NEW) | 503 |
| Gotenberg HTTP 4xx | `input_unprocessable` | 422 |
| Gotenberg HTTP 5xx, malformed PDF output, read timeout mid-render | `render_failed` | 500 |
| Worker exit 1 / 137 / bad PDF | `render_failed` | 500 |
| Worker exit 2 | `license_expired` | 503 |
| Worker exit 3 | `input_unprocessable` | 422 |
| Size cap | `input_too_large` | 400 |
| Non-HTML upload to engine endpoint | `input_unprocessable` | 422 |
| HTML upload to generic endpoint | `unsupported_format` | 400 |

`engine_unavailable` joins the wire-stable FailureClass enum (Go only; recorded as a deliberate
Go-side extension in the parity notes).

## BR-6 Timeouts
- Gotenberg client: total request timeout `GotenbergTimeout` (default **120 s**); must satisfy
  `GotenbergTimeout > waitDelay_max (30 s)` by construction.
- Aspose one-shot: existing worker timeout setting (chunk-timeout equivalent) applies unchanged.
- No retries on either engine path (single-shot semantics; the operator IS the retry loop in a
  benchmarking tool).

## BR-7 Page geometry (fair-comparison rule, D2)
Both engines MUST render to **US Letter (8.5 × 11 in) with 0.5 in margins**:
- Gotenberg: `paperWidth=8.5`, `paperHeight=11`, `margin*=0.5` form fields on every request.
- Aspose: worker sets `PageSetup` (width/height/margins) on all sections after HTML load.

## BR-8 Output validation
First 5 bytes of the engine output MUST be `%PDF-`; otherwise `render_failed`. Applied before
any byte is streamed to the client (deferred-status pattern preserves a clean JSON Diagnostic).

## BR-9 Telemetry
Every terminal outcome (success or failure) records a `ConversionRecord` with
`engine`, `format:"html"`, duration_ms, output_size_bytes (success), failure_class (failure).
Stats endpoint exposes the per-engine HTML breakdown (count/avg/p95).
