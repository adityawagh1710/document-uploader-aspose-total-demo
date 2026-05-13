# Functional Design Plan — office-converter (Local v1)

## Purpose

Detailed business logic for the single unit `office-converter`. Builds
on Application Design (high-level components + interfaces) by
specifying the *algorithms*, *business rules*, and *domain model*
the components will implement. Technology-agnostic — implementation
patterns (asyncio specifics, FastAPI specifics) belong in NFR Design.

## Plan Checklist

- [ ] Collect answers to functional design questions (this document)
- [ ] Analyze answers for ambiguities; ask follow-ups if needed
- [ ] Generate `office-converter/functional-design/business-logic-model.md`
- [ ] Generate `office-converter/functional-design/business-rules.md`
- [ ] Generate `office-converter/functional-design/domain-entities.md`
- [ ] (Skip `frontend-components.md` — no UI)
- [ ] Present completion message and wait for approval

## Design Questions

Each question has a recommended "proceed default" answer with
rationale. Reply with `[Answer]:` tag values, or say "proceed" to
lock all defaults.

---

### Q1 — Memory cost estimation function

The chunk planner bounds chunks by "50 MB estimated rendered size".
What formula estimates rendered MB from input metadata?

A) **Pages × per-format constant**:
   `est_mb = pages × {docx: 0.5, pptx: 5.0, xlsx: 2.0, pdf: 1.0}`.
   Simple, format-aware, ignores per-document variance.
B) **Input size pro-rated by page**:
   `est_mb = (input_size_bytes / page_count) × pages × amplification(format)`
   where `amplification` is `{docx: 5, pptx: 8, xlsx: 4, pdf: 2}`.
   Uses actual document weight; better for outlier pages.
C) **Hybrid**: use B as the primary estimate, clamp to a minimum
   of A so a small but heavy document still triggers chunking.

**Recommendation (proceed default): C — hybrid.**

**Rationale:** B is more accurate on average but underestimates a
1-MB DOCX with a 50-MB embedded image on page 5. A floor (option A's
constant) catches that case. The over-estimation cost (smaller
chunks than necessary) is acceptable; the under-estimation cost
(OOM at render time → subdivision retry → wall-time hit) is what
we're trying to avoid.

[Answer]: B

---

### Q2 — Natural-seam policy per format

The hybrid split strategy uses "natural seams when balanced, fall
back to page-range otherwise". What seams per format, and what's
the balance test?

A) **Use seams when largest resulting chunk ≤ 1.5× target**;
   otherwise page-range. Seams per format:
   - DOCX: section breaks
   - PPTX: slide ranges (every chunk is a slide range; PPTX has no
     "section" concept that maps cleanly)
   - XLSX: sheets (one chunk per sheet, OR contiguous groups of
     sheets up to the size bound)
   - PDF: page boundaries only (no semantic seams to use)
B) **Always use page-range for all formats** (ignore seams).
   Simpler, deterministic, no balance test.
C) **Format-specific policies hand-tuned per format** (defer
   exact rules to NFR/measurement).

**Recommendation (proceed default): A — seam-with-1.5×-balance-test.**

**Rationale:** The design doc's hybrid policy is the right shape;
the 1.5× threshold is a defensible default. PDFs have no useful
seams so always go page-range. XLSX sheets are the most natural
boundary because cross-sheet rendering involves no shared
formatting state in Aspose. PPTX slide ranges of arbitrary length
are fine since each slide is independent. DOCX section breaks are
the cleanest cross-format equivalent but rare in practice — most
DOCXs have one section and fall through to page-range.

[Answer]: A

---

### Q3 — Subdivision algorithm

When a chunk OOMs at N pages, how is it subdivided?

A) **Binary halving**: split into ⌈N/2⌉ and ⌊N/2⌋ pages. Recurse
   on each. Termination floor: single page.
B) **Quartering**: split into 4 equal-ish ranges. Faster
   convergence to single-page if the OOM is on a specific page.
C) **Adaptive**: if N ≥ 4, halve; if N = 2 or 3, split to all
   single pages at once.

**Recommendation (proceed default): A — binary halving.**

**Rationale:** Simplest, deterministic, terminates predictably
(log₂(10) ≈ 4 levels max for a 10-page chunk). Quartering (B) is
faster in the worst case but multiplies parallel-dispatch
overhead. C is the same as A in steady state. PBT will verify
termination and determinism (NFR-6).

[Answer]: A

---

### Q4 — License-expiry state machine

The license has three observable states (>7 days, ≤7 days, ≤1 day,
expired). Map them to log levels and `/health` impact.

A) **Three states + post-expiry**:

| Days remaining | Log level on `/convert` | `/health.ready` |
| -------------- | ----------------------- | --------------- |
| > 7            | DEBUG once per request  | true            |
| 4 to 7         | WARN once per request   | true            |
| 1 to 3         | ERROR once per request  | true            |
| 0 (= today)    | ERROR + `/health.ready: false` | false    |
| Expired (past today) | request fails 503 license_expired | false |

B) **Two states**: only WARN below 7 days, fail when expired.
   Simpler, less noise.
C) **Operator-configurable thresholds** via env vars.

**Recommendation (proceed default): A — three states with the table above.**

**Rationale:** Operators need progressive warnings on a 30-day
temp license. A WARN-only model (B) doesn't differentiate "noticed
last week" from "urgent — expiring tomorrow"; useful in alerting
rules. Configurable thresholds (C) are over-engineered for v1.
The "≤ today" → not-ready is intentional: gives ops a clean
signal to renew before requests fail.

[Answer]: A

---

### Q5 — Failure-class taxonomy refinement

Application Design defined the failure→HTTP map. Now map Aspose
exception types to the failure classes:

A) **Use Aspose exception type names where possible**:

| Failure class                  | Aspose exception (Python)                          |
| ------------------------------ | -------------------------------------------------- |
| `unsupported_format`           | format detected at server before Aspose call       |
| `input_unprocessable`          | `FileCorruptedException`, `IncorrectPasswordException`, `UnsupportedFileFormatException` |
| `render_failed`                | any other `AsposeException` subclass not above     |
| `subdivision_floor_exceeded`   | OOMError raised by orchestrator after subdivide(1-page) fails |
| `license_expired`              | `InvalidLicenseException` after license_manager.is_expired() |
| `merge_failed`                 | qpdf process exit != 0                             |

B) **Treat everything Aspose throws as `render_failed`** (simpler,
   less precise diagnostics).
C) **Define our own exception hierarchy in `types.py` and
   translate at the worker boundary** (decouples from Aspose's
   exception names which may shift across versions).

**Recommendation (proceed default): C — own hierarchy, translate at
worker boundary.**

**Rationale:** Coupling our diagnostic taxonomy to Aspose's exception
class names is fragile — Aspose's Python bindings expose .NET
exceptions, which have versioned names that have changed across
releases. The worker subprocess translates whatever Aspose throws
into our own exception types (with exit codes per the
component-methods.md contract), and the orchestrator-side
`aspose_worker` raises typed Python exceptions from those codes.
A's table becomes documentation; the *code* depends on our own
hierarchy.

[Answer]: PROCEED

---

### Q6 — Cache atomicity protocol

When writing to the filesystem cache, how is partial-write
visibility prevented?

A) **Write to `<final-path>.tmp.<pid>.<uuid>` then `os.rename`
   to `<final-path>`.** POSIX rename is atomic within a
   filesystem. Readers see either the old file or the new file,
   never a partial one.
B) **Write to a staging directory under the cache, then `rename`
   into the canonical location.** Same atomicity guarantees;
   slightly more cleanup work.
C) **No atomicity protocol** — accept that a crashed mid-write
   leaves a corrupt cache entry, document operator must purge.

**Recommendation (proceed default): A — temp-file + rename, in the
same directory.**

**Rationale:** Standard atomic-write pattern. Reader-writer
correctness without locks. Cleanup of orphaned `.tmp.*` files is
trivial: any `.tmp.*` file older than ~1 hour is from a dead
writer and can be deleted by an operator cron (not required in
v1, but documented). C is too cavalier — a corrupt cache file
produces a corrupt download, which violates "fail loudly".

[Answer]: PROCEED

---

### Q7 — Concurrency edge case: hung render

What happens if a chunk render subprocess hangs (doesn't exit, no
OOM, just stuck)?

A) **Hard timeout per chunk render**: 5 minutes. On timeout,
   kill the subprocess (SIGTERM, then SIGKILL after 5 s), treat
   as render failure, subdivide and retry.
B) **No timeout**: rely on the overall HTTP request timeout
   (caller's responsibility per NFR-4).
C) **Configurable**: env var `OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS`,
   default 300.

**Recommendation (proceed default): C — configurable, default 300s.**

**Rationale:** A is the right behavior; C makes it tunable for
operators with unusual workloads (very large single chunks legitimately
take >5 min). Hung renders are rare but real (Aspose has been
observed to deadlock on certain XLSX files with circular references);
without a timeout, a single bad input wedges a worker slot
indefinitely. The default of 300s is generous — most chunks render
in single-digit seconds.

[Answer]: PROCEED

---

### Q8 — ProbeResult.natural_seams content per format

What does `natural_seams: list[tuple[int, int]]` actually contain
per format?

A) **List of (start_page, end_page) ranges defining natural
   chunks** per format. Empty list = no useful seams (PDF, single-
   section DOCX).
   - DOCX: ranges between section breaks
   - PPTX: empty (every slide is its own seam; chunk planner
     handles this case directly from `page_count`)
   - XLSX: ranges representing sheets (sheet 1 = pages 1-N₁,
     sheet 2 = pages N₁+1 to N₁+N₂, etc.)
   - PDF: empty
B) **Always empty** — chunk planner ignores `natural_seams`
   entirely (matches "always use page-range" if Q2 = B).
C) **Format-specific richer model** with a separate `seams_by_kind`
   dict (e.g. PPTX has both slide and section-master boundaries).

**Recommendation (proceed default): A.**

**Rationale:** Consistent with Q2 = A. Empty list for formats
without useful seams is cleaner than a separate flag. PPTX gets
empty because slides are uniform enough that the chunk planner
can derive boundaries from page count alone. Format-aware
extensions (C) are over-design for v1.

[Answer]: PROCEED

---

### Q9 — Cache write trigger

When does the cache write happen for the final-output cache?

A) **Never** — only per-chunk caching is useful in practice
   because final-output cache only hits if the exact same source
   is submitted again. Drop final-output cache.
B) **Always, by buffering the merged PDF to disk first, then
   streaming the disk file to the HTTP response.** Defeats the
   streaming-merge property (NFR-1 says output never buffered).
C) **Only when the response succeeds end-to-end** — `qpdf` writes
   to a temp cache file AND its stdout is teed to the HTTP
   response. Same memory profile as direct streaming, but with
   an additional disk write that lands in the cache on success.

**Recommendation (proceed default): C — tee qpdf output.**

**Rationale:** B violates NFR-1. A is a defensible simplification
but loses the "submit the same doc twice → instant response"
property which is useful for development. C uses a `tee`-style
mechanism (specifically: an async task that writes qpdf stdout to
both the HTTP response generator AND a temp cache file). Costs
one extra disk write at write speed (cheap on NVMe). The temp
cache file is renamed into the cache only on success.

[Answer]: PROCEED

---

### Q10 — Input format validation timing

When does format validation happen?

A) **At server receive, before buffering to disk** — read first
   N bytes from the multipart stream, detect format by magic
   bytes, reject early if unsupported. Fast-fail on bad inputs.
B) **After buffering to disk** — buffer entire upload, then
   detect format. Simpler but wastes I/O on bad inputs.
C) **At Aspose open time** — let Aspose decide. Wastes a
   subprocess fork on bad inputs.

**Recommendation (proceed default): A — magic-byte detection at receive.**

**Rationale:** Cheapest, fail-fast, prevents bad-input DoS-by-IO.
Format detection by magic bytes is well-defined for our four
formats:
- DOCX/PPTX/XLSX: ZIP magic `PK\x03\x04` + look for specific
  Content-Types in `[Content_Types].xml`
- PDF: `%PDF-` in first 8 bytes

[Answer]: PROCEED

---

### Q11 — Open items resolution (from Requirements Analysis)

Three open items have been pending since Requirements Analysis.
Please resolve:

**Q11a — Sample document corpus**:

A) User will supply representative documents for integration tests.
B) AI generates synthetic test documents (one each: small/medium
   DOCX, simple/complex PPTX, single/multi-sheet XLSX, simple PDF).
C) Both — use my synthetic corpus initially, user adds real
   documents later.

**Recommendation: B — AI-generated synthetic corpus.**

[Answer]: PROCEED

**Q11b — Target host environment**:

A) Pure Linux container (production-shaped from day 1).
B) Linux container + Docker Desktop on macOS (amd64 emulation
   acceptable for dev).
C) Linux container + native macOS / Windows runs (would require
   non-x86_64 Aspose support; almost certainly out of scope).

**Recommendation: B — Linux container + Docker-on-Mac acceptable.**

[Answer]: PROCEED

**Q11c — Python version pin**:

A) Python 3.11 (broadly available, mature asyncio).
B) Python 3.12 (newer, better type system, asyncio improvements).
C) Latest 3.x (auto-track upstream).

**Recommendation: A — pin to 3.11.**

**Rationale:** 3.11 is well-supported across Aspose-python builds,
pydantic-settings, FastAPI, and uvicorn. 3.12 is fine but
unnecessary for v1. C invites breakage on a future Python
release.

[Answer]: PROCEED

---

### Q12 — Anything else?

Domain-model entities, business rules, or constraints I should
know before producing the functional design artifacts?

[Answer]: PROCEED

---

**When you're done**, reply "answered" or just say "proceed" to
lock all defaults, and I'll generate the three functional design
artifacts.
