# Application Design Plan — Office Converter (Local v1)

## Purpose

High-level component identification and interface design for the local
v1 Office→PDF converter. Detailed business logic (chunk-planning
algorithm specifics, subdivision policy, etc.) is deferred to
Functional Design in the Construction phase.

## Plan Checklist

- [ ] Collect answers to design questions (this document)
- [ ] Analyze answers for ambiguities; ask follow-ups if needed
- [ ] Generate `application-design/components.md`
- [ ] Generate `application-design/component-methods.md`
- [ ] Generate `application-design/services.md`
- [ ] Generate `application-design/component-dependency.md`
- [ ] Generate consolidated `application-design/application-design.md`
- [ ] Validate design completeness and consistency
- [ ] Present completion message and wait for approval

## Design Questions

Please answer each `[Answer]: PROCEED — use recommended default` tag. Default I'll use if you say "proceed" is
shown in parentheses after each option set.

---

### Q1 — Aspose integration mechanism

The design doc names **Aspose.Total C++**. From Python we need some way
to invoke it. Three viable paths:

A) **Aspose.Total for Python via .NET** (pythonnet) — Aspose's official
   Python distribution wraps the .NET implementation; widely used,
   pip-installable, runs on Linux via Mono or .NET runtime.
B) **Aspose.Total C++ + custom C++ render binary** — write a small
   `aspose-render-chunk` C++ executable that links Aspose.Total C++,
   takes args (input path, page range, output path), exits with
   documented codes. Python subprocesses it.
C) **Aspose.Total C++ via pybind11 / FFI** — direct binding from Python
   into the C++ library, in-process. (Rejected by Q2 = A in the v1
   scope: subprocess isolation needed for RAM ceiling enforcement, so
   this would still need to fork a child process anyway.)

**Recommendation (proceed default): A — Aspose.Total for Python (.NET-backed).**

**Rationale:** Aspose's "Total C++" SKU is technically distinct from
"Total for Python via .NET", but for the local v1's purposes the
Python distribution is functionally equivalent: same APIs, same
license file format, same memory characteristics (since the .NET
runtime backs both). Importantly, it's pip-installable, removes the
need for a separate C++ build pipeline, and the subprocess isolation
required by Q2 = A is achieved by Python spawning a worker Python
script (not a C++ binary). If you specifically have a license for
Aspose.Total C++ and not the Python distribution, switch to B; the
license type determines this answer.

[Answer]: PROCEED — use recommended default

---

### Q2 — Component slice / project layout

How should the Python package be sliced?

A) **Layered**: `api/` (FastAPI), `orchestrator/` (job logic),
   `workers/` (subprocess wrappers), `lib/` (chunk planner, qpdf
   wrapper, cache).
B) **Flat modules**: a single package directory with files —
   `server.py`, `orchestrator.py`, `chunk_planner.py`,
   `aspose_worker.py`, `qpdf.py`, `cache.py`, `license.py`,
   `logging.py`.
C) **Hexagonal / ports-and-adapters**: `domain/` (chunk planner,
   pure logic), `adapters/` (Aspose, qpdf, filesystem, HTTP),
   `app/` (composition).

**Recommendation (proceed default): B — Flat modules.**

**Rationale:** v1 is small (8-12 files). Flat modules keep imports
shallow and discoverable. Layered (A) imposes structure that pays off
at 30+ files but is ceremony at 10. Hexagonal (C) is the right shape
if we expect many adapter swaps (e.g. mock Aspose for tests) — but
PBT and the FastAPI `TestClient` cover testing without needing a
ports/adapters abstraction. Can refactor to A or C later if v2 cloud
work grows the surface.

[Answer]: PROCEED — use recommended default

---

### Q3 — HTTP handler async model

FastAPI supports both `async def` and `def` handlers. The chunk-render
subprocess calls are inherently blocking from Python's perspective
unless explicitly async.

A) **`async def` handler + `asyncio.create_subprocess_exec`** — true
   concurrent subprocess management, single event loop, per-job
   `asyncio.Semaphore` for parallel chunk renders.
B) **`def` handler + `subprocess.Popen`** — FastAPI runs `def`
   handlers in a thread pool. Each request is one thread. Per-job
   parallelism via `concurrent.futures.ProcessPoolExecutor`.

**Recommendation (proceed default): A — `async def` + `asyncio.subprocess`.**

**Rationale:** Cleaner concurrency model for the per-job parallelism
inside a single request, integrates naturally with FastAPI's request
lifecycle, and gives us `asyncio.timeout` for free if we later add
per-job timeouts. The thread-pool approach (B) works but stacks two
concurrency mechanisms (FastAPI threads × ProcessPoolExecutor) which
makes the worst-case-RAM calculation messier.

[Answer]: PROCEED — use recommended default

---

### Q4 — License manager lifecycle

How does the License component activate Aspose's `SetLicense()`?

A) **Once at server startup**, store license object globally, expose
   `days_remaining()` and `is_expired()` helpers; per-request the
   `/convert` handler checks `is_expired()` and returns 503 if so.
B) **Per chunk-render subprocess** — each worker subprocess loads the
   license fresh from the bind-mounted path before doing any Aspose
   work.
C) **Both** — server-level check for fast-fail on `/health` and
   `/convert`; worker subprocess re-applies the license because
   subprocess-loaded state doesn't carry over.

**Recommendation (proceed default): C — both layers.**

**Rationale:** Aspose's `SetLicense()` applies to the process, not the
file system. A subprocess that doesn't call `SetLicense()` runs in
evaluation mode and produces watermarked output. So we MUST call it
in every worker subprocess (B). The server-level check (A) is the
fail-fast: when expiry is reached, the server returns 503 immediately
without spawning workers. C combines both: server validates expiry
date once at startup (and refreshes on file change if the operator
drops a new `.lic`), per-worker subprocess re-applies the license
during its render. Validates the file path is correct on startup.

[Answer]: PROCEED — use recommended default

---

### Q5 — Cache layer placement

The optional filesystem cache (FR-7) caches both final-output PDFs and
per-chunk PDFs. Where does the cache integration live?

A) **Inside the orchestrator** — orchestrator checks cache before
   probing/planning (final-output) and before dispatching each chunk
   render (per-chunk).
B) **Decorator pattern around worker invocations** — `cached_render()`
   wraps `aspose_render()`; the orchestrator doesn't know caching
   exists.
C) **Separate cache service** — orchestrator calls `cache.get()` /
   `cache.put()` explicitly.

**Recommendation (proceed default): C — separate cache module with
explicit get/put.**

**Rationale:** Cache semantics need to be observable (`X-Cache-Hits`
response header per FR-1) and bypassable (per-request `cache: false`
per FR-7). A decorator (B) hides this. The orchestrator owning cache
calls (A) couples two concerns. A separate `cache` module with
explicit calls keeps the orchestrator readable and makes the cache
unit-testable in isolation.

[Answer]: PROCEED — use recommended default

---

### Q6 — Aspose version in cache key

When Aspose is upgraded, the cache from the old version may produce
subtly different output for the same source. Should cache keys
include the Aspose version?

A) **Yes** — key = `sha256(source) || aspose_version` so an Aspose
   upgrade naturally invalidates the cache without manual cleanup.
B) **No** — keep keys content-only; operators manually clear the
   cache on Aspose upgrades.

**Recommendation (proceed default): A — include Aspose version.**

**Rationale:** Operators forget. Including the version (e.g. "v24.6")
in the cache key path (`cache/<aspose_version>/final/<sha256>.pdf`)
makes Aspose upgrades safe by default, with no extra ops procedure.
Cost is trivial: old version directories are abandoned until the
operator deletes them, no functional impact.

[Answer]: PROCEED — use recommended default

---

### Q7 — Temp file / scratch directory

Where do per-request scratch files live (uploaded input buffered to
disk, chunk PDFs before merge, Aspose `TempFolder` spill)?

A) **`/tmp/office-convert/<request_id>/`** — per-request directory,
   cleaned up at end of request (success or failure).
B) **Operator-configurable via `OFFICE_CONVERT_SCRATCH_DIR`** —
   default `/tmp/office-convert/`, override for hosts where `/tmp`
   is undersized or backed by tmpfs.
C) **Inside the cache directory** — re-use `OFFICE_CONVERT_CACHE_DIR`
   when set; fall back to `/tmp` when not.

**Recommendation (proceed default): B — configurable env var, default
`/tmp/office-convert/`, per-request subdirectory.**

**Rationale:** On real hosts `/tmp` can be tmpfs (RAM-backed!), which
would defeat the purpose of Aspose's TempFolder spill. Operator needs
the ability to point this at a real disk. C (cache dir) conflates
distinct lifetimes — cache is durable, scratch is per-request. A
hardcoded path is the right default but should be overridable.

[Answer]: PROCEED — use recommended default

---

### Q8 — Probe mechanism

To plan chunks, we need page count and structural metadata (Word
sections, PPT slide ranges, Excel sheets, PDF page ranges). How is
probing implemented?

A) **Via Aspose** — lightweight Aspose call (`Document.open()` +
   metadata read) without full render. Uses Aspose's
   `LoadOptions::TempFolder` + `MemoryOptimization` to bound RAM.
B) **Format-specific lightweight probes** — for OOXML formats
   (DOCX/PPTX/XLSX), parse the zip + relevant XML for metadata. For
   PDF, use `pdfinfo` (poppler) or qpdf's `--show-npages`.
C) **Hybrid** — format-specific where it's reliable (PDF, simple
   DOCX); Aspose where it's needed (PPTX with complex media, XLSX
   with large sheets).

**Recommendation (proceed default): A — Aspose probe with memory
options.**

**Rationale:** Aspose probe is the simplest path: same library
already loaded for rendering, no extra dependency, handles all four
formats uniformly. Format-specific probes (B) add code and edge
cases. Hybrid (C) is the right answer if Aspose probe turns out to
be too expensive in real testing — but that's a NFR-design-time
optimization, not a v1 starting decision. Ship A; reserve C if probe
duration becomes a measured problem.

[Answer]: PROCEED — use recommended default

---

### Q9 — Worker subprocess implementation

Following Q1 = A (Aspose.Total for Python), what runs in the chunk-
render subprocess?

A) **Python script `worker_main.py`** — invoked as `python -m
   office_convert.worker --input ... --pages ... --output ...`;
   imports Aspose, applies license, renders, exits.
B) **A separate console-script entry point** — `office-convert-worker
   --input ...` via `pyproject.toml` `[project.scripts]`; identical
   semantics, cleaner invocation.

**Recommendation (proceed default): B — console-script entry point.**

**Rationale:** Cleaner subprocess invocation (no `python -m`
indirection), shorter command lines (helps `prlimit` invocation
readability), supports stable argument parsing via the same typer/argparse
that the main package uses. Cost: one more entry in `pyproject.toml`.

[Answer]: PROCEED — use recommended default

---

### Q10 — qpdf integration

qpdf is a separate native binary (not a Python library). How does the
Python package call it?

A) **`subprocess.run(["qpdf", "--empty", "--pages", ...])`** —
   shell-out to the qpdf binary on PATH (provided by the Docker
   image).
B) **`pikepdf`** — Python bindings to qpdf; in-process, no subprocess
   needed.

**Recommendation (proceed default): A — shell-out to qpdf binary.**

**Rationale:** The streaming merge requires qpdf's CLI behavior
(write merged PDF to stdout, which we then stream to the HTTP
response body via chunked transfer encoding). pikepdf (B) is great
for in-process PDF manipulation but it builds the merged document
in memory before writing — which violates NFR-1 (no full output
buffered in memory). A is the only option compatible with streaming
merge.

[Answer]: PROCEED — use recommended default

---

### Q11 — Configuration source

What's the single source of truth for runtime configuration (max-jobs,
parallel, cache-dir, license-path, log-format, log-level, scratch-dir,
etc.)?

A) **Environment variables only**, prefixed `OFFICE_CONVERT_*`. CLI
   flags shadow env vars where uvicorn needs them.
B) **TOML/YAML config file** mounted into the container, env vars
   override.
C) **CLI flags only** — passed to uvicorn at container `CMD`.

**Recommendation (proceed default): A — env vars (with optional
shadowing by uvicorn CLI flags).**

**Rationale:** 12-factor: env vars are the standard for
containerized apps. No file to mount, no parse step, easy to set
via `docker run -e`. Pydantic-settings handles validation and
defaults cleanly. A config file (B) is right for >20 settings; we
have ~7. CLI-only (C) makes scripted deployment fragile.

[Answer]: PROCEED — use recommended default

---

## Free-form

### Q12 — Anything else for the design?

Components, patterns, or constraints I should know before producing
`components.md` and the rest of the design artifacts?

[Answer]: PROCEED — use recommended default

---

**When you're done**, reply "answered" (or just paste the file back)
or "proceed" to use my recommendations for any blank answers, and
I'll analyze for contradictions and generate the design artifacts.
