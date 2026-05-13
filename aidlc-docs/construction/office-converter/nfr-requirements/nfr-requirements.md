# NFR Requirements — office-converter (Local v1)

Formalized non-functional requirements for the single v1 unit.
Builds on `aidlc-docs/inception/requirements/requirements.md`
NFR-1 through NFR-8 with the concrete gates and security posture
decided during NFR planning, reflecting the Application Design
Q1 = B pivot (native C++ worker).

## 1. Performance

### 1.1 No Committed SLO; No Internal Budget Targets

Per Q1 = B in the NFR plan: the v1 service operates strictly on a
best-effort basis. There are no committed performance SLOs and no
internal informational budget targets. Performance is observed
(via logs and operator measurement) but not gated.

This is consistent with `requirements.md` NFR-4 ("best-effort
wall-time targets, no formal SLO in v1") and with the v1 scope
(single user, local deployment, exploratory PoC).

### 1.2 Memory Budget (Hard)

The single hard performance constraint:

- Each `office-convert-worker` subprocess address space is bounded
  by `RLIMIT_AS` (configurable via `OFFICE_CONVERT_WORKER_RAM_BYTES`,
  default 6 GiB, compose deployment sets 10 GiB to match the
  container's `mem_limit + memswap_limit` budget). The cap is
  enforced by the kernel, not by application convention.
- **Revised 2026-05-12**: the original 2 GB hard floor was relaxed
  to enable Docker-swap-backed survivability for 500 MB-class PPTX
  inputs. `RLIMIT_AS` counts swapped pages against the cap, so it
  MUST be sized ≥ `mem_limit + memswap_limit` for the swap cushion
  to function. See `aidlc-state.md` "Per-pod RAM ceiling" for the
  full revision history and current posture.
- The full output PDF MUST NOT be buffered at any layer (Python or
  C++). qpdf streams to stdout; FastAPI streams to the response
  body via chunked transfer encoding.

### 1.3 Throughput

Determined by `max_jobs × parallel` (defaults 1 × 2 = 2 concurrent
C++ worker subprocesses). v1 expects low single-user load; no
explicit throughput requirement.

## 2. Reliability

### 2.1 Failure Recovery

- **Process-level failures** (license missing, qpdf binary missing,
  Aspose `.so` not found, unrecoverable startup error): container
  exits non-zero. The operator's container runtime restarts via
  `--restart unless-stopped` or equivalent. No in-process recovery
  beyond per-request error handling.
- **Per-request failures**: handled via the failure-class taxonomy
  (functional-design `business-rules.md §3`). Failures never
  cascade beyond a single request; the server remains available
  to other requests.
- **Mid-request crashes**: in-flight requests fail at the HTTP
  layer (connection drop). The caller retries.

### 2.2 Determinism

| Subsystem               | Deterministic? | Verified by         |
| ----------------------- | :------------: | ------------------- |
| Chunk planner           | yes            | PBT (Hypothesis)    |
| Subdivision logic       | yes            | PBT                 |
| Cache keys              | yes            | Unit + PBT          |
| qpdf concat             | yes            | Integration tests   |
| Aspose render (bytes)   | no             | (accepted, NFR-5 in requirements.md) |

## 3. Security

Compliance with the enabled `security-baseline` extension.

### 3.1 Trust Boundary

- v1 runs locally; trust boundary is the host.
- Server binds to `0.0.0.0:8080` inside the container.
- Operator decides host-side port mapping. README documents
  `--publish 127.0.0.1:8080:8080` as the recommended localhost-
  only deployment.

### 3.2 No Application-Layer Auth in v1

Explicit non-requirement, documented in `requirements.md` NFR-8.
Future scope (cloud v2) re-introduces auth.

### 3.3 Secrets and Sensitive Data

- Aspose `.lic` file is bind-mounted at runtime; NOT baked into the
  image, NOT committed to source.
- License contents never appear in logs. Error paths redact
  license content before logging.
- Document contents never appear in logs. Log events carry only
  metadata (size, format, page count, request_id).

### 3.4 Input Validation

- Format detection by magic bytes (not file extension) on the
  first 512 bytes of the multipart stream, before any disk write.
- Inputs exceeding 1 GB are rejected at ingest with HTTP 400.
- Inputs failing magic-byte detection are rejected with HTTP 400
  before any subprocess is spawned.

### 3.5 Container Security Posture

| Control                       | Mechanism                                                   |
| ----------------------------- | ----------------------------------------------------------- |
| Non-root user                 | Runtime stage creates `appuser:appgroup`, `USER appuser`    |
| Read-only root filesystem     | Image compatible with `--read-only`; operator passes `--tmpfs /tmp --tmpfs /var/run` |
| Dropped Linux capabilities    | Operator passes `--cap-drop=ALL`; image needs no capabilities |
| Minimal runtime base image    | `python:3.11-slim-bookworm`                                 |
| Pinned dependency versions    | `uv.lock` in image for Python deps; Aspose tarball version pinned in Dockerfile |
| C++ build artifacts isolated  | Multi-stage build; compiler, headers, build tools never enter runtime image |

### 3.6 Process Isolation

- Each C++ worker render runs in a fresh subprocess with
  `RLIMIT_AS=2 GB`.
- Subprocess cannot write outside the per-request scratch directory.
- Subprocess receives only the paths it needs (input, output,
  license) via argv; no environment-variable leakage.
- No Python loaded inside the worker process — minimal attack
  surface, no Python interpreter to exploit.

## 4. Maintainability

### 4.1 Code Quality Gates

| Check                | Tool          | Gate                                       |
| -------------------- | ------------- | ------------------------------------------ |
| Python linting       | ruff          | All rules in project `ruff.toml` pass      |
| Python formatting    | ruff format   | No diffs on `--check`                      |
| Python type checking | mypy --strict | No errors on `office_convert/`             |
| Python unit tests    | pytest        | All pass                                   |
| Property-based tests | Hypothesis    | All pass; 500 examples on chunk planner, 100 elsewhere |
| Integration tests    | pytest + FastAPI `TestClient` | All pass               |
| Line coverage (Python) | pytest-cov  | ≥ 80% on `office_convert/` (excluding `server` HTTP wiring) |
| C++ build            | CMake (`cmake --build`) | Builds cleanly with `-Wall -Wextra -Werror` |
| C++ tests (if present) | GoogleTest  | All pass (optional in v1; skip if no C++ unit tests) |
| Container build      | docker buildx | Image builds cleanly                       |
| Container smoke      | bash + curl   | `/health` returns 200, sample doc converts |

### 4.2 Documentation Requirements

- `README.md` (root of source repo) covers: prerequisites
  (Aspose.Total C++ license + SDK tarball, Docker), build (multi-
  stage), run, env vars, troubleshooting, known v1 limitations.
- Inline comments only where the WHY is non-obvious.
- Public function/class docstrings (Python) and public-header
  doc comments (C++) on modules with > 1 public symbol.

### 4.3 Dependency Hygiene

- Python direct dependencies pinned to a minor version in
  `pyproject.toml`; transitive pinned exactly via `uv.lock`.
- C++ direct deps: Aspose.Total C++ tarball version pinned in
  Dockerfile (filename or download URL); CMake project specifies
  required C++ standard.
- Lockfiles committed to source. Regenerated via `uv lock`.
- Security advisories tracked via Dependabot or operator's choice;
  not enforced in v1.

## 5. Testability

### 5.1 Test Pyramid

| Layer                       | Tooling                                | Targets                                    |
| --------------------------- | -------------------------------------- | ------------------------------------------ |
| Python unit (pure logic)    | pytest                                 | chunk_planner, qpdf wrapper, license parser, cache, probe parsing, format detection |
| Python unit (HTTP)          | pytest + FastAPI `TestClient`          | endpoint routing, request validation, error→status mapping |
| Property-based              | Hypothesis (500 for planner, 100 elsewhere) | chunk planner invariants, subdivision termination, qpdf concat round-trip |
| Integration (in-process)    | pytest + `TestClient` + fake worker    | full async pipeline with mocked worker subprocess |
| End-to-end (Docker)         | pytest + **Testcontainers** + real container | real Dockerfile + real C++ worker + real Aspose; gated by `OFFICE_CONVERT_E2E_LICENSE` |
| C++ unit (optional)         | GoogleTest                             | format dispatch, license parsing, exit-code translation (if value warrants) |
| Smoke (manual + automated)  | `curl` against the container           | `/health` reachable, golden doc convert    |

The **end-to-end layer (Testcontainers)** is the only test path that
verifies the Dockerfile, real Aspose linkage, real qpdf concat at
real sizes, and real `prlimit RLIMIT_AS=2G` behavior. Skipped by
default (gated on `OFFICE_CONVERT_E2E_LICENSE`); CI without an Aspose
license runs only the in-process suite. Dual-mode design: tests that
exercise rendering accept either HTTP 200 (real Aspose linked) or 500
`render_failed` (scaffolded worker without Aspose SDK) so the suite
verifies the Docker plumbing even before Aspose is fully wired in.

### 5.2 Test Document Corpus

Synthetic corpus generated by AI (Q11a in Functional Design),
checked into `tests/corpus/`:

- `small.docx` (3 pages, plain text)
- `medium.docx` (100 pages, mixed content)
- `simple.pptx` (5 slides, plain)
- `complex.pptx` (20 slides, embedded images)
- `single_sheet.xlsx` (1 sheet, 20 columns × 100 rows)
- `multi_sheet.xlsx` (4 sheets of varying sizes)
- `simple.pdf` (10 pages, generated by ReportLab or similar)

Each fixture has a sibling `.expected.txt` with metadata (page
count, format, expected number of chunks at default settings).

Generation scripts in `tests/corpus/_generate.py`.

### 5.3 Integration Test Strategy

Integration tests must invoke the real C++ worker binary (so they
exercise Aspose.Total C++, the license-loading path, and the
exit-code contract). Two run modes:

- **In-container**: tests run inside the container during CI;
  worker binary, Aspose `.so`, and license are all present.
- **Outside-container** (dev): operator runs `docker compose up`
  (or similar) and points `pytest` at the running container's
  port. Slower but useful for diagnosing test failures.

## 6. Observability

### 6.1 Logging

- Structured JSON-lines to stdout by default (Q10 default).
- Human format opt-in via `OFFICE_CONVERT_LOG_FORMAT=human`.
- Closed event vocabulary (functional-design `business-rules.md §8`).
- Every event carries `request_id` via `contextvars` propagation.
- No remote sink in v1; operator handles aggregation.
- C++ worker writes diagnostic JSON to stderr on failure; the
  orchestrator captures and incorporates into Python-side log
  events.

### 6.2 Health and Status

- `/health` returns readiness state:
  - 200 with body `{ready: true, license_days_remaining, active_jobs, max_jobs}`
    when: license is valid AND scratch dir is writable AND qpdf
    binary is present AND C++ worker binary exists AND Aspose `.so`
    is loadable (verified at server startup).
  - 503 otherwise with body identifying the failure.

### 6.3 Metrics

- No metrics endpoint in v1. Operators derive metrics by stream-
  processing the structured logs.

## 7. Availability

- Local v1 has no formal availability target. The service is
  available iff its single container is running and the host is
  up.
- No HA, no failover, no replication. Documented as v1 scope per
  `requirements.md` "Out of Scope" section.

## 8. Usability (Operator Experience)

- All Python configuration via env vars. No config file to learn.
- README documents every env var with type, default, and constraint.
- README documents the Aspose tarball acquisition step (operator
  downloads from their Aspose account, places in build context).
- Failure responses include `request_id` so operators can grep
  logs by ID.
- `/health` provides a single readable signal of service state.

## 9. Property-Based Testing Compliance

The PBT extension is enabled as blocking. Required invariants
(see `functional-design/business-logic-model.md §2.6` and
`domain-entities.md`):

### Chunk planner (500 Hypothesis examples)

- `sum(c.pages for c in plan.chunks) == probe.page_count`
- Chunks are non-overlapping, monotonic, complete cover of `[1..page_count]`
- `all(c.estimated_mb ≤ max_mb × balance_factor for c in plan.chunks)`
- `all(c.pages ≤ max_pages × balance_factor for c in plan.chunks)`
- Determinism: `plan_chunks(p, m, mb) == plan_chunks(p, m, mb)`

### Subdivision logic (100 Hypothesis examples)

- Terminates: `subdivide(c)` with `c.pages > 1` returns 2 chunks
  whose combined page range equals `c`'s range
- Floor: `subdivide(c)` with `c.pages == 1` returns `[]`
- Determinism

### qpdf concat (100 Hypothesis examples)

- `concat(a, b).page_count == a.page_count + b.page_count`
- Order preserved
- Associative

### Format detection (100 Hypothesis examples)

- `detect_format(magic_bytes_for(f))` returns `f` for f ∈
  {docx, pptx, xlsx, pdf}
- `detect_format(random_bytes_not_matching_any_magic)` raises
  `UnsupportedFormatError`

## 10. Open Items

None. All Requirements Analysis open items (sample corpus, target
host, Python version, license type) were resolved during prior
stages. The C++ pivot resolved itself (Application Design Q1 = B).
