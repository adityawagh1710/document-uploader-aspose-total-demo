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
| XLSX 10 MB (2501 pages) | ~16 min |
| Probe (any format) | <0.01 sec |
