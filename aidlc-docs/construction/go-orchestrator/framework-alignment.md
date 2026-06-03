# Go orchestrator — framework/library alignment

**Status**: DONE 2026-06-03. **Decision owner**: operator. **Branch**: `feat/go-orchestrator`.

## Context

Ahead of the Phase 8 dev05 cutover we cross-checked the Go orchestrator against
the **`document-uploader` project's AIDLC `tech-environment.md`** — the org's
per-language framework guidance. For Go it lists, under *"Preferred (sensible
defaults; team may override via an inception rerun)"*:

- HTTP routing: **`chi`** ("lightweight, `net/http`-compatible") — or `net/http`
  stdlib "where routing needs are minimal"
- structured logging: **`log/slog`**
- testing: **`testify`** (`assert`/`require`) + **`go-cmp`** for deep comparisons

These are *overridable* recommendations from a *different* project's AIDLC, not
binding on this repo. The operator chose **strict full adoption** for org
consistency. This is an orchestrator-internal refactor with **zero wire-contract
change** — the C++ workers, the JSON-stdio protocol, the Streamlit UI, and the
Helm chart are untouched.

## Adopt-vs-already-met

| Recommendation | Before | After | Notes |
| --- | --- | --- | --- |
| `log/slog` | already used (`internal/oclog`) | unchanged | already compliant |
| Go modules (`go.mod`/`go.sum`) | yes | unchanged | already compliant |
| AWS SDK v2 | yes | unchanged | already compliant |
| HTTP routing → `chi` | pure `net/http` (15 routes) | **`go-chi/chi/v5`** | route table/methods/params identical |
| testing → `testify` | stdlib `testing` (11 files) | **`testify` assert/require** | behavior-preserving |
| deep compare → `go-cmp` | hand-rolled `jsonDiff` (~60 LOC) | **`go-cmp`** + `cmpopts.EquateApprox(0,1e-9)` | net code deletion |

## What changed

- **`internal/server/server.go`** — `http.NewServeMux()` → `chi.NewRouter()`;
  the 15 `mux.HandleFunc("METHOD /path", …)` → chi verbs (`r.Get/Post/Delete`);
  `GET /{$}` → `r.Get("/")`; `requestIDMiddleware` registered via `r.Use(...)`
  (its `func(http.Handler) http.Handler` shape already matched chi's contract);
  the 3 `r.PathValue("request_id")` → `chi.URLParam(r, "request_id")`.
  `cmd/orchestrator/main.go` is unchanged (`chi.Router` is an `http.Handler`).
- **`internal/server/golden_test.go`** — `jsonDiff` replaced with
  `cmp.Diff(want, got, cmpopts.EquateApprox(0, 1e-9))`. The float tolerance is
  what makes Python's `1.0` and Go's `1` compare equal (both decode to
  `float64`); the cursor decode + sentinel normalization are unchanged.
- **11 `*_test.go` files** — `t.Errorf` → `assert.*`, `t.Fatalf` → `require.*`
  (idiomatic forms: `require.NoError`, `require.ErrorAs`, `assert.Equal`,
  `assert.InDelta`, etc.). The PBT files use `require` against `*rapid.T`, which
  satisfies `require.TestingT` (`Errorf` + `FailNow`), so a violated property
  still feeds rapid's shrinker.
- **`go.mod`** — +3 deps: `go-chi/chi/v5` (runtime), `stretchr/testify` +
  `google/go-cmp` (test-only). Module goes from 3 → 6 direct deps; one new
  runtime dep (chi).

## Why this is safe (contract neutrality)

The routing swap is the only behavioral risk, and it's pinned by the
**golden-fixture parity gate** (`make golden-verify`, 14/14): the gate replays
the frozen Python-oracle responses against the Go server and diffs them. It
stayed green across the chi migration, proving the route table, path params,
status codes, headers, and bodies are byte-equivalent (semantically) to before.
Full `make test-go` and `make qa` (Python untouched: 237 passed / 1 skipped)
also green.

## Verification

```
make golden-verify   # 14/14 — wire contract preserved across the chi swap
make test-go         # whole Go module green (all 11 testify-converted files)
make qa              # Python untouched — 237 passed / 1 skipped
```
