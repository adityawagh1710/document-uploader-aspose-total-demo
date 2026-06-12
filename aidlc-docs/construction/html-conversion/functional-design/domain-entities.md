# Domain Entities — unit `html-conversion`

All changes are **additive**; existing entities/wire shapes are untouched. Go orchestrator only.

## Extended entities

### DispatchFormat (`internal/types`)
- New member: `DispatchHTML = "html"`.
- NOT added to `FormatName` / `AsposeFormats` (the closed chunk-pipeline set) — HTML never
  enters the chunk planner.

### ConversionRecord (`internal/obs` / recent feed)
- New field: `engine string` — `""` (existing office paths) | `"gotenberg"` | `"aspose"`.
- Serialized in `/v1/conversions` entries; omitted/empty for legacy records (additive JSON).

### FailureClass (`internal/oerrors`)
- New value: `engine_unavailable` → HTTP 503. New error type `EngineUnavailableError`
  (fields: engine, endpoint URL, cause) following the existing ConversionError pattern.

### Stats payload (`/v1/conversions/stats`)
- New optional object:
  `"per_engine_html": {"gotenberg": {"count":N,"avg_ms":N,"p95_ms":N}, "aspose": {...}}`
  (present once ≥1 HTML conversion recorded; additive).

## New value objects

### HTMLWaitOptions (Gotenberg endpoint)
| Field | Type | Constraint |
|---|---|---|
| `waitDelay` | duration string | optional; valid duration; ≤ 30 s (BR-3) |
| `waitForExpression` | string | optional; ≤ 1024 chars; forwarded opaque |

Parsed from multipart form fields (NOT from the `options` JSON — they are engine-protocol
fields, mirroring Gotenberg's own form contract).

### DenyPolicy (`internal/netpolicy` — new small package)
- Pure matcher: `Denied(rawURL string) (denied bool, reason string)`.
- Embeds the normative table from business-rules.md BR-4 (loopback, RFC1918, link-local,
  IPv6 private, single-label hostnames, non-http(s) schemes).
- Two consumers: (a) emitted as the `--chromium-deny-list` regex for compose (generated
  constant, kept adjacent so the two stay in sync); (b) reimplemented in C++ inside
  `worker-docx`'s resource callback citing BR-4.

## New settings (`internal/config`, env prefix `OFFICE_CONVERT_`)

| Setting | Env var | Default |
|---|---|---|
| `GotenbergURL` | `OFFICE_CONVERT_GOTENBERG_URL` | `http://gotenberg:3000` |
| `GotenbergTimeout` | `OFFICE_CONVERT_GOTENBERG_TIMEOUT_SECONDS` | `120` |
| `HTMLMaxBytes` | `OFFICE_CONVERT_HTML_MAX_BYTES` | `10485760` (10 MiB) |

Empty `GotenbergURL` ⇒ Gotenberg endpoint returns `engine_unavailable` immediately (engine not
configured) — keeps the Aspose endpoint and the rest of the service fully functional without
the extra container.

## Worker contract extension (`worker_cpp`, worker-docx only)
- `--format html` accepted by `worker-docx` (guard relaxed from `!= "docx"` to
  `not in {"docx","html"}`); probe mode for html not required this iteration (no chunking).
- Words `LoadFormat` auto-detection handles `.html`; resource loads pass through the BR-4
  callback; PageSetup forced per BR-7. Exit codes unchanged.

## Relationships

```
HTTP /v1/convert/html/gotenberg --uses--> HTMLWaitOptions --forwarded-to--> Gotenberg service
HTTP /v1/convert/html/aspose    --invokes--> worker-docx(--format html)
both --validate-with--> HTML sniffer (DispatchHTML)
both --record--> ConversionRecord{engine} --aggregated-by--> per_engine_html stats
Gotenberg service + worker-docx callback --enforce--> DenyPolicy (BR-4, single normative source)
```
