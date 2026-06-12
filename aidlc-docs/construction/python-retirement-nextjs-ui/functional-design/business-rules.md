# Business Rules — unit `python-retirement-nextjs-ui`

## BR-R1 Retirement inventory (exact, FR-1/FR-2)

**DELETE** (only after the Next.js UI is verified against the live stack — execution-plan order):
```
office_convert/                  # Python orchestrator (~25 modules)
office_convert_ui/               # Streamlit UI
tests/                           # pytest suite (unit/integration/property/e2e/corpus)
pyproject.toml  ruff.toml
Dockerfile                       # Python prod image (superseded by go.Dockerfile)
Dockerfile.test                  # Python test-runner image
Dockerfile.ui                    # Streamlit image (replaced by ui/Dockerfile)
scripts/capture_golden.py        # capture oracle (fixtures stay)
.mypy_cache/ .ruff_cache/ .pytest_cache/ .hypothesis/   # local caches (gitignored anyway)
```

**MOVE**: `tests/corpus/*` → `testdata/corpus/` (D4); update references in README +
build-and-test docs + Makefile convert examples.

**KEEP frozen**: `internal/server/testdata/golden/` (Go-only regression anchors);
`smoke_test/` (C++ Words smoke — not Python).

## BR-R2 Consolidation mapping (FR-4)

| From | To |
|---|---|
| `go.Dockerfile` | `Dockerfile` (the only backend image) |
| `compose.go.yaml` | merged INTO `compose.yaml` (Go api + gotenberg + localstack + new ui); overlay file deleted |
| `ui/Dockerfile` builds `office-convert-ui:dev` | same local tag convention (cross-repo unaffected) |
| Make: `build-go/test-go/run-go/up-go/logs-go…` | canonical `build/test/run/up/logs…`; Python-only targets (`lint`, `format*`, `typecheck`, `qa`, `build-test`) deleted; new `ui-install/ui-dev/ui-build/ui-lint` |
| Image tag `office-convert:go` | KEPT as-is (classification-service consumer contract) |

## BR-R3 CI / Dependabot (FR-5)

- `.github/workflows/ci.yml`: delete the `qa` job; keep `go-test` (golden 14/14 is the
  contract tripwire) + `helm-lint`; add `ui-test` job: `npm ci && npm run lint && tsc
  --noEmit && next build` (working-directory `ui/`).
- `security.yml` (Trivy): unchanged (fs scan now covers `ui/` too).
- `dependabot.yml`: `pip` ecosystem → `npm` (directory `/ui`); `gomod`, `docker`,
  `github-actions` unchanged.

## BR-R4 Rollback anchor (FR-6)
Annotated tag `last-python-backend` on the branch HEAD **before** the deletion commit, pushed
with the branch. Message records: last commit containing the Python orchestrator + Streamlit
UI + pytest suite.

## BR-UI rules (the Next.js app)

- **BR-UI-1 Single origin**: every browser API call goes to `/api/*` (Next rewrites →
  `API_URL`). The ONLY direct browser→API URL is the dashboard iframe src.
- **BR-UI-2 Error rendering**: non-200 responses are parsed as `Diagnostic`
  (`{request_id, failure_class, detail}`) and rendered as text (failure_class chip + key/value
  detail). Never `dangerouslySetInnerHTML` with API data.
- **BR-UI-3 Client-side mirrors of server caps** (fast feedback; server stays authoritative):
  HTML uploads ≤ 10 MiB; `waitDelay` ≤ 30s pattern-checked; wait fields disabled on the
  Aspose run.
- **BR-UI-4 Polling cadence**: health/stats 3 s, history/perf 5 s (SWR `refreshInterval`),
  paused when the tab is hidden (SWR default focus/visibility behavior).
- **BR-UI-5 History is API-truth** (D5): no client-side history store; engine chip from the
  record's `engine` field; presign links minted per click, never cached.
- **BR-UI-6 Re-run scope** (D7): re-run buttons only for files held in this session's memory.
- **BR-UI-7 Security headers** (SECURITY-04, via next.config `headers()`):
  `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; style-src 'self'
  'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src <PUBLIC_API_ORIGIN>;
  object-src 'none'; base-uri 'self'` · `X-Content-Type-Options: nosniff` ·
  `X-Frame-Options: SAMEORIGIN` · `Referrer-Policy: strict-origin-when-cross-origin`.
  (`style-src 'unsafe-inline'` is the Tailwind/Next reality; scripts stay nonce-free because
  no inline scripts are emitted with the App Router defaults we use.)
- **BR-UI-8 Container**: `node:22-alpine`, `USER node`, port 3000, `output: 'standalone'`;
  compose maps `127.0.0.1:8501:3000` (D6); Helm service stays on its existing port mapping.
- **BR-UI-9 Accessibility/automation**: semantic landmarks; `data-testid` on all interactive
  elements (stable names listed in frontend-components.md).
