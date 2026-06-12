# Frontend Components — Next.js UI (`ui/`)

**Stack**: Next.js 15.x (App Router) · TypeScript strict · Tailwind CSS · SWR (polling) ·
recharts · `output: 'standalone'` (slim Docker runtime, non-root).

## Directory layout

```
ui/
├── app/
│   ├── layout.tsx              # shell: header (health pill, version), dark theme
│   ├── page.tsx                # single-page dashboard (sections, server component shell)
│   ├── globals.css             # Tailwind + design tokens (slate dark, cyan/violet accents)
│   └── api-proxy note: NO route handlers — next.config rewrites do the proxying
├── components/
│   ├── convert/ConvertPanel.tsx        # uploader + options + submit + result
│   ├── html-compare/ComparePanel.tsx   # dual-engine comparison (port of the Streamlit panel)
│   ├── html-compare/EngineCard.tsx     # per-engine result card
│   ├── history/HistoryPanel.tsx        # /v1/conversions table, cursor pagination, filters
│   ├── history/PresignButton.tsx       # /v1/downloads/presign flow
│   ├── stats/HealthTiles.tsx           # /health + /v1/stats + /v1/workers KPI tiles
│   ├── stats/PerfPanel.tsx             # per_format + per_engine_html (recharts bars)
│   ├── dashboard/DashboardFrame.tsx    # iframe → PUBLIC_API_URL/v1/dashboard
│   └── ui/                             # Badge, Card, Spinner, ErrorDiagnostic
├── lib/
│   ├── api.ts                  # typed fetch helpers (all calls → /api/* proxy paths)
│   ├── types.ts                # wire types (see domain-entities.md)
│   └── format.ts               # bytes/ms/date helpers (ports of _human_bytes etc.)
├── next.config.ts              # rewrites + security headers (BR-UI-7)
├── Dockerfile                  # multi-stage node:22-alpine, USER node, port 3000
└── package.json / tsconfig.json / .eslintrc
```

## Proxy (the single-origin mechanism, Q2:A)

```ts
// next.config.ts
rewrites: async () => [{ source: '/api/:path*', destination: `${process.env.API_URL}/:path*` }]
```
Browser only ever talks to the UI origin; the Next server forwards to the Go API. `API_URL`
defaults to `http://office-convert:8080` (compose service name), exactly like Streamlit.
`NEXT_PUBLIC_DASHBOARD_URL` (browser-facing) replaces `PUBLIC_API_URL` for the iframe src only.

## Components ↔ endpoints ↔ polling

| Component | Endpoints (via /api) | Refresh |
|---|---|---|
| HealthTiles | `/health`, `/v1/stats`, `/v1/workers` | SWR 3 s |
| ConvertPanel | `POST /v1/convert` (multipart; streamed blob) | on action |
| ComparePanel | `POST /v1/convert/html/{gotenberg,aspose}` (parallel `Promise.allSettled`) | on action |
| HistoryPanel | `/v1/conversions?cursor&limit&filter` | SWR 5 s + manual |
| PerfPanel | `/v1/conversions/stats` | SWR 5 s |
| DashboardFrame | `GET /v1/dashboard` (iframe, direct browser→API origin) | self-refreshing |

## State rules

- **Server state**: SWR only — no client store. History is API-backed (D5), which makes
  cross-service conversions (curl, classification fanout) visible by construction.
- **Client state**: uploads in flight + the latest comparison result live in component state;
  re-run keeps the last uploaded File in memory for the session (D7).
- **URL state**: history filter + active section as search params (shareable).

## Interaction notes (ports of Streamlit behaviors)

- ConvertPanel: file picker accepts the documented format list; S3-output checkbox appears
  only when `/health`-adjacent config says S3 enabled (probe via a failed presign is NOT used;
  a `NEXT_PUBLIC_S3_ENABLED` env mirrors compose). Response handled as blob; non-200 parsed as
  `Diagnostic` and rendered by `ErrorDiagnostic` (failure_class chip + detail table).
- ComparePanel: wait-control inputs (Gotenberg only) with the JS-fidelity hint text; "Convert
  with BOTH" uses `Promise.allSettled` so one engine failing never hides the other; latency
  bar via recharts; cumulative `per_engine_html` row under the cards.
- HistoryPanel: engine chip column; presign button mints fresh URLs per click; stale-cursor
  responses reset pagination with a toast.
- All interactive elements carry `data-testid` (`convert-submit-button`,
  `compare-both-button`, `history-filter-input`, …) per automation-friendly rules.

## PBT-01 note
No property-based-testable components (rendering/interaction only; all algorithms live
server-side). Documented rationale — component tests (vitest + testing-library) cover
ComparePanel and HistoryPanel logic instead.
