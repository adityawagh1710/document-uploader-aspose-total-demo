# NFR Design Plan — office-converter (Local v1)

## Purpose

Resolve the implementation patterns that NFR Requirements left at a
high level: concrete subprocess invocation form, streaming response
wiring, async context propagation, multipart buffering, cache
atomicity execution, resilience behaviors at the seams. C++ worker
+ Python orchestrator boundary informs most of these.

## Plan Checklist

- [ ] Collect answers to NFR design questions (this document)
- [ ] Analyze answers for ambiguities; ask follow-ups if needed
- [ ] Generate `office-converter/nfr-design/nfr-design-patterns.md`
- [ ] Generate `office-converter/nfr-design/logical-components.md`
- [ ] Present completion message and wait for approval

## Questions

Each has a `[Answer]: PROCEED — locked 2026-05-11` default — reply "proceed" to lock all.

---

### Q1 — prlimit invocation form

How exactly does the orchestrator apply the 2 GB ceiling to the C++
worker subprocess?

A) **External `prlimit` CLI wrapper**: argv starts with `["prlimit",
   "--as=2147483648", "--", "/usr/local/bin/office-convert-worker",
   ...]`. The `prlimit` binary applies `RLIMIT_AS` and exec's the
   worker. Simple, no Python-side work, reliable.
B) **`preexec_fn` callback in `asyncio.create_subprocess_exec`** —
   Python function runs in the child after fork but before exec,
   calls `resource.setrlimit(resource.RLIMIT_AS, ...)`. Not
   supported in asyncio (`preexec_fn` requires `subprocess.Popen`,
   not asyncio's subprocess API).
C) **POSIX wrapper script** — short shell wrapper
   `worker-with-limit.sh` that runs `ulimit -v` then `exec` the
   worker. One extra file.

**Recommendation: A — external prlimit CLI wrapper.**

**Rationale:** asyncio's subprocess API has no `preexec_fn`, so B
is a non-starter without using `subprocess.Popen` directly (which
defeats the async model). A is the cleanest: `prlimit` is a
standard `util-linux` binary, no Python-side resource fiddling, no
extra files to maintain. The argv shape is documented in
`business-logic-model.md §3`.

[Answer]: PROCEED — locked 2026-05-11

---

### Q2 — Multipart upload buffering pattern

NFR-3 caps inputs at 1 GB. FastAPI's default `UploadFile` already
spills to disk past `SPOOL_MAX_SIZE` (default 1 MB). How do we
extract the file to the per-request scratch path?

A) **Use FastAPI's `UploadFile` directly**, then on receipt:
   `await async_copy(file.file, scratch_path)` to copy the
   already-spooled tempfile to our scratch directory. One disk
   write.
B) **Stream the request body manually** via FastAPI's `Request`
   object: `async for chunk in request.stream()` writing directly
   to the scratch path. Skips FastAPI's `UploadFile` overhead;
   slightly more code.
C) **Symlink/rename FastAPI's tempfile** into the scratch path
   instead of copying. Fast but couples us to FastAPI's tempfile
   handling.

**Recommendation: A — UploadFile + async copy.**

**Rationale:** Cleanest, idiomatic FastAPI. The single extra copy
on top of FastAPI's existing tempfile-spool is negligible at v1
scale (single user, ≤1 GB inputs). B is faster on hot paths but
v1 doesn't have hot paths. C is fragile across FastAPI versions
(the temp-file location is an implementation detail).

[Answer]: PROCEED — locked 2026-05-11

---

### Q3 — Streaming response wiring (qpdf stdout → HTTP body)

How does qpdf's stdout reach the HTTP response without buffering?

A) **`StreamingResponse` with an async generator** that wraps the
   qpdf subprocess: yield `process.stdout.read(64 KB)` in a loop
   until empty, await process exit at end. FastAPI/Starlette
   handles chunked transfer encoding automatically.
B) **`StreamingResponse` with a raw `IO` object** passed as
   content. Starlette iterates it. Less control over chunk size
   and error handling.
C) **Server-sent events (SSE)** — wrong protocol for binary PDF.

**Recommendation: A — async generator with explicit 64 KB reads.**

**Rationale:** Idiomatic Starlette/FastAPI streaming pattern.
64 KB read size matches typical pipe-buffer behavior on Linux and
gives reasonable backpressure granularity. The async generator
also acts as the natural place to apply the tee-to-cache logic
from `business-logic-model.md §5` (Q9 default): same generator
writes each chunk to the temp cache file AND yields it upstream.

[Answer]: PROCEED — locked 2026-05-11

---

### Q4 — contextvars propagation across asyncio.gather

Chunk renders dispatch concurrently via `asyncio.gather` under a
per-job `Semaphore`. The `request_id` lives in a `ContextVar` that
should propagate to every chunk's log lines.

A) **Rely on asyncio's automatic contextvars copying** — every
   coroutine launched via `gather`, `create_task`, etc. snapshots
   the current context. Set `request_id` at the top of the request
   handler; it flows everywhere automatically.
B) **Explicit context passing** — accept `request_id` as a
   function parameter to every coroutine. Verbose but explicit.
C) **Hybrid** — use contextvars but assert in tests that the value
   is present at every chunk-completion log line.

**Recommendation: A with C's assertion — use contextvars, add
hypothesis-driven tests to verify propagation.**

**Rationale:** asyncio DOES auto-copy `ContextVar` state into
spawned tasks. This is the idiomatic pattern. B turns every
function signature noisy. C adds confidence cheaply — a unit
test that spawns 10 fake chunk renders via `gather` and asserts
each emitted log line has the right `request_id`.

[Answer]: PROCEED — locked 2026-05-11

---

### Q5 — Worker stderr capture pattern

The C++ worker writes diagnostic JSON to stderr on failure. The
orchestrator must capture it without deadlocking the pipe.

A) **`asyncio.create_subprocess_exec(stderr=PIPE)` + concurrent
   reads**: `await asyncio.gather(process.wait(), capture_stderr())`
   where `capture_stderr()` drains the pipe into a buffer. Avoids
   pipe-full deadlocks.
B) **`stderr=DEVNULL`** — discard stderr entirely. Loses the
   diagnostic. Not acceptable.
C) **Tee stderr to a file in scratch dir** — `stderr=open(path)`,
   read the file after exit. Works but introduces a file we have
   to clean up.

**Recommendation: A — concurrent drain via gather.**

**Rationale:** Standard asyncio subprocess pattern for "capture
stderr while subprocess is running." Stderr buffer size is
bounded by what the worker writes (the worker logs a single JSON
line on failure, well under any reasonable pipe buffer). The
captured bytes are then attached to the `RenderError`'s
`stderr_tail` field per the diagnostic schema.

[Answer]: PROCEED — locked 2026-05-11

---

### Q6 — Cache write atomicity execution pattern

`business-rules.md §7` documents the temp-file + rename protocol.
Confirm the execution mechanics:

A) **Write to `<final>.tmp.<pid>.<uuid4>`, `fsync()`, then
   `os.rename()`**. POSIX rename is atomic within a filesystem.
   `fsync` ensures durability before the rename makes the file
   visible. Orphan cleanup: separate operator concern.
B) **Same as A but with `os.replace()`** instead of `os.rename()`
   for cross-platform atomicity (works on Windows too).
C) **`fcntl` advisory locks** during writes. Slower, no benefit
   over rename atomicity.

**Recommendation: B — `os.replace()`.**

**Rationale:** Same atomicity guarantees as `os.rename` on POSIX
(per Python docs: `os.replace` is atomic on POSIX iff source and
target are on the same filesystem, which they are by construction).
Marginally safer if someone runs tests on Windows (cross-platform
atomicity). `fsync` before replace; cheap correctness insurance.

[Answer]: PROCEED — locked 2026-05-11

---

### Q7 — Health check probing pattern

`/health` reports readiness based on license, scratch, qpdf, worker
binary, and Aspose `.so`. When are these checked?

A) **Once at server startup**, results cached in memory; `/health`
   reads the cached state. Refresh on `LicenseManager.refresh()`
   call.
B) **Every `/health` call** — re-stat each path, re-parse license,
   try to load Aspose .so. Slower but more accurate.
C) **Hybrid**: license is re-checked every call (cheap, important
   for the 30-day temp); filesystem paths checked once at startup
   then cached.

**Recommendation: C — hybrid.**

**Rationale:** License expiry changes by the day and is the most
operationally critical signal — must be live. Filesystem and
binary checks are quasi-static (paths don't disappear in normal
operation); cache them. /health response stays under 100 ms even
on slow disks.

[Answer]: PROCEED — locked 2026-05-11

---

### Q8 — Resilience: license file disappears mid-flight

Operator could rotate the bind-mounted `.lic` file while requests
are in flight. Worker subprocess re-reads on every invocation.
What's the orchestrator's behavior?

A) **Best-effort, no special handling** — if license file is
   gone when a worker starts, the worker exits 2; orchestrator
   raises `LicenseExpiredError`; request returns 503. Next /health
   call reports not-ready.
B) **Pre-flight check** — orchestrator re-stats the license file
   at the start of every request; fails fast with 503 if missing
   without spawning workers.
C) **File watcher** — `inotify` (or pythonic equivalent) monitors
   the license path; updates `LicenseManager` state in real time;
   serves 503 with current status.

**Recommendation: A — best-effort, no special handling.**

**Rationale:** B adds a per-request stat that's wasteful in the
99.99% case where the file is present. C adds a file watcher
dependency for a corner case. The worker's exit code 2 already
handles the failure cleanly; the operator who removed the license
file accepts the consequences (a few seconds of failed requests
before /health alerts). Documented v1 limitation.

[Answer]: PROCEED — locked 2026-05-11

---

### Q9 — Anything else?

Patterns, primitives, or constraints I should know before producing
the NFR design artifacts?

[Answer]: PROCEED — locked 2026-05-11

---

**When you're done**, reply "answered" or "proceed" to lock all
defaults, and I'll generate the two NFR design artifacts.
