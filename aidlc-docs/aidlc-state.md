# AI-DLC State Tracking

## Project Information
- **Project Type**: Greenfield
- **Start Date**: 2026-05-11T00:00:00Z
- **Current Stage**: INCEPTION - Requirements Analysis

## Workspace State
- **Existing Code**: No
- **Programming Languages**: N/A (no source yet — design doc only)
- **Build System**: N/A
- **Project Structure**: Empty (design document only)
- **Workspace Root**: /home/adityawagh/opus2-workspace/aspose-total
- **Reverse Engineering Needed**: No
- **Source Material**: office-converter.md (design specification for chunked Office→PDF conversion service on EKS using Aspose.Total C++)

## Code Location Rules
- **Application Code**: Workspace root (NEVER in aidlc-docs/)
- **Documentation**: aidlc-docs/ only
- **Structure patterns**: See code-generation.md Critical Rules

## Extension Configuration

| Extension              | Enabled | Decided At                              |
| ---------------------- | ------- | --------------------------------------- |
| security-baseline      | Yes     | Requirements Analysis (2026-05-11)      |
| property-based-testing | Yes     | Requirements Analysis (2026-05-11)      |

## User Goal

- **Mode**: Full AI-DLC through code generation (INCEPTION + CONSTRUCTION end-to-end)

## Scope Pivot (2026-05-11) — Local v1, Cloud Deferred

**User directive**: "keep it simple for now - we'll get it working locally first (no EKS)."

**v1 scope**: Local proof of the chunk-render-merge algorithm. Single laptop or single
Linux box. No EKS, no SQS, no DynamoDB, no S3, no multi-tenancy, no auth, no
autoscaling, no observability stack. Just the engine working on real Office documents
inside the 2 GB RAM ceiling.

**Deferred to future cloud scope**: everything in
`inception/requirements/requirement-verification-questions.md`. That file's 25 answers
represent the eventual cloud-target requirements and are preserved for that future
work, but are NOT requirements for v1.

**v1 requirements live in**: `inception/requirements/local-v1-scope.md`.

## Hard Constraints (load-bearing — propagate to all stages)

- **Per-pod RAM ceiling**: **4 GB physical + 2 GB swap = 6 GB total budget** (Aspose worker container). Revised 2026-05-12 from the original 2 GB ceiling; swap budget tightened the same day from 6 GB to 2 GB per operator decision.
  - **History**: Originally 2 GB (stated by user 2026-05-11). Bumped to 4 GiB `worker_ram_bytes` default in code during construction. Formally relaxed 2026-05-12 to enable swap-backed survivability for 500 MB-class PPTX inputs. Swap component reduced from 6 GB → 2 GB later 2026-05-12 (operator preferred a smaller cushion — see audit entry for the rationale).
  - **Current posture** (compose.yaml): `mem_limit: 4g` + `memswap_limit: 6g` + `mem_swappiness: 60`. Kernel pages out to host NVMe swap under RAM pressure rather than OOM-killing the worker.
  - **Worker-side**: `OFFICE_CONVERT_WORKER_RAM_BYTES=6442450944` (6 GiB) sized to match `memswap_limit` so the per-process `prlimit RLIMIT_AS` cap does NOT block swap. `RLIMIT_AS` counts swapped pages — if it were < `memswap_limit` the worker would `ENOMEM` before the kernel pages out, defeating the cushion.
  - **Implication**: OOM escape valves now in order: (a) swap cushion absorbs spikes up to ~2 GB beyond RAM (latency cost), (b) subdivision re-render at smaller page range, (c) dead-letter at single-page floor. Chunk timeout bumped 300s → 600s (`OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS`) to accommodate swap-thrash latency.
  - **Implication**: Aspose's documented 2–20× amplification factor means worst-case safe input chunk ≈ (6 GB − overhead) ÷ 20 ≈ ~270 MB; typical safe chunk ≈ 600 MB – 1 GB. With the tighter 2 GB swap cushion, the chunk planner is the primary line of defense, not swap. Inputs > ~250 MB of PPTX may exceed the budget even with swap.
  - **Risk**: a runaway worker can consume the full 2 GB swap budget for ~10+ minutes before timeout; observability needs `worker_swap_used_bytes` alerting (already on the metric list). The smaller cushion means OOM kills will be more frequent on big-PPTX inputs vs. the 6 GB swap variant.
  - **Compose change ALSO applies to** future EKS migration: node group needs `memswap_limit`-equivalent via `failSwapOn: false` + `memorySwap.swapBehavior: LimitedSwap` (already documented in the swap-on-Aspose-pods constraint below — those flags now actually deliver value because the orchestrator/worker won't block swap usage via RLIMIT_AS).
- **C++ worker Tier-1 perf optimizations** (2026-05-11): lazy product activation (`apply_license(path, format)` activates only the Aspose namespace matching the requested format; saves ~150–600 ms per worker startup) + Release-config compiler/linker flags (`-O2 -flto -fvisibility=hidden -fdata-sections -ffunction-sections` + `--gc-sections` + strip; ~30–100 ms saved per spawn, 10–30% smaller binary). Total expected saving: ~200–700 ms per chunk render of startup overhead.
- **Testcontainers-driven E2E test layer** (2026-05-11): `tests/e2e/` with session-scoped Docker fixture, bind-mounted real Aspose license, real HTTP via httpx. Gated on `OFFICE_CONVERT_E2E_LICENSE` env var; dual-mode design accepts either 200 (real Aspose linked) or 500 `render_failed` (scaffolded worker) so Docker plumbing is validated even before Aspose SDK is wired in.
- **Swap enabled on Aspose worker pods** (Q10 sub-requirement, 2026-05-11). 2–4 GB swap per pod on local NVMe storage, LimitedSwap behavior.
  - **Implication**: Node OS must support cgroupv2 — Bottlerocket or Amazon Linux 2023, NOT AL2.
  - **Implication**: Worker node group pinned to instance types with local NVMe (m5d/m6id/r5d family); EBS-backed swap is unacceptable.
  - **Implication**: Custom node bootstrap or custom AMI required; standard EKS-optimized AMIs do not ship with swap.
  - **Implication**: Kubelet config: `failSwapOn: false`, `memorySwap.swapBehavior: LimitedSwap`.
  - **Implication**: Observability adds `worker_swap_used_bytes` and `worker_swap_in_pages_per_second` to the worker metric list; chronic swap usage is an alerting condition signaling the chunk planner needs revisiting.
- **Aspose SKU pivot: Aspose.Total C++** (2026-05-11). Application Design Q1 revised from A (Aspose.Total for Python via .NET) to B (Aspose.Total C++ with a custom C++ render binary invoked as subprocess from Python). The Python orchestrator, chunk planner, HTTP server, qpdf wrapper, cache, license verification, and types remain Python; only the worker subprocess is now compiled C++.
  - **Implication**: No .NET runtime in the image. No `aspose-words` / `aspose-slides` / `aspose-cells` / `aspose-pdf` Python packages. .NET-related NFR Q5 is N/A.
  - **Implication**: Dockerfile becomes multi-stage — a C++ builder stage (compiler + Aspose.Total C++ headers + library) producing the `office-convert-worker` binary, copied into the slim runtime stage.
  - **Implication**: Aspose Temporary License must be the **C++ SKU** specifically; the Python-via-.NET temp license is not compatible.
  - **Implication**: Worker module changes from `office_convert.worker` (Python) to a compiled C++ binary `office-convert-worker` shipped in `/usr/local/bin/` of the runtime image. The argv + exit-code contract from `business-rules.md §2` is unchanged.
  - **Refinement (2026-05-12) — 4-libs vendor path supersedes Total-tarball path**: The shipped Linux Total bundle (`aspose.total_for_cpp_linux_26.4.0.zip`) omits **Aspose.Words for C++** and **Aspose.Email for C++** — Aspose differentially excludes them from the C++ Linux SKU. The intermediate "C++ SKU is missing Words → pivot to Python via .NET" pivot has been abandoned. The **resolution** is the 4-libs vendor path (Path B): source 4 individual product libraries into per-product directories.
    - **Sourcing decision**: Words 26.3 from `Aspose.Words.Cpp_26.3.zip` (inner artifact of `~/Downloads/aspose.total_for_cpp_windows_26.4.0.zip`; the inner zip is a cross-platform package that ships Linux x86_64 `.so` despite the parent zip's "windows" name). Cells 26.4 + Slides 26.4 + PDF 26.4 from the existing Linux Total bundle's per-product inner zips. Zero new downloads required.
    - **License**: existing `Aspose.TotalforC++.lic` (umbrella `<Product>Aspose.Total for C++</Product>`) unlocks all 4 — each library's validator accepts the Total umbrella string.
    - **Layout**: per-product subdirectories under `vendor/aspose/{Words,Cells,Slides,PDF}/` (each ships its own `libcodeporting...so` sibling; flattening would cause ABI clashes). RUNPATH `$ORIGIN:$ORIGIN/../lib` finds each lib's siblings inside its own subdir. Runtime image deploys these as `/opt/aspose/{Words,Cells,Slides,PDF}/`.
    - **glibc floor**: Words 26.3 `.so` requires GLIBC_2.34 + GLIBCXX_3.4.30 (verified via `readelf`). Existing Dockerfile base `python:3.12-slim-bookworm` provides glibc 2.36 — compatible. Forecloses Alpine/musl and Ubuntu ≤ 20.04 bases.
    - **CMakeLists.txt impact**: replace single `-DASPOSE_SDK=<path>` with 4 `find_package(Aspose.{Words,Cells,Slides,Pdf}.Cpp REQUIRED PATHS vendor/aspose/<Product>)` calls. Each product ships its own CMake config (`*-config.cmake`, `*-targets.cmake`).
    - **Dockerfile impact**: builder stage receives 4 vendor trees via `COPY vendor/aspose/ /opt/aspose/` instead of `COPY aspose-total-cpp.tar.gz`. Multi-stage shape unchanged; runtime stage copies `/opt/aspose/<Product>/lib/` per product.
    - **make verify-sdk impact**: existing target validates one tarball; needs rewrite to validate 4 vendor trees.
    - **AI-DLC state**: `project_pending_aspose_pivot.md` memory is now SUPERSEDED. The Python-via-.NET pivot does NOT happen.
- **Maximum input size: 10 GB / 50,000 pages** (Q3 answered C, revised from D). Inputs above this ceiling are rejected at ingest. Confirmed by user on 2026-05-11.
  - **Implication**: Ingest validation on S3 `HeadObject` `ContentLength`; second gate on probe-derived page count.
  - **Implication**: Orchestrator streams source rather than materializing on local disk (avoids 10 GB ephemeral-disk overhead per pod). Aspose workers pull only assigned page ranges.
  - **Implication**: Chunk plan externalized to SQS for horizontal-orchestrator scaling, even though in-memory would fit at this ceiling.
  - **Implication**: Job state persisted to DynamoDB; orchestrator restart is resumable.
  - **Implication**: Probe must respect 2 GB ceiling (`LoadOptions::TempFolder`, `MemoryOptimization`). Per-format verification required during NFR design.
  - **Implication**: Tiered SLO — Q2's p95 ≤ 15 min (revised 2026-05-11) applies to inputs ≤ 100 MB only; > 1 GB is best-effort; intermediate measured but not SLO'd in v1.
- **Queue-driven API** (Q5 answered D, revised from B). Callers submit via SQS, no public HTTP endpoint on the orchestrator. Confirmed by user on 2026-05-11.
  - **Implication**: Per-tenant submit queue `aspose-jobs-<tenant-id>`; IAM `sqs:SendMessage` is the auth surface.
  - **Implication**: Submit message carries `correlation_id`, source S3 URI, optional callback ARN, options.
  - **Implication**: Completion event published to caller-supplied SQS/SNS ARN (if any); always written to S3 at `<tenant_id>/<correlation_id>/output.pdf`.
  - **Implication**: Status / progress via DynamoDB table keyed by `(tenant_id, correlation_id)` — caller reads directly via IAM-scoped access. Per-tenant progress SNS topic is Phase-2.
  - **Implication**: Q9 worker sizing — final pick is A (static fixed replica count) after revision history D → C → A on 2026-05-11. v1 ships with N=4 starting heuristic, manual operator scaling. KEDA is explicitly NOT on the roadmap — bring back into scope only if duty cycle data shows the static fleet is materially mis-sized.
  - **Implication**: Q18 DLQ — SQS native redrive (`-dlq` queue) handles orchestrator-crash retries; S3 forensic record still needed for chunk-subdivision-floor failures.
- **No application-layer auth in v1** (Q6 answered X, revised from A). Service-enforced caller identity verification deferred to v2. Confirmed by user on 2026-05-11.
  - **Implication**: `tenant_id` is caller-asserted in v1 (taken from SQS message body, not validated against IAM principal). Q4 = B remains the *data layout*, but isolation reduces to organizational convention under the AWS account boundary.
  - **Implication**: Per-tenant quotas (Q4) deferred to v2. v1 enforces only global fleet-wide ceilings.
  - **Implication**: Presigned URL TTL tightened from 24 h → 1 h in v1 to shrink unintended-access window; restored to 24 h in v2.
  - **Implication**: Q14 cache-bypass abuse mitigated by global ceiling + CloudWatch alarm on per-IAM-principal `nocache: true` submission rate.
  - **Implication**: v1 → v2 migration is a top-line requirement, not a future-feature bullet. Triggers documented (compliance change, second tenant, alarm trip).
  - **Risk**: Cross-tenant pollution by misconfigured AWS principal is possible in v1. Documented in operator runbook.
- **Multi-tenant logical isolation** (Q4 answered B, revised from A). Caller-scoped S3 keys, shared worker pods. Confirmed by user on 2026-05-11. **Note (2026-05-11):** isolation reduces to data-layout convention in v1 under Q6 = X; true isolation lands in v2.
  - **Implication**: IAM principal → tenant ID mapping at orchestrator API edge; tenant ID flows through every job record, S3 key, log, metric, trace.
  - **Implication**: S3 key layout: `s3://<bucket>/<tenant-id>/<job-id>/...`. Bucket policy denies cross-tenant prefix access; presigned URLs scoped to requesting tenant.
  - **Implication**: Per-tenant quotas at submit endpoint (concurrent jobs, jobs/hour, storage). Structured 429 on quota exhaustion.
  - **Implication**: Cache scope (Q13) locked to per-tenant namespace on 2026-05-11. Cross-tenant content-addressable cache is a probing oracle; opt-in flag deferred to future scope but key layout is forward-compatible (`cache/<tenant_id>/...`).
  - **Implication**: DLQ + failed-jobs S3 prefix (Q18) and CloudWatch metric dimensions (Q17) are tenant-scoped.

## Execution Plan Summary

- **Stages to Execute**: Application Design, Functional Design, NFR Requirements, NFR Design, Code Generation, Build and Test (6 remaining).
- **Stages to Skip**: Reverse Engineering (greenfield), User Stories (single-user local PoC), Units Generation (single package), Infrastructure Design (Dockerfile only, folded into Code Generation).

## Stage Progress

### 🔵 INCEPTION PHASE

- [x] Workspace Detection — Greenfield, design doc present
- [x] Reverse Engineering — SKIPPED (greenfield)
- [x] Requirements Analysis — Complete 2026-05-11; `requirements.md` approved by user
- [x] User Stories — Complete 2026-05-11 (retroactively added per user request); personas.md + stories.md generated, awaiting approval
- [x] Workflow Planning — Complete 2026-05-11; `plans/execution-plan.md` generated, awaiting user approval
- [x] Application Design — Complete 2026-05-11; 5 artifacts generated, awaiting user approval
- [ ] Units Generation — SKIP (single Python package, single Docker image)

### 🟢 CONSTRUCTION PHASE

- [x] Functional Design — Complete 2026-05-11 (unit: `office-converter`); 3 artifacts generated, awaiting user approval
- [x] NFR Requirements — Complete 2026-05-11 (C++ pivot reflected); 2 artifacts generated, awaiting user approval
- [x] NFR Design — Complete 2026-05-11; 2 artifacts generated, awaiting user approval
- [ ] Infrastructure Design — SKIP (just Dockerfile, folded into Code Generation)
- [x] Code Generation — Complete 2026-05-11 (Part 1 plan + Part 2 execution); all 20 steps executed; awaiting user approval
- [x] Build and Test — Complete 2026-05-11; 5 instruction docs + summary generated under `construction/build-and-test/`, awaiting user approval

### 🟡 OPERATIONS PHASE

- [ ] Operations — PLACEHOLDER. **Design reference**: [`operations/eks-production-topology.md`](operations/eks-production-topology.md) captures the intended EKS production topology, pod model, IAM, scaling, failure handling, and gap-to-implementation. Authored 2026-05-12. Not yet executed as a formal OPERATIONS stage.

## Current Status

- **Lifecycle Phase**: INCEPTION
- **Lifecycle Phase**: CONSTRUCTION
- **Current Stage**: **WORKFLOW COMPLETE** — all in-scope stages approved (2026-05-11)
- **Next Stage**: OPERATIONS (placeholder; out of v1 scope)
- **Status**: AI-DLC v1 deliverables complete. Operator owns: real Aspose SDK + license, multi-stage Docker build, test execution, security checks. See `construction/build-and-test/build-and-test-summary.md` for the operator's ordered next-step list.

## Post-AI-DLC Production Integration (2026-05-12)

Following the SKU pivot refinement above, the production worker was actually wired against the 4-libs vendor path and run end-to-end. Status:

### What works end-to-end

| Format | HTTP status | Output | Notes |
| --- | --- | --- | --- |
| **DOCX → PDF** | 200 | Valid PDF v1.7, ~21 KB for small.docx | Page-range subsetting via `Aspose::Words::Saving::PageSet` |
| **PPTX → PDF** | 200 | Valid PDF v1.7, ~15 KB for simple.pptx | Page-range subsetting via slide-index array export (`Presentation::Save(path, slides[], SaveFormat::Pdf, opts)`). Planner now chunks PPTX normally (carve-out lifted 2026-05-13). |
| **PDF → PDF** | 200 | Valid PDF v1.3, ~5 KB for simple.pdf | Page-range subsetting via page extraction into new Document (`Pages::Add(page)`). Planner now chunks PDF normally (carve-out lifted 2026-05-13). Fast-path: full-document range saves directly without copy. |
| **XLSX → PDF** | 200 | Valid PDF v1.7, ~52 KB single_sheet, ~18 KB multi_sheet | **License resolved 2026-05-12**: root cause was a missing `Aspose::Cells::Startup()` call before `License::SetLicense()`. Cells's `Initializer.h` documents Startup() as required; the other three Aspose products have no equivalent init contract. Skipping Startup() left OpenSSL (used by Cells's RSA license validator) uninitialized, producing the misleading `code=24 encoding` error. Fix: `apply_license()` in `worker_cpp/formats/xlsx.cpp` calls `Aspose::Cells::Startup()` before `SetLicense()` and registers `Cleanup(true)` via `std::atexit`. **Page-range slicing implemented 2026-05-13** (motivating case: 94 MiB / ~1 M-row `sample_sales_data.xlsx` wedged in a single chunk past the 600s chunk timeout): render path uses `Aspose::Cells::PdfSaveOptions::SetPageIndex/SetPageCount` (0-based; the orchestrator's 1-based `RenderArgs` are converted on entry); probe uses `Aspose::Cells::Rendering::WorkbookRender::GetPageCount()` to emit a real page count. Planner carve-out for XLSX lifted; orchestrator applies a per-format `max_pages_per_chunk` floor (`Settings.xlsx_min_pages_per_chunk`, default 1500) because each Cells subprocess pays a fixed `Workbook.Load` + full-workbook pagination cost before rendering its slice, so chunks must be coarse enough to amortize that overhead. **Horizontal-slicing fix 2026-05-13** (motivating case: `student_marks_with_charts.xlsx`, a 14 KB dashboard XLSX whose `pageSetup orientation="portrait"` produced 2 PDF pages with the Total column and both charts vertically sliced): `configure_natural_pagination` in `xlsx.cpp` now sets `AllColumnsInOnePagePerSheet(true)` on both `PaginatedSaveOptions` and `ImageOrPrintOptions`. Columns scale to fit one page width on every sheet; rows still paginate vertically. `OnePagePerSheet` deliberately stays `false` because that flag silently drops rows on 1M-row sheets. Side-effect: `sample_sales_data.xlsx` page count dropped 70,911 → 23,637 (more rows per page at the scaled-down column width), so the planner now emits 16 chunks instead of 48 and full-conversion wall time falls ~108 min → ~36 min at parallel=2. Applies to all XLSX variants (.xlsx, .xls, .xlsm, .xlt, ...) via the same worker. |

### How v1 dispatches: per-product worker binaries (post-2026-05-12 ABI fix)

Aspose ships:
- **Words for C++ 26.3** (one release behind the rest)
- **Cells / Slides / PDF for C++ 26.4**

Each carries its own copy of `libcodeporting.translator.cs2cpp.framework_x86_64_libstdcpp_libc2.23.so` with the SAME SONAME but different versions. The previous single-worker build linked all four products into one binary; the dynamic linker resolved to one CodePorting at startup and Cells's plain-C++ `Workbook` constructor then crashed when invoked alongside Words/Slides state. PPTX tolerated the mismatch (Slides 26.4 forgives the older 26.3 framework); Cells did not.

**v1 implementation**: Four worker binaries — `office-convert-worker-{docx,pptx,xlsx,pdf}` — each linking exactly one Aspose product (plus that product's own CodePorting framework where relevant). The Python orchestrator (`aspose_worker._run_worker`) resolves the binary path as `f"{settings.worker_binary_prefix}-{format}"`. No two CodePorting versions ever coexist in the same process, so the address-space collision is structurally impossible. `apply_license` no longer takes a format argument — the binary's compiled-in product is the format. The format CLI arg is retained for orchestrator compatibility and validated on entry; mismatches return `input_unprocessable`.

Trade-offs: ~600 MB additional image footprint (each binary drags its own product .so subtree); subprocess startup unchanged (each binary loads only one product, so dynamic-linker work per spawn drops vs. the previous mega-binary). Each product gets its matching CodePorting framework back — Words 26.3 + cs2cpp 26.3, Slides 26.4 + cs2cpp 26.4, PDF 26.4 + its bundled cs2cpp, Cells 26.4 (plain C++ — no framework). The "Modules versions mismatch!" runtime warnings the old build emitted are gone.

### Production refactor — files changed 2026-05-12 (initial 4-libs vendor wiring)

- `worker_cpp/CMakeLists.txt` — 4 `find_package(CONFIG)` calls + manual IMPORTED target for PDF (no CMake config shipped). Per-product RPATH list in worker binary. Tier-1 perf flags preserved.
- `worker_cpp/license.cpp` — real Aspose `License::SetLicense()` calls per product. ASCII→u16 conversion for Cells's `char16_t*` API.
- `worker_cpp/probe.cpp` — real probe via Aspose APIs (page count only; natural seams empty in v1).
- `worker_cpp/formats/docx.cpp` — page-range render via `PageSet` (the only format that honors --page-range correctly).
- `worker_cpp/formats/{pptx,xlsx,pdf}.cpp` — full-document save (v1 limitation, page-range argv ignored).
- `Dockerfile` — multi-stage builder consumes `vendor/aspose/{W,C,S,P}/` instead of `aspose-total-cpp.tar.gz`; runtime stage copies per-product `.so` subdirs to `/opt/aspose/<Product>/`.
- `Makefile` — `check-sdk`/`verify-sdk` replaced with `check-vendor`/`verify-vendor`. `VENDOR_DIR` variable replaces `SDK_TARBALL`. `make convert` `-i` bug fixed (HTTP headers no longer leak into output PDF).
- `office_convert/server.py` — format detection moved AFTER body buffering (was 512-byte prefix; OOXML Content-Types is typically in the central directory near end-of-file; fix is now full-file zip inspection capped at 64 KB).

### Per-product worker split — files changed 2026-05-12 (later same day; ABI fix)

The single `office-convert-worker` was split into four `office-convert-worker-{docx,pptx,xlsx,pdf}` binaries to eliminate the CodePorting framework SONAME collision that was breaking XLSX:

- `worker_cpp/CMakeLists.txt` — rewritten around an `add_aspose_worker()` function that emits one executable per product using manual IMPORTED targets (no shared `find_package` state means each binary's link line is fully isolated). Per-product `INSTALL_RPATH`. Words gets its own CodePorting 26.3 back (the old build had been pinned to Slides' 26.4 to suppress warnings).
- `worker_cpp/{license,render,probe}.cpp` — deleted. The entry points (`apply_license`, `dispatch_render`, `dispatch_probe`) are now defined inside each `formats/<fmt>.cpp` so each translation unit pulls exactly one product's headers. No more cross-product header soup in one TU.
- `worker_cpp/{license,render,probe}.h` — slimmed to declarations only. `apply_license` no longer takes a `format` arg (the binary identity is the format).
- `worker_cpp/probe_util.h` — new header-only helper for the probe JSON shape + file_size_bytes, shared by all four format files (no Aspose deps).
- `worker_cpp/main.cpp` — calls `apply_license(license_path)` without a format param; per-binary `dispatch_*` validates the `--format` arg against compiled-in product.
- `Dockerfile` — builds and installs all four binaries; restores Words' bundled CodePorting 26.3 `.so` tree (which the single-binary build had skipped to dedup with Slides 26.4).
- `office_convert/config.py` — `worker_binary` → `worker_binary_prefix` (the suffix is appended per request).
- `office_convert/aspose_worker.py` — `_run_worker` resolves the binary as `f"{prefix}-{format}"`.
- `office_convert/server.py` — health check verifies all four per-format binaries exist via `ACCEPTED_FORMATS`.
- `tests/conftest.py` + per-test fixtures — fake worker is now written to four `<prefix>-<fmt>` paths so the orchestrator's per-format dispatch resolves.

### Verification matrix (2026-05-12)

- ✅ `make verify-vendor` — all 4 trees Linux x86_64 ELF, CMake configs present (PDF excepted as expected).
- ✅ `make build` — production image builds cleanly.
- ✅ `make test` — 103 passed / 1 skipped (no regression).
- ✅ `make up` + `make health` — `{"ready": true, "license_days_remaining": 361, ...}`.
- ✅ `make qa` — ruff 0 errors, ruff format 0 changes, mypy 0 issues.
- ✅ DOCX, PPTX, PDF conversions return valid PDFs via HTTP (verified post-split 2026-05-12).
- ✅ cs2cpp ABI collision resolved structurally: PPTX worker no longer emits "Modules versions mismatch!" warnings; each binary loads only one product's `.so` set.
- ❌ XLSX still fails — but at a *different* layer than before: `Aspose::Cells::License::SetLicense` rejects the Aspose.Total umbrella license with `ExceptionType::Internal` code=24 message=`encoding`. Reproduces with all three documented SetLicense overloads + UTF-8-explicit XML prolog. Independent of the cs2cpp split, distinct from the prior Workbook-constructor crash. See audit.md 2026-05-12T10:55:43Z entry for the investigation trace; root cause + fix tracked separately.

### What's NOT yet updated (deferred)

- `construction/office-converter/code/code-summary.md` SDK layout description still references old single-tarball path.
- `construction/office-converter/nfr-requirements/tech-stack-decisions.md` Aspose SKU rationale not yet rewritten.
- `construction/build-and-test/build-instructions.md` operator steps still reference `make verify-sdk` / `aspose-total-cpp.tar.gz`.
- `README.md` SDK acquisition section still references the old single-tarball flow.
- C++ `--mode=pool` implementation for the worker process pool (Python side ready in `office_convert/worker_pool.py`).
- Benchmarking with real large files to validate the adaptive chunk sizing speedup numbers.

These are documentation updates and follow-up implementation that don't affect the runtime; deferred to focused passes.

### Legacy Office format support (2026-05-13)

`/convert` now accepts pre-2007 binary Office inputs (OLE2/CFB):
`.doc/.dot`, `.xls/.xlt/.xlm`, `.ppt/.pot/.pps`. Detection happens in
`office_convert/probe.py::detect_format()` — OLE2 magic
`\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` triggers a head-scan for
UTF-16LE stream-name signatures (`WordDocument`, `Workbook` or
`Book`, `PowerPoint Document`); the uploaded multipart filename's
extension is the fallback when no signature matches. Stream
signature wins over filename. Legacy inputs route to the
existing per-product workers — Aspose.Words / Aspose.Cells /
Aspose.Slides load both binary and OOXML through one constructor,
so no new workers or vendor changes are needed. See
`construction/office-converter/functional-design/business-rules.md`
§7.1 for the full routing table and `audit.md` (2026-05-13T06:15:00Z)
for the change log.

### Performance Improvement Strategy (2026-05-13)

Six-strategy performance optimization pass addressing slow conversion of large files:

1. **Adaptive chunk sizing** — `chunk_planner.adaptive_max_pages()` computes optimal pages-per-chunk per request based on file size, page count, format amplification, and RAM budget. Replaces the static `max_pages_per_chunk=10` default. Config value (now 200) acts as ceiling; OOM subdivision remains the safety net.

2. **Increased parallelism** — `OFFICE_CONVERT_PARALLEL` raised 2→4. More chunks render simultaneously.

3. **Cache (future improvement)** — `office_convert/cache.py` is fully implemented. Enable by setting `OFFICE_CONVERT_CACHE_DIR=/cache` and mounting a volume. Deferred until conversions are verified stable end-to-end.

4. **PPTX page-range slicing** — `pptx.cpp` rewritten to export only requested slides via `Presentation::Save(path, slideIndexArray, SaveFormat::Pdf, opts)`. Planner carve-out removed.

5. **PDF page-range slicing** — `pdf.cpp` rewritten to extract requested pages via `Pages::Delete()` (removing pages outside the range). Planner carve-out removed.

6. **Worker process pool (ACTIVE)** — C++ workers support `--mode=pool` (stdin/stdout JSON protocol). Document loads once, renders all chunks from memory. Enabled by default. Verified working: 8.5 MB PPTX went from ~337s (one-shot) to **11.6s** (pool). Set `OFFICE_CONVERT_POOL_MODE=0` to disable.

**Additional fixes (2026-05-13)**:
- **Fontconfig crash fix**: Added `fontconfig` + `fonts-dejavu-core` to Dockerfile, `fc-cache -f` at build time, `/var/cache/fontconfig` tmpfs + `HOME=/tmp` in compose.
- **Exact PPTX probe**: `probe_lite` counts `ppt/slides/slide*.xml` entries in the ZIP for exact slide count (microseconds, no Aspose).
- **Size-based probe fallback**: When OOXML metadata is missing, estimates pages from file size (instant) instead of falling through to 15+ minute Aspose probe.
- **XLSX chunk floor lowered**: 1500→500 pages/chunk for better parallelism on large workbooks.

**Verified performance**:
| Test | Time |
|------|------|
| PPTX 8.5 MB (28 slides) | 11.6 sec |
| DOCX 42 KB (1 page) | 0.6 sec |
| DOCX 5.8 MB (44 pages) | 9.6 sec |
| XLSX 10 MB (2501 pages) | ~10 min |
| XLSX 14 KB (1 page) | 0.14 sec |
| Probe (any format) | <0.01 sec |

**Additional fixes (2026-05-13, iteration #3)**:
- **Auto re-planning**: Pool workers report actual page count → orchestrator re-plans chunks if estimate was wrong. Works for all formats automatically.
- **Stale metadata detection**: DOCX `app.xml` saying 1 page but file >200 KB → falls back to size estimate.
- **Format mismatch auto-retry**: When worker rejects file with format hint → retries with correct worker automatically.
- **OLE2 scan expanded**: 65KB→512KB scan window. Collects all signatures, priority: Word > PowerPoint > Excel.
- **Pool load timeout**: 120s→600s for large documents.
- **Access log silenced**: `--no-access-log` in uvicorn CMD.
- **Streamlit Test UI**: `test_ui.py` + `Dockerfile.ui` with live stats, background conversion, download history with time taken.
- **Architecture diagram**: `aidlc-docs/construction/office-converter/architecture-diagram.md` with Mermaid flowcharts.

### Container memory ceiling: bumped then reverted (2026-05-14 → 2026-05-15)

Mid-session bump: `mem_limit: 4g → 8g`, `memswap_limit: 6g → 12g`, `OFFICE_CONVERT_WORKER_RAM_BYTES: 6 GiB → 12 GiB`, after `sample_stress_test_100mb.docx` triggered OOM under the legacy N-independent-workers pool model — 4 parallel Aspose.Words loads of the same 100 MB DOCX hit 6-8 GB peak, overflowing the 4 GB ceiling.

**Reverted on 2026-05-15** back to `mem_limit: 4g` + `memswap_limit: 6g` + `OFFICE_CONVERT_WORKER_RAM_BYTES=6 GiB`. The revert is safe because **fork-after-load is now the default pool model** — fork's copy-on-write sharing keeps peak RAM at ~1× the loaded Document instead of N×. Verified peak ≤ 3 GB on the 100 MB DOCX stress files (`sample_100mb.docx` 64 pages, `sample_large_100mb.docx` 505 pages — both converted successfully under the restored 4 GB ceiling).

This means the Hard Constraint at the top of this file (`4 GB physical + 2 GB swap = 6 GB total`) is now accurate again. Don't raise these without also raising `OFFICE_CONVERT_WORKER_RAM_BYTES` to match `memswap_limit`.

### Observability: worker heartbeat dashboard (2026-05-14)

Pool-mode load and render were previously opaque from outside the process: the orchestrator's only signal was "stdout JSON response" or "600s timeout." For a 100 MB DOCX whose load took >10 min, the 600s window was indistinguishable from a deadlock.

**What was added:**

- **C++ heartbeat thread** (`worker_cpp/pool.cpp`): every load and render command wraps a `Heartbeat` RAII guard that spawns a background thread emitting one JSON line/2s (configurable) to stderr — `{"type":"heartbeat","pool_index":N,"phase":"load|render","elapsed_s":N,"rss_bytes":N,"swap_bytes":N,"cpu_jiffies":N}`. Reads `/proc/self/status` for `VmRSS` + `VmSwap` (swap is load-bearing under `memswap_limit`) and `/proc/self/stat` for utime+stime jiffies.

- **Python stderr tailer** (`office_convert/worker_pool.py`): per-worker `asyncio.Task` drains stderr concurrently with the stdout protocol, parses heartbeat JSON, forwards to the structured logger at DEBUG and to the heartbeat store. Non-heartbeat stderr (Aspose warnings) passes through as a warning.

- **Per-request heartbeat store** (`office_convert/heartbeats.py`): new module. Thread-safe bounded deque per request_id (5000 entries, 30 min TTL). Keyed on the orchestrator's full 32-char `X-Request-ID`.

- **GET /jobs/{request_id}/heartbeats endpoint** (`office_convert/server.py`): returns the recorded trail. UI polls every 1s during active conversion.

- **UI heartbeat panel** (`test_ui.py`): live per-pool-index table under the "⏳ Converting" callout — phase, elapsed-in-phase, RSS in MB, **Swap in MB (highlighted orange when non-zero)**, CPU jiffies, time since last heartbeat. Green dot if heartbeat ≤6s old, orange if stale. UI uses its own UUID as `X-Request-ID` so the panel correlates 1:1 with the in-flight conversion.

- **Pool-min-chunks knob** (`Settings.pool_min_chunks`, default 2): replaces the historical hard-coded `len(plan.chunks) > 1` gate in `orchestrator.py`. Set to 1 via `OFFICE_CONVERT_POOL_MIN_CHUNKS=1` to force pool mode (and thus the heartbeat panel) on every conversion — useful for exercising the dashboard on small files.

- **Heartbeat cadence knob**: `OFFICE_CONVERT_HEARTBEAT_MS` (default 2000). `0` disables heartbeats entirely.

- **Log level**: heartbeat events emit at DEBUG, not INFO, so production logs stay quiet. The dashboard remains the canonical surface; bump `OFFICE_CONVERT_LOG_LEVEL=debug` for greppable post-mortem trails.

**Files**: `worker_cpp/pool.cpp`, `office_convert/{heartbeats.py,worker_pool.py,server.py,config.py,orchestrator.py}`, `test_ui.py`, `compose.yaml`.

### Fork-after-load: load-once-render-many (2026-05-14)

**Motivating failure:** `sample_stress_test_100mb.docx` (107 MB DOCX, real page count 64) reproducibly hit the 600s `pool_load_timeout`. Heartbeats confirmed the workers were alive and grinding the entire 600s — not deadlocked, just contending. Root cause: `WorkerPool._spawn_workers` fires 4 worker processes in parallel via `asyncio.gather`, each independently loading the same 107 MB DOCX into its own Aspose.Words `Document`. Linear contention: 4 × disk reads of the same file, 4 × Aspose parse work fighting for CPU cores, 4 × memory growth competing for the 8 GB RAM ceiling. Per-worker load time scaled near-linearly with pool size — for this file, 4-way contention pushed each load over 600s.

**Design:** one **leader** process loads the document. After load completes (and `get_PageCount` triggers full pagination), the leader `fork()`s `pool_size-1` child renderers. On Linux, `fork()` copies the page table and marks pages copy-on-write; as long as children only read from the loaded `Document` (which rendering does), physical RAM stays at ~1× the loaded size instead of N×. Total processes: 1 leader + N-1 children, all sharing the same parsed Document via COW.

**C++ implementation** (`worker_cpp/pool.cpp::pool_loop_forked`):
- License + load happen in the leader as today.
- After load, leader creates N-1 `socketpair`s, `fork()`s once per pair.
- Each child: closes leader's end of all socketpairs except its own, closes stdin/stdout (children never talk to the orchestrator directly), sets `g_pool_index = i`, enters `child_render_loop` reading line-delimited JSON commands from its socketpair.
- Leader runs a `poll()` loop multiplexing stdin + N socketpair fds. Render commands carry a `seq` integer; leader picks a free child (or itself if all children busy), writes the command to that child's socketpair, reads the response, forwards verbatim to stdout.
- On `quit` or stdin EOF: send quit to each child, `waitpid` cleanup.
- Each child's own heartbeats tag themselves with `pool_index` so the shared stderr pipe is demultiplexable.

**Python implementation** (`office_convert/worker_pool.py::ForkedPoolLeader` + `ForkedWorkerPool`):
- Spawns a single subprocess with `--pool-size N` instead of N independent subprocesses.
- `ForkedPoolLeader` holds a seq counter and a `dict[int, Future]` of pending responses. A persistent `asyncio.Task` reads stdout line-by-line, parses each JSON response, looks up the seq, and resolves the matching future.
- Multiple concurrent `render_chunk` calls run as before via `asyncio.gather`; each allocates a unique seq, sends its command on the shared stdin, awaits its future.
- Same external interface as `WorkerPool` (async context manager + `render_chunk`) so the orchestrator can pick between models with a one-line dispatch.

**Orchestrator gate** (`office_convert/orchestrator.py`): `Settings.fork_after_load` (default `False`, shell-overridable via `OFFICE_CONVERT_FORK_AFTER_LOAD=1`) selects `ForkedWorkerPool` over `WorkerPool`. Emits `dispatch_mode mode=pool_fork` for visibility.

**Verified results** (`req_bfd7edbb`, 2026-05-14):

| | Before (req_42776db1) | After (req_bfd7edbb) |
|---|---|---|
| File | `sample_100mb.docx` (107 MB) | same |
| Plan | 27 chunks (size-based estimate) | 27 chunks → replanned to 11 after load |
| Load phase | **timeout at 600s — failed** | **14 s** |
| Total wall time | failed | **28.8 s** |
| Memory | 4 × ~117 MB = 468 MB (small.docx baseline) | 152 MB leader + 28-30 MB × 3 children ≈ 238 MB (small.docx baseline) |
| Aspose internal thread state after fork() | n/a | survived — full conversion completed without deadlock |

**Risk partially materialized:** `fork()` inside a process holding heavily-multithreaded native code (Aspose's `CodePorting.Translator.Cs2Cpp.Framework` spawns its own threads during library init and document load) is canonically fragile — only the calling thread survives in the child, and any locks held by orphaned threads in the parent are still "held" from the kernel's perspective. On DOCX (Aspose.Words) and PPTX (Aspose.Slides) the threads survived cleanly. **On XLSX (Aspose.Cells) it crashed** — see the next section "XLSX crash" for the per-format allowlist that resolved it. **Default flipped to ON on 2026-05-15** (`Settings.fork_after_load = True`, compose default `OFFICE_CONVERT_FORK_AFTER_LOAD=1`) — this is what made the 4g/6g RAM revert safe. Set `OFFICE_CONVERT_FORK_AFTER_LOAD=0` for a global kill switch; XLSX is automatically opted out at the format-aware gate regardless of this flag.

**Files**: `worker_cpp/{main.cpp,pool.h,pool.cpp}`, `office_convert/{worker_pool.py,orchestrator.py,config.py}`, `compose.yaml`.

### Fork-after-load: per-format allowlist after XLSX crash (2026-05-15)

Broader testing on the first XLSX run under fork mode surfaced the risk the previous section flagged. **Aspose.Cells does not survive `fork()`.**

**Crash trace** (`req_2c92692a`, 2026-05-15):

```
18:51:35  request_received          format=xlsx  source=sample_sales_data.xlsx (98 MB)
18:51:35  probe_start
18:52:13  probe_complete            duration_s=37.26  page_count=23637
18:52:13  plan_complete             chunks=48  pool_size=4
18:52:13  dispatch_mode             mode=pool_fork  worker=xlsx
18:52:13  fork_pool_spawn           pid=952  pool_size=4  worker=xlsx
18:52:45  fork_pool_loaded          page_count=23637            ← load OK in leader
            ↓ (no further log lines from this process)
            RenderError: render failed (exit=1): worker stdout EOF
```

The leader successfully completed `pool_load` (Cells's `Workbook` parsed + paginated) and returned the page count. Then on the first `{"cmd":"render","seq":1}` after fork, the leader **closed stdout without writing a response and without flushing any stderr** — the signature of a SIGSEGV / SIGABRT in the parent process touching post-fork-broken native state.

**Why Cells specifically:**
- Cells requires an explicit `Aspose::Cells::Startup()` call before `License::SetLicense()` (documented contract, load-bearing fix from the 2026-05-12 license issue above). Startup() initializes OpenSSL state for the RSA license validator and spawns internal Cells worker threads.
- After `fork()`, the child inherits the parent's memory pages but **only the calling thread**. The internal Cells worker threads cease to exist in the child; any mutex they were holding is now permanently "held" from the kernel's perspective.
- In the leader, the post-fork state itself is broken because Cells's internals weren't designed to live across the call. The first render attempt triggers an internal touch that hits an invalid state and dies.
- Words and Slides have no `Startup()` contract and survived fork() on every file tested. PDF is currently in the allowed set but unverified.

**Resolution** (`office_convert/worker_pool.py`):

```python
_FORK_UNSAFE_FORMATS: frozenset[FormatName] = frozenset({"xlsx"})

def fork_after_load_enabled(settings: Settings, format: FormatName) -> bool:
    if format in _FORK_UNSAFE_FORMATS:
        return False
    return bool(getattr(settings, "fork_after_load", False))
```

XLSX automatically falls back to the legacy `WorkerPool` (N independent subprocesses, each loading the workbook independently — slower but stable). DOCX, PPTX, and PDF continue to use fork-after-load.

**Per-format fork verification status:**

| Format | SDK | Fork-safe? | Verified on |
|---|---|---|---|
| DOCX | Aspose.Words | ✅ verified | 64-page + 505-page 100 MB stress files; 100-page 17 MB enterprise file |
| PPTX | Aspose.Slides | ✅ verified | 31-slide enterprise_suite.pptx |
| XLSX | Aspose.Cells | ❌ **fork-unsafe** | crashed on 98 MB / 23,637-page sample_sales_data.xlsx (req_2c92692a) — added to `_FORK_UNSAFE_FORMATS` |
| PDF | Aspose.PDF | ⚠️ allowed but unverified | needs a stress PDF; add to deny-list if it crashes the same way |

**Future investigation** (deferred): a clean fix for XLSX under fork would require re-initializing Cells in each child after fork — call `Aspose::Cells::Startup()` post-fork plus possibly re-creating the `Workbook` object from a serialized state. Whether that's even possible with Cells's current API is unclear. For now, XLSX uses the legacy pool and pays the N× load cost that fork was designed to eliminate. Acceptable because: (a) XLSX render-per-chunk is the dominant cost, not load (each chunk renders the full workbook anyway under the current Cells page-range API); (b) XLSX-heavy workloads are less common than DOCX in the operator's workload mix.

**Files**: `office_convert/worker_pool.py` (`_FORK_UNSAFE_FORMATS` + format-aware `fork_after_load_enabled`), `office_convert/orchestrator.py` (call-site passes `format`).

### Known follow-ups (introduced 2026-05-14, not yet done)

- **Cache write directory robustness**: the cache module writes to `/cache/<tenant>/{final,chunks}/` and crashes with `FileNotFoundError` if those subdirs are missing. The write path should `mkdir -p` on demand. Surfaced during a routine `find /cache -delete` cleanup.
- **Pre-probe**: size-based probe estimate was off by 83× on `sample_100mb.docx` (estimated 5345 pages, real 64). Re-plan after load corrects the chunk count, but pool size is already sized for the bad estimate. A pre-probe in a single worker before sizing the pool would let small-page-count documents take the single-worker path.
- **PDF fork verification**: PDF still in the fork-allowed set but never tested under fork. First PDF crash with `worker stdout EOF` should trigger adding `"pdf"` to `_FORK_UNSAFE_FORMATS`.
- **XLSX-under-fork investigation**: see above — would require post-fork Cells re-init. Currently deferred; XLSX uses legacy pool.

### Dev cluster deployment + ingress decision (2026-05-18)

Live deployment on `DEV05-EKS-CLUSTER`, namespace `office-convert-dev`, helm release `office-convert`. Both API + UI pods Running. NLB hostnames provisioned via `aws-load-balancer-scheme: internal` (VPC-only). Image tag `be4ac93-uifix1` (after the 2026-05-18 UI OOM fix bundle: limits 512Mi → 1.5Gi, docker-socket-guarded `_docker_monitor`, removed `curl get.docker.com` from `Dockerfile.ui`).

**Reachability constraint, confirmed permanent (2026-05-18)**: NLB private IPs in `10.35.0.0/16` are unreachable from the operator's laptop. The corp FortiClient VPN gives `/32` routes for the EKS API endpoint (kubectl works) but does NOT tunnel VPC data-plane CIDRs. Verified by `ip route add 10.35.0.0/16 via 192.168.8.24 dev fctvpndc0b79cc` probe — packets enter the tunnel, hit corp HQ, get dropped because corp's server-side routing has no path to the VPC. **Server-side VPC peering does not exist**; the "add route" workaround is permanently dead.

**Decision: ALB Ingress + ACM TLS** (mirrors `argocd/argocd-http-ingress`'s shape — only proven external-ingress precedent in this cluster). Path 1: **two Ingresses sharing one ALB** via `alb.ingress.kubernetes.io/group.name: office-convert`, **subdomain routing** (over path-routed, because Streamlit-behind-prefix is painful). Final hostnames:

- **UI**: `office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com`
- **API**: `office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com`

Both single-label under the existing wildcard cert `*.dev05.k8s.opus2dev.com` (`arn:aws:acm:eu-west-1:537462380503:certificate/fab42f33-7d67-4ecf-b200-38af584485b0`, ISSUED). Route 53 zone `dev05.k8s.opus2dev.com.` is `Z045669519R5D9D8CKC79`. No external-dns installed — DNS records will be created manually post-Helm-deploy.

**Rejected alternatives**: (B) Istio Ambient + Ingress Gateway — adds machinery no other app in cluster uses, doesn't fix reachability any better; (C) NLB scheme flip to `internet-facing` + `loadBalancerSourceRanges` — fastest but plaintext HTTP, new hostnames every redeploy, no app-layer surface for future Cognito/OIDC; (Path 2 one-Ingress path-routed) — Streamlit `STREAMLIT_SERVER_BASE_URL_PATH` + websocket idle_timeout + static asset rewrites; (multi-label hostnames) — would need a new ACM cert.

**Implementation deferred to next session**. Helm chart change list, pre/post-deploy verifications, and reversibility notes captured in `aidlc-docs/operations/dev-deployment-topology.md`. Operator-memory landing pad: `reference_alb_ingress_plan.md` in the auto-memory store.

**Gotcha for the next operator**: argocd's own Ingress references an **expired** cert ARN (`213a9222-0466-4e0f-9ca2-87e92c92944c`). Do not copy that ARN when cloning argocd's annotation shape. Use the wildcard `fab42f33`.

**Argocd's corp inbound-cidrs snapshot 2026-05-18** (10 entries; verify with network admin before reusing): `213.210.23.82/32, 213.210.23.84/32, 31.121.79.58/32, 31.121.79.60/32, 18.133.115.188/32, 54.91.4.210/32, 18.168.253.57/32, 52.74.117.130/32, 165.65.37.128/29, 136.40.11.230/32`.

### Port-forward wrapper verified clean (2026-05-18)

`deploy/scripts/portforward.sh` re-tested end-to-end on 2026-05-18 from clean state. Both API (18080) and UI (8501) port-forwards bound on base ports, `/health` and `/_stcore/health` returned 200, kubectl processes stayed alive. The prior session's "UI didn't bind 8502 within 5s" failure did NOT reproduce — forensic read on `deploy/logs/portforward-ui.log` shows kubectl had actually printed `Forwarding from 127.0.0.1:8502 -> 8501` (port WAS bound); the prior failure was a VPN flap that killed kubectl's upstream tunnel after a successful bind, and `start_one()`'s "didn't bind in 5s" tail-log path emitted the misleading error.

**Latent script bug** (deferred, not blocking): `start_one()` conflates "never bound" with "bound then upstream died" — both report "didn't bind within 5s". Fix would track whether `ss -tlnH` ever showed the port up inside the wait loop and emit a distinct error like "bound but kubectl process died — VPN/RBAC?" when it had. Low priority because the actionable cause (check VPN) is the same.

### ALB Ingress cutover complete (2026-05-19)

The forward plan in `dev-deployment-topology.md` §6 landed end-to-end on 2026-05-19 in three commits:

- **`37f01c0`** — added `templates/ingress.yaml` (two Ingresses sharing `group.name: office-convert`), `values.yaml` `ingress:` block, plus `deploy/scripts/route53-{upsert,delete}.sh` wired into `Makefile` deploy step 7/8 and undeploy step 1/4. ALB provisioned alongside the dormant NLBs — zero-impact additive step. Wildcard cert `fab42f33` (`*.dev05.k8s.opus2dev.com`), 10-CIDR corp allowlist, 300s idle timeout for Streamlit websocket, per-Ingress healthcheck paths (`/_stcore/health` UI, `/health` API).
- **`33ba4c6`** — both `Service` resources flipped `LoadBalancer → ClusterIP`. AWS LBC deprovisioned the NLBs within ~60s of `helm upgrade`. ALB is now the sole ingress surface. `git revert` brings NLBs back without disturbing the ALB.
- **`3cbc332`** — docs alignment: Makefile step counters + ALB URLs in post-deploy block, `deploy/README.md`, and `dev-deployment-topology.md` updated to match the as-built 8-step deploy / 4-step undeploy pipeline.

**As-built final hostnames** (single-label under the existing wildcard cert):
- UI: `office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com`
- API: `office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com`

**Verification matrix** (live 2026-05-19, pre-undeploy): DNS resolves to 3 ALB IPs; valid TLS chain on wildcard cert; `GET /_stcore/health` → `200 OK "ok"` (~620 ms); `GET /health` → `200 OK {"ready":true,"license_days_remaining":354,...}` (~630 ms); curl from non-allowlisted IP TCP-times-out after 8 s (SG drop confirmed).

### Office VPN CIDR commit-then-revert (2026-05-19)

Aditya's 4 office VPN egress CIDRs (`114.143.153.146/32`, `114.143.153.147/32`, `103.68.11.58/32`, `103.68.11.59/32`) were live-patched onto the running ALB Ingresses via `kubectl annotate`. Commit `05bcbe2` then persisted them into `values.yaml` `ingress.inboundCidrs` so they'd survive `make deploy-dev` redeploys. Reverted the same day in `9345f30` per operator preference: personal/office IPs are NOT chart artifacts. Use the live `kubectl annotate` pattern (or a future `values-dev.yaml` overlay) instead. The personal home-ISP CIDR (`103.53.234.52/32`) was never committed — rotates with DHCP. Only the 10-CIDR argocd-lineage corp allowlist lives in the chart.

### Upload-cap + UI memory hardening (2026-05-19)

Two coordinated bumps so the UI handles inputs up to the API ceiling:

- **`897dc1e`** — `STREAMLIT_SERVER_MAX_UPLOAD_SIZE` raised 200 MiB → 1024 MiB to match the API's `OFFICE_CONVERT_MAX_INPUT_BYTES=1 GiB`. Both plumbed through `values.yaml`. Streamlit's 200 MiB default was silently rejecting large files before they reached the API. Pydantic `Field(le=1*1024*1024*1024)` caps any env-var override at 1 GiB.
- **`fd5b595`** — UI pod memory limit `1.5Gi → 4Gi` (request `512Mi → 1.5Gi`). Verified OOMKill (`exitCode 137`) on a 398 MiB XLSX upload caused by Streamlit's ~3× peak-memory profile during multipart parse + base64 transport. Node capacity verified before bump (c5ad.2xlarge nodes ~15 GiB allocatable, ~43% committed). API pod resources unchanged — inputs > ~250 MiB may still OOM mid-conversion on the API side (no swap on K8s).

### Cross-env CPU/RAM tiles via cgroup-backed /stats (2026-05-19, `f56481b`)

Replaced the UI's `docker stats` / `docker top` subprocess path with HTTP `GET /stats` + `GET /workers` against the API. API reads `/sys/fs/cgroup` (cgroup v1+v2 auto-detect) and `/proc/[pid]/cmdline` inside its own container — works identically on Docker compose and EKS pods. The `/var/run/docker.sock`-exists gate is gone, so CPU/RAM tiles populate on EKS for the first time (K8s pods use CRI runtimes, not Docker, and mounting the host socket into a pod is a security non-starter).

New module: `office_convert/container_stats.py`. New routes in `server.py`. UI's `_docker_monitor` rewritten to consume HTTP JSON; CPU% computed from cumulative-usec deltas. Loop cadence 1.5 s → 1 s (HTTP GET is no longer the bottleneck).

Compose-side companion: added `STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false` + `STREAMLIT_SERVER_ENABLE_CORS=false` to mirror the EKS chart — newer Streamlit versions enforce XSRF strictly and were silently 403-ing the `/upload_file` endpoint locally, making the file_uploader appear broken.

### Pool mode forced ON by default + format-aware skeleton placeholders (2026-05-19, `ffb86d9`)

`Settings.pool_min_chunks` default flipped `2 → 1` in three places (Pydantic, `compose.yaml`, chart `values.yaml` + `api-configmap.yaml`). Single-chunk conversions now take the pool dispatch path that emits heartbeats. Previously the default left small files on the one-shot path with no telemetry, and the dashboard Memory chart stayed empty even for valid conversions. Cost: ~1-2 s fork+load overhead per conversion — acceptable for the dev cluster.

`test_ui.py`'s Time-per-stage + Chunk Gantt charts now show format-aware empty-state messages instead of silently-stuck-empty mystery space when the active conversion is a format that doesn't emit timing events (at this commit only XLSX did; `77781df` later closed that gap).

### UI dashboard polish: bounded history, delete button, skeletons (2026-05-19, `3db61fa`)

- `st.session_state.history` capped at `MAX_RECENT_RESULTS=20` (matches the process-wide store ceiling). Each entry holds the full output PDF in `item["data"]`; without the cap, 50× 50 MB conversions would push the UI pod past its memory limit.
- Per-history-row 🗑️ delete button replaces the redundant col3 timestamp (time already in col1). Per-session delete only — process-wide `s["results"]` store untouched so other sessions still see the entry until rotation ages it out. `seen_result_ids` retains the deleted id to suppress re-display on script rerun.
- `uploaded_file.size` swapped for `len(uploaded_file.getvalue())` — the old call copied the full byte buffer just to compute the size string, O(file_size) work on every script rerun between drop and click-Start.
- `_build_empty_chart()` helper renders a dark-grid Plotly figure with centered "Awaiting conversion data" annotation matching the live-chart theme (`height=210`). `live_charts()` no longer early-returns when there's no active job; pre-first-conversion the Mega Row shows 3 chart skeletons matching the Kubernetes-dashboard "instrument ready, awaiting telemetry" aesthetic. Per-chart graceful degradation: after a conversion, if one builder returns None (e.g., timing data missing), only that chart stays as the placeholder while the others show data.

### Per-format pool_load/pool_render timing events (2026-05-19, `77781df`)

Three changes that together populate the dashboard's Time-per-stage + Chunk Gantt charts for DOCX/PPTX/PDF (previously XLSX-only):

1. New `worker_cpp/timing_util.h` — shared `emit_timing_ms()` + `emit_render_summary()` helpers. `xlsx.cpp` keeps its own anonymous-namespace copy untouched (zero-risk; identical body); a future de-dup pass is deferred.
2. `docx.cpp` / `pptx.cpp` / `pdf.cpp` `pool_load` + `pool_render` now wrap their load + pagination + render stages with `emit_timing_ms()` plus a summary event mirroring xlsx.cpp's shape. PDF gets an extra `pool_render.delete_pages` stage because its render path mutates pages per chunk.
3. `office_convert/worker_pool.py::ForkedPoolLeader._handle_stderr_line` gained the missing `{"type":"timing"}` branch — the legacy `WorkerPool` always had this; the forked variant didn't. Without this, the new C++ events were emitted correctly but Python dropped them on the floor for DOCX/PPTX/PDF. **Gotcha**: timing JSON parsing is now split across `ForkedPoolLeader` (DOCX/PPTX/PDF — fork-after-load path) and `WorkerPool` (XLSX — legacy pool path). New event types need branches in BOTH parsers.

Verified per-format event counts: docx 4 (`document_load`, `pagination`, `save`, `summary`); pptx 4 (`presentation_load`, `slide_count`, `save`, `summary`); pdf 5 (`probe`, `document_load`, `delete_pages`, `save`, `summary`); xlsx 5 (regression check — still works).

### Dockerfile CVE patch (2026-05-19, `0cf9f43`)

ECR BASIC scan on tag `77781df` flagged 26 OS-level CVEs per image (2 CRITICAL, 14 HIGH, 8 MEDIUM, 2 LOW). All inherited from `python:3.12-slim-bookworm` / `debian:bookworm` base layers — gnutls28, glibc, systemd, dpkg, krb5, libcap2, expat, libxml2, libgcrypt20, sed — none of which we install directly. Base images never get patched after their tag is published.

Added `apt-get upgrade -y` between `apt-get update` and `apt-get install` in all three apt stages (`Dockerfile` builder, `Dockerfile` runtime, `Dockerfile.ui`). Picks up the latest Debian Bookworm point-release fixes for everything in the base layer. Cost: +20–40 s build time + ~30–100 MB image size per stage. Cleared ~54-58% of the CVE list. Remaining are upstream-unfixed (gnutls28, expat, libxml2).

Python-dep CVEs (pandas/plotly/streamlit on UI; aiofiles/fastapi/etc. on API) not covered — would require ECR scan flipped from BASIC to ENHANCED (Inspector v2). Deferred.

### Dev cluster undeployed for cost (2026-05-19T23:04)

`make undeploy-dev` ran cleanly: `route53-delete.sh` removed both A-aliases (~60 s propagation), `helm uninstall` deprovisioned the ALB (~60 s), license `Secret` + namespace deleted. ECR image `0cf9f43` retained (cost ~$0.10/mo). Saves ~$18/mo ALB cost while the cluster isn't being actively dogfooded. Redeploy:

```bash
IMAGE_TAG=0cf9f43 make deploy-dev
```

Current dev state: **UNDEPLOYED** at the end of 2026-05-19. Cluster credentials, Helm chart, ECR image, and operator memory all intact for fast redeploy.

### Dev cluster redeployed (2026-05-20T10:30 IST)

Brought the cluster back up on the same image `0cf9f43`. Operator-side Docker Desktop was unavailable, so the canonical `make deploy-dev` path couldn't execute its build+push steps (3-4). Since the ECR image was already present from the 2026-05-19 push (digests: API `sha256:6e50b9b6…`, UI `sha256:d12f06d8…`), the build is wasted work — ran Makefile steps 5-8 directly:

1. `kubectl create namespace office-convert-dev` + license Secret apply.
2. `helm upgrade --install ... --wait --timeout 5m` → completed in ~30 s, Helm rev 1 (prior undeploy cleared release history).
3. `./deploy/scripts/route53-upsert.sh` → ALB hostname `k8s-officeconvert-921b81ff67-1648401858.eu-west-1.elb.amazonaws.com` populated in ~30 s, both A-aliases UPSERT'd (Route 53 change `/change/C053553214DQ5801DCTFA`).
4. Post-deploy: both pods 1/1 Ready, end-to-end curl from laptop returned `200 OK` on both `/health` and `/_stcore/health` after live-patching `36.255.185.84/32` (current home ISP — rotated from `103.53.234.52/32` within <24 h) onto both Ingresses atomically. Subsequently added the 4 office VPN CIDRs (`114.143.153.146/32`, `114.143.153.147/32`, `103.68.11.58/32`, `103.68.11.59/32`) the same way.

**Final live allowlist**: 15 CIDRs (10 chart corp + 1 home ISP + 4 office VPN). The 5 non-chart CIDRs are live-patched only — lost on next undeploy/redeploy cycle.

**Confirmed via routing probe**: FortiClient was connected (interface `fctvpn184dd436` today; the suffix rotates per session, so scripts must NOT hardcode it). Even so, all 3 ALB public IPs (`34.255.138.245`, `52.208.35.24`, `99.80.38.211`) routed via wifi (`192.168.31.1 dev wlp0s20f3`), not the tunnel — split-tunnel as documented. The 10 corp CIDRs in the allowlist do NOT match Aditya's laptop traffic to the ALB; the entry that made `curl` work is `36.255.185.84/32`.

### UI polish ship pile (2026-05-20 mid-afternoon, 3 commits)

After the morning's redeploy and live-patches stabilized the deployment, the rest of the session focused on UI improvements driven by an enumerated menu of 12 items. 10 shipped, 2 declined.

Three commits on `aspose-upgrades-v2` (pushed to origin):

- **`a3f006f`** fix: cache mkdir on demand + always render chart skeletons. Two latent bugs: (a) `Cache.final_temp_path()` returned a Path without ensuring parent dir exists — qpdf crashed `FileNotFoundError` mid-stream when cache vol was empty (local only; dev05 has cache disabled so unaffected). (b) `live_charts()` call site still gated on `_snap_active or _snap_results`, defeating commit `3db61fa`'s "skeletons always render" intent — dropped the `elif` so `live_charts()` always runs.
- **`d0ca782`** feat: `DELETE /cache` endpoint + `CacheManager.clear()`. Wipes the on-disk conversion cache. Returns `{enabled, files_deleted, bytes_freed, errors}`. On EKS the response is `{enabled: false}` because the chart doesn't set `OFFICE_CONVERT_CACHE_DIR`. Verified locally end-to-end: cleared 289 files / 7.15 GB.
- **`d206642`** feat(ui): dashboard polish + cache/history controls. 15 features added to `test_ui.py` (+758 lines): equalizer bars replacing static `⏳` + LIVE dot, sparklines in CPU/RAM util-cards (30-sample ring buffer, y-axis pinned 0..100), worker-row cyan pulse when CPU > 0.5%, slide-in toast on completion (state machine with `toast_shown_ids` + `toast_active.expires_at`), 🧹 Clear-all history button, 🔍 history filter, 🔄 Re-run latest (preserves input bytes on s["results"][0] only), 🗑️ Clear-cache button (hits `DELETE /cache`), skeleton shimmer on empty Plotly slots (HTML fallback because Plotly canvas can't host CSS animations — see memory `reference_plotly_css_limit.md`), hover glow on KPI tiles + status pills, License countdown progress bar, format icons (📄/📊/📈/📕) in history rows, Lifetime KPI tile (cumulative count + bytes_in/out since process start), per-format performance summary panel (count + avg + p95 per format).

Items declined from the menu: **#12 cancel in-flight conversion** (heaviest, needs backend cooperation — signal handling for pool workers + new `DELETE /jobs/{request_id}` endpoint).

### Image swap to `d206642` on dev05 via `kubectl set image` (2026-05-20T14:55 IST)

**Method**: image-only roll via `kubectl set image` rather than the canonical `make undeploy-dev && make deploy-dev`. Rationale: full undeploy resets the live ALB allowlist to the 10 chart CIDRs only, losing the 5 live-patched ones (`36.255.185.84/32` home + 4 office VPN). Image-swap preserves everything except the pod ReplicaSets.

Sequence:
1. ECR login (`aws ecr get-login-password | docker login`).
2. `docker build -t office-convert:dev .` — fully cache-hit since local image was warm from the morning's rebuilds. ~5 s.
3. `docker tag office-convert:dev <ECR>/office-convert:d206642` + `docker push`. ~10 s.
4. `docker build -t office-convert:ui -f Dockerfile.ui .` — cache-hit. ~5 s.
5. `docker tag office-convert:ui <ECR>/office-convert-ui:d206642` + `docker push`. ~5 s.
6. `kubectl set image -n office-convert-dev deploy/office-convert office-convert=<ECR>/office-convert:d206642`
7. `kubectl set image -n office-convert-dev deploy/office-convert-ui office-convert-ui=<ECR>/office-convert-ui:d206642`
8. `kubectl rollout status` — both deploys successfully rolled out (~35 s API, ~32 s UI).

Verification (from laptop, traffic via local ISP per split-tunnel):
- API `GET /health` → 200, `{"ready":true,"license_days_remaining":353,...}`
- UI `GET /_stcore/health` → 200
- New `DELETE /cache` route reachable: returns `{"enabled":false,...}` on dev05 — confirms d206642 code is running AND that the cache is correctly disabled (no `OFFICE_CONVERT_CACHE_DIR` in chart).

Image digests in ECR (tag `d206642`, account 537462380503, region eu-west-1):
- API: `sha256:eece63482ea0fbe5624ad1921d68bcbe4b07be4aec2602da628ac68378074d46`
- UI:  `sha256:3d50dfc572814210582de91cf43a166e7c17d5a26f09a703dde2b88e56ec8e91`

### Helm release vs live state divergence (2026-05-20T14:55 IST)

After the kubectl set image swap, attempted `helm upgrade --reuse-values --set image.tag=d206642 --set ui.image.tag=d206642` to record a Helm rev matching live state. **The upgrade FAILED** with a Server-Side-Apply field-manager conflict:

```
conflict with "kubectl-annotate" using networking.k8s.io/v1:
  .metadata.annotations.alb.ingress.kubernetes.io/inbound-cidrs
```

Root cause: when the operator (earlier in this session) ran `kubectl annotate` to add the 5 non-chart CIDRs onto the Ingress, kubectl registered itself as the field manager for that annotation. Modern helm (3.13+) uses Server-Side Apply and refuses to overwrite fields owned by another manager — defense against accidental cross-controller stomping.

Net state:
- **Helm rev 1**: `deployed`, May 20 10:30, `image.tag: 0cf9f43` (initial values).
- **Helm rev 2**: `failed`, May 20 14:45, SSA conflict during Ingress apply. BUT `helm get values` returns rev 2's values (`tag: d206642`), so the stored values dict IS updated despite the failed apply.
- **Live state**: pods on `d206642` (from step 6-7 above), 15-CIDR allowlist intact, both endpoints HTTP 200.
- **Ingress field managers**: `helm` (rev 1 ownership of chart-rendered fields), `controller` (AWS LBC reconciler, ×2 for HTTP/HTTPS listeners), `kubectl-annotate` (our 5 live-patches).

**Gotcha shift**: the original "naked `helm upgrade` would silently downgrade to 0cf9f43" risk is RESOLVED (rev 2's stored values say d206642). A NEW failure mode took its place: any helm operation that re-applies the Ingress will hit the same SSA conflict and fail. **`make deploy-dev IMAGE_TAG=anything` is now blocked** at the `helm upgrade --install` step until field-manager ownership of `inbound-cidrs` is reconciled — only the full undeploy+deploy path (which deletes the Ingresses and clears all field managers) restores helm reconcilability.

**Recommended posture going forward**:
- **Image-only rolls** → `kubectl set image` (bypasses helm; preserves live allowlist).
- **Chart changes** (env vars, resource limits, ingress shape) → `make undeploy-dev && make deploy-dev`, then re-annotate the 5 non-chart CIDRs (5 minutes of allowlist rebuild — same workflow as 2026-05-19T23:04 → 2026-05-20T10:30).
- **Long-term fix** (still an open question, raised on 2026-05-19): a gitignored `values-dev.yaml` overlay containing the 4 office CIDRs, sourced via `helm install -f values-dev.yaml`. That would bake them back into the chart-render pass so only the personal home ISP needs live-patching post-deploy. Memory note: this just got more pressing because every chart-change deploy now triggers the SSA conflict path until field managers are reset.
