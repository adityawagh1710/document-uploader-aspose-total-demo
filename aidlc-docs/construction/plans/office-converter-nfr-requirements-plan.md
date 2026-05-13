# NFR Requirements Plan — office-converter (Local v1)

## Purpose

Formalize the non-functional requirements established in
`requirements.md` (NFR-1 through NFR-8), make explicit tech stack
decisions, and ask the remaining questions where the high-level NFR
shape leaves room for choice.

## Plan Checklist

- [ ] Collect answers to NFR questions (this document)
- [ ] Analyze answers for ambiguities; ask follow-ups if needed
- [ ] Generate `office-converter/nfr-requirements/nfr-requirements.md`
- [ ] Generate `office-converter/nfr-requirements/tech-stack-decisions.md`
- [ ] Present completion message and wait for approval

## Questions

Each has a `[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)` default — reply "proceed" to lock all
or override specific questions.

---

### Q1 — Performance budgets (concrete numbers)

`requirements.md` NFR-4 states "best-effort wall-time targets, no
formal SLO in v1." For internal sanity checks during testing and
NFR Design, what concrete budgets should we measure against?

A) **Budgets:**

| Metric                                  | Budget (informational, not committed) |
| --------------------------------------- | ------------------------------------- |
| Per-chunk render (10-page DOCX, 5 MB)   | ≤ 5 s wall time                       |
| Per-chunk render (10-page PPTX, 20 MB)  | ≤ 15 s wall time                      |
| Probe (any format, ≤100 MB input)       | ≤ 2 s wall time                       |
| qpdf concat (10 chunks)                 | ≤ 1 s wall time                       |
| End-to-end (100-page DOCX, 5 MB)        | ≤ 30 s wall time, N=4 chunks, parallel=2 |
| Server startup (cold)                   | ≤ 10 s to ready                       |
| `/health` response                      | ≤ 100 ms                              |

B) **No budgets at all** — strictly best-effort, no measurement
   targets.

[Answer]: B

---

### Q2 — Reliability and restart policy

What recovery behavior should the v1 service exhibit?

A) **Standard Docker restart-on-failure**: container exits non-zero
   on unrecoverable errors (license-load failure at startup,
   missing qpdf binary, etc.). Operator's container runtime handles
   restart via `--restart unless-stopped` or equivalent.
   Mid-request crashes mean those in-flight requests fail with
   connection drop (HTTP layer); the caller retries.
B) **In-process recovery** — try to handle unrecoverable errors
   without exiting. Higher complexity, less safe.

**Recommendation (proceed default): A — Docker restart, no in-process
recovery beyond per-request error handling.**

[Answer]: A

---

### Q3 — Tech stack confirmation (REVISED for C++ worker, 2026-05-11)

Aspose integration switched to native C++ (Application Design Q1 = B).
Confirm or override the revised tech stack:

| Layer                    | Choice                                                |
| ------------------------ | ----------------------------------------------------- |
| Orchestrator language    | Python 3.11                                           |
| **Worker language**      | **C++ (linking Aspose.Total C++)**                    |
| HTTP framework           | FastAPI                                               |
| ASGI server              | Uvicorn                                               |
| Settings model           | pydantic-settings (v2)                                |
| Async subprocess         | asyncio (stdlib)                                      |
| Test runner              | pytest                                                |
| Property-based testing   | Hypothesis                                            |
| HTTP test client         | FastAPI `TestClient`                                  |
| Python linter            | ruff                                                  |
| Python formatter         | ruff format                                           |
| Python type checker      | mypy in strict mode                                   |
| **C++ compiler**         | **gcc from Debian Bookworm (gcc 12.x)**               |
| **C++ standard**         | **C++17**                                             |
| **C++ build system**     | **CMake (3.25+)**                                     |
| **C++ test framework**   | **(optional) GoogleTest for worker-internal tests**   |
| **Aspose binding**       | **Aspose.Total C++ shared library, dynamic-linked**   |
| PDF concat (native)      | qpdf binary                                           |
| RAM limit enforcement    | prlimit (util-linux)                                  |
| Container base image     | python:3.11-slim-bookworm (runtime); debian:bookworm + gcc + Aspose tarball (builder) |
| Python package manager   | uv                                                    |

A) Accept as listed.
B) Override specific entries (describe below).

**Recommendation (proceed default): A.**

[Answer]: A

---

### Q4 — Container base image

Docker base image for the runtime image.

A) **`python:3.11-slim-bookworm`** — official, slim Debian-based,
   well-supported, easy to install qpdf via apt.
B) **`python:3.11-bookworm`** — full Debian, larger but everything
   pre-installed.
C) **`debian:bookworm-slim`** with Python and .NET installed by us
   — full control, larger Dockerfile.

**Recommendation (proceed default): A — slim-bookworm.**

**Rationale:** Smallest image that still has apt for `qpdf` and
`util-linux`, official Python builds, recent glibc for .NET runtime
that backs aspose-python.

[Answer]: A

---

### Q5 — Aspose.Total C++ acquisition and placement (REPLACES old .NET runtime question)

The original Q5 about .NET runtime is N/A — no .NET in the C++ path.
The new Q5 is about how Aspose.Total C++ enters the image.

Aspose distributes Aspose.Total C++ as a downloadable tarball
containing shared libraries (`.so`), C++ headers, and license-handling
infrastructure. How does the Dockerfile obtain and stage these?

A) **Builder stage downloads from Aspose's CDN** at build time using
   a URL pinned in the Dockerfile (e.g.
   `https://releases.aspose.com/...`); copies headers and `.so` into
   `/opt/aspose/`. Linker flags point to that path.
B) **Bake-in via build context**: operator drops the Aspose tarball
   into a known location next to the Dockerfile; builder stage
   `COPY`s and extracts. Image build is reproducible without network.
C) **Both A and B**: try the local tarball first (B), fall back to
   download (A) if not present.

**Recommendation (proceed default): B — bake-in via build context.**

**Rationale:** Aspose's download URLs require auth in some
configurations and the licensing requires you to have downloaded
the SDK matching your license. Operator-supplied tarball avoids
build-time network dependencies and matches the bind-mount approach
already used for the runtime license file. A is risky for CI/CD
without a credentials story. C is reasonable but adds Dockerfile
complexity.

**Sub-question:** in the runtime image, where do the Aspose `.so`
files live? **Default: `/usr/local/lib/aspose/`** with
`LD_LIBRARY_PATH=/usr/local/lib/aspose:$LD_LIBRARY_PATH` set in the
runtime stage's `ENV`. Worker binary's RPATH points here.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q6 — Property-based testing scale

Hypothesis's `@given` examples count. PBT is a blocking extension
(NFR-6).

A) **Default**: 100 examples per `@given` (Hypothesis default).
B) **Increased**: 500 examples for chunk-planner properties (the
   most load-bearing surface per business-logic-model §2.6);
   default for others.
C) **CI vs local**: 100 locally, 500 in CI.

**Recommendation (proceed default): B — 500 for chunk planner, 100
elsewhere.**

**Rationale:** Chunk-planner correctness is load-bearing; 500
examples gives good coverage of edge cases in page counts and
seam distributions. PBT for qpdf concat and subdivision logic are
simpler surfaces — 100 is fine. Single setting keeps the test
runner simple; CI-only-higher is a refinement we can add if
needed.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q7 — Test coverage targets

Coverage gate for v1.

A) **No coverage gate** — coverage is observed but not enforced.
B) **80% line coverage** on `office_convert/` (excluding the
   `worker_main` entry point and `server.py` framework wiring,
   which are hard to unit-test).
C) **90% line coverage** with same exclusions.

**Recommendation (proceed default): B — 80% with exclusions.**

**Rationale:** 80% is a defensible quality bar for v1 without
chasing the last 20% which often costs disproportionate time.
`worker_main` is integration-tested by the subprocess invocation
in `aspose_worker` tests; `server.py` is covered by FastAPI
`TestClient` tests but those count as integration tests, not
unit. PBT contributes to coverage of `chunk_planner` and friends
which are the algorithm-load-bearing modules.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q8 — Health check semantics

`/health` endpoint behavior:

A) **Liveness only** — return 200 if the process is running.
   Lightweight.
B) **Readiness** — return 200 if process is running AND license is
   not expired AND scratch dir is writable AND qpdf binary is
   present. Return 503 otherwise.
C) **Two endpoints** — `/health` (readiness) and `/livez` (liveness).

**Recommendation (proceed default): B — single readiness endpoint.**

**Rationale:** v1 runs in Docker; the container runtime handles
liveness via process exit. Readiness is what callers actually
need ("can I submit requests right now?"). A separate
`/livez` (C) is the Kubernetes pattern and not useful for the
plain-Docker v1 deployment.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q9 — Container security posture

Standard container security choices:

A) **Run as non-root user**: create `appuser:appgroup` in the
   Dockerfile, `USER appuser` for the runtime.
B) **Read-only root filesystem** at run time: operator passes
   `--read-only --tmpfs /tmp --tmpfs /var/run`. The image is
   compatible (no writes outside `/tmp` and the bind-mounted
   directories).
C) **Drop Linux capabilities**: operator passes `--cap-drop=ALL`;
   image works with zero capabilities.

**Recommendation (proceed default): A + B + C — all three.**

**Rationale:** These are cheap to do right at image build time;
expensive to retrofit. Non-root means a CVE in any dependency is
not a root-on-host concern. Read-only root + tmpfs scratch is
defense-in-depth. Drop capabilities because we don't need any
(no ptrace, no network admin, etc.). Documented in the README so
operators know the image supports these flags.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q10 — Logging dependencies

Structured JSON logging needs a small implementation choice:

A) **stdlib `logging` + a custom JSON formatter** — zero
   dependencies, write a small `JsonFormatter` ourselves.
B) **`structlog`** — popular structured logging library, more
   features (context vars, processor chain), adds a dep.

**Recommendation (proceed default): A — stdlib + custom formatter.**

**Rationale:** Our log schema is tiny (8 fields) and stable. The
formatter is maybe 30 lines of code. `structlog` is great for
larger apps but adds operational risk (another dep to vet,
update, and understand). The `contextvars` propagation in
Application Design §11 works equally well with stdlib `logging`
via a custom `Filter` that injects the contextvar.

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

### Q11 — Anything else?

NFRs, tech stack constraints, or quality bars I haven't asked about?

[Answer]: PROCEED — locked 2026-05-11 (C++ pivot)

---

**When you're done**, reply "answered" or "proceed" to lock all
defaults, and I'll generate the NFR Requirements artifacts.
