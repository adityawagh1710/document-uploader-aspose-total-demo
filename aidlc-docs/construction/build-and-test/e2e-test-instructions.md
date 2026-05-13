# End-to-End Test Instructions — office-converter (Local v1)

## Purpose

Verify the real Docker image end-to-end via [Testcontainers](https://testcontainers.com/).
Catches what in-process tests (FastAPI TestClient + fake worker) cannot:

- Dockerfile correctness (apt deps, ENV, USER, LD_LIBRARY_PATH)
- Real C++ worker binary linkage (Aspose `.so` symbols resolvable)
- Real Aspose render + license activation
- Real qpdf concat at real PDF sizes
- Real `prlimit RLIMIT_AS=2G` behavior

## Test Inventory

`tests/e2e/test_real_conversion.py` — 5 tests:

| Test | What it verifies |
| ---- | ---------------- |
| `test_health_reports_ready` | `/health` returns 200 with `ready: true` and `license_days_remaining > 0` |
| `test_simple_pdf_converts` | Real `simple.pdf` round-trips through `/convert` and returns a valid PDF |
| `test_unsupported_format_is_rejected` | Real container correctly returns 400 + structured failure |
| `test_request_id_correlates_with_response_header` | `X-Request-ID` matches `body.request_id` |
| `test_docx_converts_if_corpus_present` | **Dual-mode** — accepts 200 (real Aspose linked) OR 500 `render_failed` (scaffolded worker without Aspose SDK). Validates Docker plumbing even before Aspose is wired in. |

## Prerequisites

1. **Real Aspose.Total C++ SDK tarball** in the build context. Without it,
   the C++ worker still compiles but the format renderers throw
   `RenderException("SDK not linked")` at runtime. Tests transition
   between dual-mode 500 → 200 once the SDK is properly linked.
2. **Aspose.Total C++ Temporary License** valid `.lic` file.
3. **Docker daemon** accessible at test time.
4. **e2e Python extra installed**:
   ```bash
   uv pip install -e .[dev,e2e]
   # Adds testcontainers==4.8.* and httpx==0.27.*
   ```
5. **Image built**: `docker build -t office-convert:test .`

## Setup the E2E Environment

### 1. Build the image with the real SDK

```bash
cp /path/to/aspose-total-cpp.tar.gz .
docker build -t office-convert:test .
```

(See `build-instructions.md` for the full Docker build procedure.)

### 2. Set the gating env vars

```bash
export OFFICE_CONVERT_E2E_LICENSE=/path/to/license.lic
export OFFICE_CONVERT_E2E_IMAGE=office-convert:test    # optional; default is this value
```

The license file MUST be readable by the user running pytest (the
Testcontainers fixture bind-mounts it into the container as read-only).

## Run

### Full e2e suite

```bash
pytest tests/e2e -m e2e -v
```

### Without `-m e2e` filter (still works; tests are individually decorated)

```bash
pytest tests/e2e -v
```

### Skipping fast in-process suite (e2e only)

```bash
pytest tests/e2e -m e2e -v --ignore=tests/unit --ignore=tests/property --ignore=tests/integration
```

## Expected Results

| Item | Expected (with real Aspose SDK) | Expected (scaffolded worker) |
| ---- | ------------------------------- | ----------------------------- |
| `test_health_reports_ready` | Pass | Pass |
| `test_simple_pdf_converts` | Pass (200 + PDF) | May fail (500 `render_failed`) |
| `test_unsupported_format_is_rejected` | Pass | Pass |
| `test_request_id_correlates_with_response_header` | Pass | Pass |
| `test_docx_converts_if_corpus_present` | Pass (200 + PDF) | Pass (500 `render_failed`) |
| Wall time | ~30-60 s session start + ~5-20 s per test | Same |

## Verify Service Interactions

The e2e fixture logs the container's stdout/stderr during the test
session — if a test fails, those logs are shown. Look for:

| Log event | Meaning |
| --------- | ------- |
| `server_start` | Container's lifespan startup ran; license loaded |
| `request_received` | A test's POST reached the FastAPI handler |
| `request_complete` / `request_failed` | Per-request outcome with request_id |
| `subdivision_retry` | An OOM triggered subdivide (won't fire with the scaffold) |

## Cleanup

Testcontainers' session-scoped `converter` fixture stops the container
automatically at session end. Manual cleanup if a session crashes:

```bash
docker ps --filter "ancestor=office-convert:test" -q | xargs -r docker stop
docker ps -a --filter "ancestor=office-convert:test" -q | xargs -r docker rm
```

## CI Considerations

- Add the e2e suite as a **separate, gated job** in CI. The fast unit +
  property + integration suites run on every commit; the e2e suite runs
  on commits to `main` and on release tags.
- CI needs Docker-in-Docker or rootless Docker.
- The Aspose `.lic` is a CI secret. Mount it into the CI runner's
  filesystem, set `OFFICE_CONVERT_E2E_LICENSE` to its path.
- Optional: rebuild the image only on Dockerfile/worker_cpp changes;
  reuse a cached `office-convert:test` tag otherwise.

## What E2E Tests Do NOT Cover (still)

- Rendering correctness at the *visual* level. The e2e suite verifies
  "valid PDF returned" — not "PDF visually matches the source DOCX".
  Visual correctness requires a separate diff-PDF or pixel-compare
  workflow, out of scope for v1.
- Performance under load. v1 has no committed SLO; soak tests are
  operator-driven, not part of the AI-DLC test pyramid.
- Multi-region / DR behavior. Single-container local-only v1.
