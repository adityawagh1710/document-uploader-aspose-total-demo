# HTML Conversion Feature — Requirement Verification Questions

**Stage**: Requirements Analysis (standard depth) · **Date**: 2026-06-12
**Feature**: HTML → PDF via two engine-specific endpoints (Gotenberg / Aspose.Words), **Go orchestrator only**.

Already decided (not re-asked): two separate endpoints `POST /v1/convert/html/gotenberg` and
`POST /v1/convert/html/aspose`; Go-only backend (Python untouched, parity gate scoped to exclude
the new routes); Gotenberg as a separate `gotenberg/gotenberg:8` service; Aspose path reuses
`worker-docx` single-shot (no chunking); `ConversionRecord` gains an `engine` field; Streamlit UI
gets an engine-comparison panel.

Please fill in the `[Answer]:` tags below (e.g. `[Answer]: A`).

---

## Q1 — JavaScript wait controls on the Gotenberg endpoint

Gotenberg snapshots a page as soon as Chromium considers it loaded; JS-heavy pages (SPAs, chart
libraries) need an explicit wait signal or the PDF captures a half-rendered page.

A) Expose **both** `waitDelay` (fixed pause, seconds) and `waitForExpression` (JS expression,
   e.g. `window.status === 'ready'`) as optional multipart form fields — full control, best for
   the fidelity benchmark.
B) Expose only `waitDelay` — simpler, brute-force.
C) Expose neither; use a fixed server-side default delay.
X) Other (please describe after [Answer]: tag below)

[Answer]: A

## Q2 — External resource fetching by Chromium (security: SSRF surface)

An uploaded HTML file can reference remote images/CSS/JS, or internal URLs
(e.g. `http://169.254.169.254/`, `http://localstack:4566/`). Chromium inside Gotenberg will fetch
whatever the page references unless restricted. Gotenberg supports allow/deny URL filters
(`--chromium-allow-list` / `--chromium-deny-list`).

A) **Deny private/internal ranges, allow public internet** — pages render with their CDN
   assets/fonts; metadata/VPC endpoints blocked. (Recommended for a realistic fidelity benchmark.)
B) Deny ALL network fetches — only self-contained HTML renders fully; maximally safe, but most
   real pages will render without styles/images.
C) Allow everything (no filter) — lab/demo only; accepts the SSRF risk inside the VPC.
X) Other (please describe after [Answer]: tag below)

[Answer]: A

## Q3 — External resources on the Aspose endpoint

Aspose.Words also fetches external resources (images/CSS) referenced by the HTML when loading,
via its resource-loading callback (no JS, but remote loads still happen).

A) Block remote loads entirely (resource callback returns skip) — deterministic, safe,
   benchmark measures pure markup rendering.
B) Mirror Q2's policy (same allow/deny posture as Gotenberg) — apples-to-apples fidelity
   comparison.
X) Other (please describe after [Answer]: tag below)

[Answer]: B

## Q4 — HTML input size ceiling

A) Reuse the existing global upload ceiling unchanged.
B) Add a smaller HTML-specific cap (e.g. 10 MB) — HTML inputs are small; a giant "HTML" file is
   almost certainly abuse or misclassification.
X) Other (please describe after [Answer]: tag below)

[Answer]: B

## Q5 — Failure taxonomy for the new endpoints

The wire-stable `FailureClass` enum maps errors → HTTP statuses. The Gotenberg engine introduces
a new failure mode: the Gotenberg service being down/unreachable.

A) Add one new failure class `engine_unavailable` (HTTP 503) for Gotenberg-down; map everything
   else onto existing classes (`render_failed`, `input_unprocessable`, `input_too_large`).
B) No new classes — map Gotenberg-down onto the existing `render_failed` (HTTP 500).
X) Other (please describe after [Answer]: tag below)

[Answer]: A

## Q6 — Deployment scope for this iteration

A) **Local compose only** — `gotenberg` service in `compose.yaml`/`compose.go.yaml`; Helm/EKS
   manifests deferred to a follow-up (benchmarking happens locally first).
B) Local compose **+ Helm chart** (Deployment/Service gated by `gotenberg.enabled`) in this
   iteration — deployable to dev05 EKS immediately.
X) Other (please describe after [Answer]: tag below)

[Answer]: A

## Q7 — UI comparison panel behavior

A) "Convert with both" fires both endpoints **in parallel**, side-by-side result cards
   (latency · output size · status · download/preview) + latency bar chart + `engine` column in
   history. Single-engine buttons also available.
B) Sequential single-engine conversions only (pick engine per run); history/stats still show the
   per-engine split.
X) Other (please describe after [Answer]: tag below)

[Answer]: A
