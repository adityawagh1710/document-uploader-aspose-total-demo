# Requirements Verification Questions — Office Converter

> **⚠ SCOPE STATUS (2026-05-11): DEFERRED — CLOUD-TARGET WORK.**
> v1 scope is now local-only per user directive ("keep it simple for now —
> we'll get it working locally first (no EKS)"). The 25 answers below
> represent the eventual cloud-deployment shape and are preserved here for
> that future work. They are NOT v1 requirements.
>
> **v1 requirements: see `local-v1-scope.md` in this directory.**
>
> The algorithm-level decisions (2 GB ceiling, conservative chunks, hybrid
> split, subdivision retry, qpdf streaming merge, swap as backstop) carry
> over from the discussion below into v1 unchanged. The infrastructure
> decisions (SQS, DynamoDB, S3, EKS, multi-tenancy, KEDA, CloudWatch) do
> not apply to v1.

**Purpose**: Resolve the gaps and deferred decisions in `office-converter.md` before
drafting the full requirements document. The design doc is solid on *strategy* (chunk
+ stream-merge) but explicitly defers many *implementation* choices. Please answer
each by filling the `[Answer]:` tag with a letter (e.g. `[Answer]: B`). For "Other",
choose `X` and describe after the tag.

Answer at your own pace and let me know when done (or say "proceed" if you'd like me
to assume reasonable defaults for anything left blank).

---

## Load-Bearing Constraint (provided by user 2026-05-11)

**Per-pod RAM ceiling: 2 GB.** This is the dominant constraint and propagates into
several answers below (notably Q3, Q10, Q11, Q19, and the retry strategy). The design
doc's "retry on higher memory tier" assumption does **not** hold under this ceiling —
the only escape valve is subdivision (smaller page range) followed by re-render.

Aspose's documented 2–20× memory amplification over input size implies, after
~200 MB of runtime/OS overhead inside the 2 GB cgroup:

- Worst-case safe input chunk (20×): ~90 MB
- Typical case (5×):                  ~360 MB
- We must size chunk defaults to the worst case, not the typical case, because
  pathological inputs (PPTX with embedded media, XLSX with huge sheets) are exactly
  the inputs most likely to hit the upper end of the amplification factor.

---

## A. Scale, SLOs, and Tenancy

### Question 1 — Target throughput

What is the steady-state job throughput this service must support?

A) Low — up to ~10 concurrent conversions, <100 jobs/hour
B) Medium — up to ~100 concurrent conversions, ~1,000 jobs/hour
C) High — up to ~1,000 concurrent conversions, ~10,000+ jobs/hour
D) Unknown / design for elasticity; let autoscaling decide
X) Other (please describe after [Answer]: tag below)

[Answer]: A — Low: up to ~10 concurrent conversions, <100 jobs/hour.
(Revised from D 2026-05-11.)

**Rationale:** Chosen by the user. Concrete capacity target, low-end. Coherent
with the rest of the v1 picks (Q6 = X deferred auth, Q9 = A static fleet) —
v1 is a small, deliberately bounded service. No autoscaling, no elasticity
plumbing.

**Capacity math (validates Q9 = A's N=4 starting heuristic):**

- 100 jobs/hour × ~10 chunks/typical job ≈ 1,000 chunks/hour
- Per-chunk render on 2 GB pod ≈ 2–5 s → 1 worker processes ~720–1,800
  chunks/hour
- N=2 workers comfortably saturate the steady-state target
- N=4 (Q9 default) gives 2–4× headroom for bursts and for jobs in the
  intermediate tier (100 MB – 1 GB) that have more chunks
- Peak concurrent in-flight: 10 jobs × 10 chunks = 100 chunks worst case;
  at N=4 workers, peak queue depth ≈ 96 pending; drain time at 4 chunks
  every 5 s ≈ 2 min. Well inside the 15 min p95 (Q2 = D).

**Implications:**

- Operator can resize the fleet via deploy or `kubectl scale` if duty
  cycle data shows the heuristic is wrong. Manual; no autoscaler in v1.
- Cost is bounded and predictable: N × (2 GB pod cost) per hour for the
  Aspose worker fleet, plus the orchestrator pod, plus DynamoDB on-demand,
  plus S3 storage, plus SQS message volume (well below free tier at this
  scale).
- This is explicitly NOT a forever-scale design. If throughput targets
  rise materially, the design needs revisiting — not just N — because
  some choices (manual scaling, no per-tenant quotas under Q6 = X)
  scale poorly past tens of tenants or hundreds of concurrent jobs.

### Question 2 — Latency target (per typical job)

For a "typical" document (say a 100-page DOCX, ~5 MB input), what is the acceptable
p95 end-to-end conversion latency?

A) Real-time — under 5 seconds
B) Interactive — under 30 seconds
C) Near-line — under 2 minutes
D) Batch — under 15 minutes is fine
X) Other (please describe after [Answer]: tag below)

[Answer]: D — Batch (15 min p95). (Confirmed by user 2026-05-11.)

**Rationale:** Chosen by the user. The 15-minute budget signals a batch-style
workload: callers are pipelines or async consumers, not humans waiting on a UI
spinner. This is a *ceiling*, not a target — most jobs will still complete in
seconds. The headroom is for cold starts, worker-pool saturation, and large
inputs.

**Implications and simplifications unlocked:**

1. **Cold-start tolerance is now generous.** Under a 30 s p95, scale-from-
   cold-pod (~30–60 s on EKS) would have been a problem. Under 15 min p95,
   it's a non-issue — first-job-after-idle still completes well within the
   ceiling. This relaxes worker-pool sizing constraints, but per Q9 = A
   v1 ships with a static fleet anyway, so the relaxation is latent.
2. **Parallel dispatch fan-out can be smaller.** Original sizing assumed
   ~5-way parallelism per job to hit 30 s p95 on a 100-page doc. At 15 min,
   even fully serial chunk dispatch (10 chunks × 5 s = 50 s) fits with
   room. Smaller per-job parallelism → less worker contention → better
   overall fleet throughput. Default to 2–3 way dispatch; spend the saved
   workers on more jobs in flight.
3. **Tiered SLO updated** (was set under Q3):

   | Input size      | SLO (p95)            |
   | --------------- | -------------------- |
   | ≤ 100 MB        | **15 min** (Q2)      |
   | 100 MB – 1 GB   | measured, no SLO     |
   | 1 GB – 10 GB    | best-effort, no SLO  |

4. **Caller UX expectation must be set explicitly.** A 15-minute ceiling is
   incompatible with a human-facing UI without progress signalling. The
   async polling API (Q5 = B) already supports periodic status updates; the
   requirements doc should require the orchestrator to emit chunk-completion
   progress (e.g. 4/10 chunks rendered) so callers can drive their own
   progress bars. This is more important under a 15 min budget than under a
   30 s budget.
5. **Caching tier change?** None. Q13 (final + per-chunk PDFs cached) still
   pays — caching saves compute cost regardless of latency, and the per-chunk
   layer specifically helps documents that get resubmitted with small edits.

Re-confirming the tradeoff in writing: the service is batch-shaped. If callers
later need an interactive variant (sub-30 s for small docs), that's a separate
"fast path" with different sizing — don't try to do both with one SLO.

### Question 3 — Maximum input size

What is the upper bound on input document size the service must accept without
failure (subdivision down to single-page chunks is allowed)?

A) ≤100 MB / ≤500 pages
B) ≤1 GB / ≤5,000 pages
C) ≤10 GB / ≤50,000 pages (effectively unbounded for practical office docs)
D) No declared upper bound — must handle anything
X) Other (please describe after [Answer]: tag below)

[Answer]: C — ≤10 GB / ≤50,000 pages. (Confirmed by user 2026-05-11.)

**Rationale:** Chosen by the user. The 2 GB *per-pod* ceiling is preserved by
chunking, so input size is not bounded by worker RAM. The declared 10 GB / 50K
page ceiling gives us a *known* upper bound for capacity planning and lets us
reject inputs above the ceiling with a clean error rather than discovering
trouble at the chunk-planning stage. Inputs above the ceiling are rejected at
ingest with a documented error code; the service does not silently extend.

**Implications to carry forward (flag for NFR design):**

1. **Ingest validation.** Orchestrator must check declared input size (S3
   `HeadObject` `ContentLength`) at job submission and reject above the
   ceiling before any work begins. Probe-derived page count is the second
   gate — if the probe reveals page count > 50,000, fail the job before
   chunk dispatch.
2. **Orchestrator should not materialize the full input on local disk.** Even
   a 10 GB input on every orchestrator pod is wasteful and ties pod ephemeral-
   disk sizing to the worst case. Stream the S3 download into the probe and
   chunk planner; Aspose workers pull only their assigned page range from a
   shared source location (S3-Express, FSx, or S3 directly with byte-range
   reads for formats that support it). Orchestrator's ephemeral disk is sized
   for the chunk PDFs in flight, not for full input copies.
3. **Chunk-plan data structure must scale to ~50K chunks worst case.** At 10
   pages per chunk and 50K pages max, the worst-case plan is ~5K chunks; at
   the conservative MB-bound, possibly more. 5K entries fit comfortably in
   memory, but the dispatch path should still externalize to SQS so the
   orchestrator can scale horizontally (multiple orchestrator pods drain
   chunks from the same job's queue without coordinating in-process state).
4. **Job state must be persistent and resumable.** A 10 GB conversion can run
   for hours and cannot die with the orchestrator pod. Per-chunk completion
   status goes to a durable store (DynamoDB job table is the natural fit on
   EKS+AWS); orchestrator restarts resume from the cursor.
5. **Probe operation must itself be memory-bounded on the 2 GB worker.**
   Aspose's probe APIs vary in memory cost by format; for very large inputs
   the probe must use `LoadOptions::TempFolder` and `MemoryOptimization` just
   like the render path. Per-format verification required during NFR design —
   PPTX probe with large embedded media is the prime risk.
6. **Wall-time SLO applies to "typical" jobs only.** Q2's 15 min p95 (revised
   2026-05-11) cannot cover a 10 GB conversion. The requirements doc declares
   a tiered SLO: p95 ≤ 15 min for inputs ≤ 100 MB; best-effort with no
   commitment for inputs > 1 GB. Intermediate tier (100 MB – 1 GB) measured
   but not SLO'd in v1. See Q2 for the full table.
7. **S3 multipart upload of the final PDF** must handle outputs that may
   exceed S3's 5 GB single-part limit. The streaming qpdf → `boto3` multipart
   path already provides this property; confirm in PBT (Q24).

### Question 4 — Tenancy and isolation model

How should jobs from different callers be isolated?

A) Single-tenant — only one trusted internal caller; no tenancy logic needed
B) Multi-tenant with logical isolation — caller-scoped S3 keys, per-tenant quotas, shared pods
C) Multi-tenant with hard isolation — per-tenant namespaces, per-tenant Aspose pools
D) Multi-tenant with optional dedicated pools for premium tenants
X) Other (please describe after [Answer]: tag below)

[Answer]: B — Multi-tenant with logical isolation. (Confirmed by user 2026-05-11.)

**Rationale:** Chosen by the user. Logical isolation keeps the elastic worker fleet
shared (preserving the autoscaling model from Q9) while adding tenant identity as a
first-class concept across the API, storage, cache, and observability surfaces. Hard
isolation (C/D) was the more expensive alternative and was rejected; we don't need
per-tenant pools.

**Implications to carry forward (flag for NFR + security design):**

1. **Tenant identity must propagate to every artifact.** Tenant ID is attached
   to every job record, S3 key, log line, metric dimension, and trace span.
   Under Q6 = X (no auth in v1), `tenant_id` is **caller-asserted** — taken
   from the SQS message body and trusted. In v2 with auth re-introduced,
   `tenant_id` will be validated against an IAM-principal-to-tenant mapping
   (`Attributes.SenderId` from the SQS message). Cross-tenant data leakage
   is the dominant security risk; the mapping is the single place that risk
   is enforced when auth lands. v1 documents the deferred-enforcement gap
   in the operator runbook.
2. **S3 key layout becomes tenant-scoped.** Suggested layout:
   `s3://<bucket>/<tenant-id>/<job-id>/{source,chunks,output}/...`. Bucket policy
   denies cross-tenant prefix access by default. Presigned URLs (Q7 = A) are
   issued scoped to the requesting tenant's prefix only.
3. **Per-tenant quotas deferred to v2.** True per-tenant quotas require
   verified tenant identity (Q6 = A or equivalent). Under Q6 = X (v1), the
   only enforceable quotas are **global** (fleet-wide concurrent-chunk
   ceiling, global rate limit on the submit queue via SQS), surfaced via
   `failed` status with `failure_class: 'global_quota_exceeded'`. v2 adds
   per-tenant concurrent-jobs and jobs-per-hour quotas, also surfaced via
   the status row. The data model already supports per-tenant quota state
   (the quotas table is keyed by `tenant_id`); only the enforcement code
   activates in v2.
4. **Cache scope decision becomes load-bearing** (revisits Q13). Two options:

   a. **Per-tenant cache namespace** (default). Cache keys include tenant ID;
      identical document submitted by two tenants renders twice. Zero leak risk.

   b. **Cross-tenant content-addressable cache** (opt-in). Cache keys are the
      raw content SHA-256, shared across tenants. Saves compute on common
      documents but creates a probing oracle: tenant A can detect whether
      tenant B has previously rendered a specific document. This may be
      acceptable for fully public inputs but is unacceptable for confidential
      inputs.

   Default (a). Flag for explicit user decision before code generation.
5. **Audit logging includes tenant ID on every entry.** Both for security-baseline
   compliance (the enabled extension) and for billing/cost-attribution use cases.
6. **DLQ and failed-jobs S3 prefix are tenant-scoped** (revisits Q18). Operator
   forensic access goes through an admin role that *can* read across tenants;
   tenant-self-service access (if any) is scoped.
7. **Per-tenant metrics** (revisits Q17). CloudWatch metric dimensions include
   tenant ID. This drives both per-tenant alerting and per-tenant billing.

---

## B. API Surface and Job Model

### Question 5 — Caller API style

How do clients submit jobs and receive results?

A) Synchronous HTTP — POST with input, response carries output PDF (only viable for small docs)
B) Asynchronous HTTP polling — POST returns job ID; GET /jobs/{id} for status; final PDF in S3
C) Webhook callback — POST returns job ID; service POSTs to caller URL on completion
D) Queue-driven — caller drops a message in SQS / Kafka; orchestrator consumes
E) Multiple of the above (please describe after [Answer]: tag below)
X) Other (please describe after [Answer]: tag below)

[Answer]: D — Queue-driven via SQS. (Confirmed by user 2026-05-11.)

**Rationale:** Chosen by the user. Combined with Q2 = D (15 min p95, batch-shaped),
this is a coherent design: callers are already async consumers, so making the
ingest contract async-first removes the half-measure of polling an HTTP API. No
public HTTP surface on the orchestrator → smaller attack surface, SQS visibility
timeout gives free crash-retry, queue depth gives natural backpressure, and
the SQS contract leaves room for autoscaling later if the static fleet
(Q9 = A) ever becomes the wrong shape — without locking in a path now.

**Concrete contract:**

1. **Submit channel.** Per-tenant SQS queue `aspose-jobs-<tenant-id>` (or a
   single shared `aspose-jobs` queue with `tenant_id` in the message and
   IAM-enforced send permission — equivalent semantically, per-tenant queue is
   easier to throttle independently). IAM authorizes `sqs:SendMessage` on the
   tenant's queue.
2. **Submit message body** (JSON):

   ```
   {
     "correlation_id": "<caller-generated UUID>",
     "tenant_id": "<derived from IAM principal at consume time, sanity-checked>",
     "source_s3_uri": "s3://<caller-or-service-bucket>/<key>",
     "callback": {
       "type": "sqs" | "sns" | "none",
       "arn": "<optional callback queue/topic ARN>"
     },
     "options": {
       "nocache": false,
       "output_profile": "generic"   // Q15
     }
   }
   ```

3. **Result location.** Orchestrator writes output to
   `s3://<bucket>/<tenant_id>/<correlation_id>/output.pdf` (matches Q4's
   tenant-scoped key layout). The caller-supplied `callback.arn` (if any)
   receives a completion event: `{correlation_id, status, output_s3_uri,
   presigned_url}` where the presigned URL has the 24h TTL from Q7.
4. **Status / progress channel.** Under Q2 = D (15 min budget), we promised
   progress signalling. Options:

   - **Status DynamoDB table** keyed by `(tenant_id, correlation_id)`, readable
     by the caller via tenant-scoped IAM. Schema: `{status, chunks_total,
     chunks_completed, last_update}`. Cheap, scales fine, no extra queue
     plumbing for callers who don't need streaming progress.
   - **Per-tenant progress SNS topic** (opt-in): caller subscribes to receive
     fine-grained chunk-completion events.

   Default: DynamoDB status table (option a). SNS progress as a Phase-2
   addition. The completion callback (point 3 above) is always available.
5. **Cache bypass** (revisits Q14) becomes the `options.nocache: true` field
   in the submit message, not a query string.

**Implications and cascading effects on other answers:**

1. **Q6 (auth) is even more naturally A (IAM).** No HTTP API to authenticate;
   SQS IAM policies are the entire auth surface. Tenant identity comes from
   the IAM principal of the message sender, verified at consume time.
2. **Q7 (result delivery) tightens to A.** Presigned URL in the completion
   event is the canonical delivery mechanism. Caller-supplied destination
   (option C) is harder under queue-driven because we'd need cross-account
   IAM grants per job; defer.
3. **Q9 (worker pool sizing): static fleet (A) is the v1 pick.** SQS-driven
   contract is compatible with future autoscaling if needed, but no
   autoscaler is in scope at this time.
4. **Q14 (cache bypass): A still, but the mechanism is a JSON field, not a
   query string.** Field semantics unchanged.
5. **Q18 (DLQ): SQS native redrive is now free** — failed message after
   `maxReceiveCount` retries lands in `aspose-jobs-<tenant-id>-dlq`. The
   "both A and B" choice still holds because we want the S3 forensic record
   for chunk-subdivision-floor failures (Q10), separate from the SQS DLQ
   for orchestrator-side crashes.
6. **Quotas (Q4)** are now enforced *inside the consumer*: orchestrator
   reads a message, checks tenant quota, and either processes or returns
   the message to queue with delayed visibility. The 429-equivalent is
   "delayed redrive with quota-exceeded reason" — surfaced to the caller
   via the status table.

**What we lose vs B (async HTTP polling):**

- **No standardized HTTP contract for non-AWS callers.** Anyone outside the
  AWS IAM boundary can't submit jobs without a wrapper. If a future caller
  is e.g. a SaaS partner, we'd need to bolt an HTTP-to-SQS proxy on. Worth
  knowing now.
- **Job submission feedback is asynchronous.** Caller can't get an immediate
  "job accepted" or "job rejected (bad input)" response. Bad-input rejection
  (Q3 ingest validation) happens at consume time, surfaced via the status
  table or completion callback. Acceptable for batch-shaped workloads;
  worth being explicit about.

### Question 6 — Caller authentication

How are callers authenticated to the orchestrator API?

A) AWS IAM (SigV4) — service-to-service inside the same AWS account/org
B) API keys via API Gateway
C) OAuth 2.0 / OIDC bearer tokens (JWT)
D) mTLS between trusted internal services
X) Other (please describe after [Answer]: tag below)

[Answer]: X — No application-layer auth in v1. To be added later. (Confirmed
by user 2026-05-11.)

**Rationale:** Chosen by the user. v1 ships without service-enforced caller
identity verification. AWS account boundary is the security perimeter:
anyone with `sqs:SendMessage` permission on the queue (granted at AWS account
or org level) can submit jobs. No application-layer enforcement of who they
claim to be.

**What "no auth" actually means under Q5 = D:**

1. SQS IAM is unavoidable — every AWS API call needs *some* credentials. The
   queue is in our AWS account; default access is restricted to principals
   in the same account/org via standard SQS queue policy.
2. Within that perimeter, the orchestrator does NOT validate that the IAM
   principal sending a message matches the `tenant_id` claimed in the
   message body. Tenant ID is **caller-asserted** in v1.
3. No per-caller rate limiting, no per-caller quotas (Q4 quotas degrade to
   global quotas only).
4. CloudTrail still logs `sqs:SendMessage` calls with the actual IAM
   principal — useful for forensic reconstruction but not for live
   authorization decisions.

**CRITICAL CONFLICT WITH Q4 = B (multi-tenant logical isolation):**

True multi-tenant isolation requires identifying the tenant. Without auth,
"logical isolation" reduces to *organizational convention* — tenants stamp
their own IDs on messages, and we trust them. This is fine for an internal
PoC where trust is given by the AWS account boundary, but it's not isolation
in the security sense.

**Proposed staged model (build the schema, defer the gate):**

- **v1 (now):** Data model is multi-tenant (S3 keys, cache keys, DynamoDB
  rows, metrics dimensions all carry `tenant_id`). `tenant_id` comes from
  the message body, caller-asserted. No principal validation.
- **v2 (when auth is added):** Orchestrator reads `Attributes.SenderId`
  from the SQS message, looks up `principal → tenant_id` in a mapping
  table, REJECTS the message if claimed `tenant_id` differs from the
  authoritative mapping. Existing data stays valid.

This staging preserves the Q4 = B data layout (forward-compatible) while
deferring the enforcement code. Concrete acceptance criterion for v2:
"Submit message with `tenant_id: 'X'` from an IAM principal mapped to
`tenant_id: 'Y'` → message rejected with `failed` status,
`failure_class: 'tenant_mismatch'`, no rendering occurs."

**Risks of v1 explicitly accepted:**

- A misconfigured or malicious AWS principal in our account can submit jobs
  claiming any `tenant_id`, polluting another tenant's S3 prefix and
  metrics. Mitigation in v1: trust the AWS account boundary; document the
  risk in operator runbook.
- No structured 429 quota enforcement; a runaway caller exhausts the
  shared fleet. Mitigation: SQS visibility timeout + maxReceiveCount
  bound runaway impact; CloudWatch alarm on per-message-source send rate
  (using `Attributes.SenderId`) for ops detection.
- Compliance regimes (HIPAA, SOC 2) generally require enforced tenant
  isolation. Q20 = A (none declared) holds for now; if it changes, v2
  auth becomes a blocker, not a future-feature.

**Other auth options for v2 reference:** A (IAM principal-to-tenant
mapping) remains the natural fit on AWS; B (API keys) requires an HTTP
proxy front-end which we don't have; C (JWT/OIDC) similarly needs an
HTTP plane; D (mTLS) is operationally heavy and also assumes HTTP. A is
the recommended v2 path.

### Question 7 — Result delivery

Where does the final PDF live and how does the caller retrieve it?

A) Service-managed S3 bucket; presigned URL returned to caller (TTL ~ 24h)
B) Service-managed S3 bucket; caller has read access via IAM
C) Caller-supplied destination S3 URI; service writes there
D) Both A and C selectable per job
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** Presigned URLs decouple caller IAM from our bucket policy — service
stays in control of retention and lifecycle, callers don't need bucket-level
permissions. Under Q5 = D (SQS-driven), the URL is delivered through two channels:

1. **Completion event** published to the caller-supplied `callback.arn` (SQS or
   SNS) carries `{correlation_id, status, output_s3_uri, presigned_url}`.
2. **Status table** (DynamoDB, keyed by `(tenant_id, correlation_id)`) carries
   the same fields on the terminal state row, so a caller without a callback
   subscription can poll for completion and read the URL.

The actual S3 object is at `s3://<bucket>/<tenant_id>/<correlation_id>/output.pdf`
matching Q4's tenant-scoped layout. Presigned URL TTL = 24h.

**Note on Q6 = X (no auth in v1):** Anyone who learns a `correlation_id` can
read its DynamoDB status row and obtain the presigned URL, then download the
output until TTL expires. v1 mitigations:

- `correlation_id` is a caller-generated UUID (high entropy) — knowing it
  requires either generating it or seeing it in the caller's CloudTrail /
  logs / message audit.
- Presigned URL TTL trimmed from 24 h → 1 h under Q6 = X to shrink the
  window of unintended exposure. Bumps back to 24 h in v2 when auth is
  restored.
- The completion callback ARN, if used, is the caller's own SQS/SNS — only
  the caller can subscribe.

B forces every caller into our IAM model (worse fit under deferred-auth v1).
C inverts who owns the data and creates a "write to a bucket we don't own"
failure mode (cross-account permissions, KMS mismatches, billing). D doubles
surface area for unclear benefit. Start with A; selectively allow C later for
callers who ask.

---

## C. Orchestrator Implementation

### Question 8 — Orchestrator language

The design doc defers Python vs TypeScript to "operational fit with the existing
stack". Which fits your stack?

A) Python (rich AWS SDK, easier multipart S3, asyncio for parallel dispatch)
B) TypeScript / Node.js (good async I/O, type safety, smaller container)
C) Go (best concurrency primitives, smallest container, lowest p99)
D) No preference — recommend based on chunk-planning complexity and ops familiarity
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** No existing-stack signal stated, so we pick on technical fit. Under
Q5 = D, the orchestrator's hottest path is "long-poll SQS, plan chunks, dispatch
chunks (also via SQS to the worker fleet), stream concatenated PDF to S3 multipart,
write completion event and status row." Python's coverage of this path:

- `aiobotocore` long-poll SQS receives with structured concurrency
- `boto3` multipart upload is the mature reference for streaming-to-S3
- DynamoDB conditional writes (for status state machine, Q24) are well-supported
- Chunk planning is arithmetic-heavy, easier to read in Python than Go/TS

Go (C) is genuinely better on p99 and container size, but the orchestrator is
I/O-bound (SQS + S3 + DynamoDB) so the win is small, and chunk-planning code
would be more verbose. TS (B) is a fine alternative; Python is the default. Flip
to D's recommendation only if the existing stack already runs TS or Go.

### Question 9 — Worker pool sizing model

How is the Aspose worker pool sized?

A) Static — fixed replica count per memory tier
B) HPA on CPU/memory — Kubernetes Horizontal Pod Autoscaler
C) KEDA on queue depth — autoscale on pending chunk count
D) Mix: static base + KEDA bursting
X) Other (please describe after [Answer]: tag below)

[Answer]: A — Static fixed replica count. (Revised from C 2026-05-11.)

**Rationale:** Chosen by the user. v1 ships with a fixed Aspose worker fleet
size; no autoscaling. Coherent with Q6 = X (deferred auth) and Q1 = A
(low throughput): keep the operational surface minimal, prove the
chunk-and-merge pipeline works at low concurrency, revisit elasticity
only if duty cycle data demands it.

With Q10 = X (single 2 GB tier, no tier promotion), "static per memory tier"
collapses to "static replica count" — there is only one tier.

**Trade-offs the user is accepting:**

- **Predictable cost.** Constant N × (2 GB pod cost) per hour regardless of
  load. Easier to budget than autoscaled spend.
- **Predictable behavior.** No autoscaling surprises during incidents. The
  fleet is the fleet; queue depth is the only thing that varies.
- **Latency variability under burst.** When concurrent submission rate
  exceeds N × (jobs-per-worker-per-hour), the SQS queue grows. Wall time
  for backlogged jobs is bounded only by Q2's 15 min p95 — backlogs
  longer than the SLO are observable as p95 misses.
- **Paying for idle capacity.** Workers consume RAM/CPU during quiet hours.
  Acceptable at small N; gets expensive as N grows. Operator can manually
  scale to zero overnight via `kubectl scale` if cost matters more than
  cold-start latency for the first morning job.
- **Manual operator action for capacity changes.** Adding/removing workers
  is a deploy or manual `kubectl scale`, not an automatic response.

**Capacity sizing (deferred to NFR design):** N depends on:

- Expected peak concurrent jobs (Q1 = D, no stated baseline → start small)
- Mean chunks per job (~10 for a 100-page DOCX under Q11 conservative)
- Mean per-chunk render time on 2 GB pod (~2–5 s, format-dependent)
- Acceptable queue-dwell time before alerting (suggest 5 min for v1)

Recommend a starting point of **N = 4 workers** for v1, with operator
runbook for scaling up/down based on observed queue dwell time. Not
load-bearing; documented as a starting heuristic.

**Why not B (HPA on CPU/memory):** Wrong signal entirely. 2 GB workers
spend most non-rendering time idle on CPU; HPA would never scale on CPU
and would false-trigger on memory under Aspose's normal allocation
patterns.

**Why not C (KEDA on SQS depth):** Was my previous pick; user has
explicitly chosen the simpler static model. KEDA is not on the roadmap
at this time — bring it back into scope only if duty cycle data shows
the static fleet is materially mis-sized (idle most of the time, or
chronically backlogged).

### Question 10 — Number of memory tiers

The doc references "a higher memory tier" for OOM retries. How many tiers?

A) 2 tiers (e.g. 2 GB / 8 GB)
B) 3 tiers (e.g. 2 GB / 8 GB / 32 GB)
C) 4 tiers (2 GB / 8 GB / 16 GB / 64 GB)
D) Recommend tier sizes based on Aspose's 2–20× amplification factor and target inputs
X) Other (please describe after [Answer]: tag below)

[Answer]: X — Single 2 GB tier with **swap enabled** as OOM cushion. Subdivision
remains the retry strategy. (Swap requirement confirmed by user 2026-05-11; aligns
with the design doc's "swap is the backstop" guidance.)

**Rationale:** This is the most consequential deviation from the design doc on
worker memory: there is no higher tier to promote into. The substitute strategy
is conservative chunking (Q11) + subdivision-on-OOM (below) + **swap as an
intermediate cushion**, in that order of preference. Swap is *not* a primary
mechanism; it's a soft buffer that prevents borderline chunks from triggering
the harder subdivision retry path.

**Memory hierarchy on worker pod (load-bearing for NFR/Infra design):**

| Layer        | Size      | Purpose                                          |
| ------------ | --------- | ------------------------------------------------ |
| RAM (limit)  | 2 GB      | Cgroup memory limit per pod (Q10 constraint)     |
| Swap         | 2–4 GB    | OOM cushion for borderline chunks (this answer)  |
| TempFolder   | sized to chunk PDFs in flight | Aspose `LoadOptions/SaveOptions::TempFolder` spill (design doc) |

**Why swap is the right backstop here, not a replacement for chunking:**

- Aspose's 2–20× amplification is mostly bounded — extreme cases (PPTX with
  huge embedded media) blow past any small multiple of the RAM limit. Swap
  fills the gap between the "typical 5× amplification" case (which would
  fit in 2 GB anyway) and the "rare 20× amplification" case (which fits in
  ~10 GB RAM+swap and only thrashes briefly).
- Hitting swap slows render — potentially by 10× or more on a heavily
  swapping path. But slow render is much better than OOM kill: an OOM
  kill = chunk failure = subdivision retry = at least 2× total wall time
  + worker churn. A swap-thrashing render that completes in 60 s on what
  should have been 5 s is still a single chunk completion, no retry.
- Crucially: the 15 min p95 (Q2 = D) has headroom for swap thrash. A
  worst-case single chunk taking 60 s of swap-heavy render still fits
  comfortably under the 15 min ceiling for the full 10-chunk typical job.

**Failure cascade:**

1. Initial chunk plan is sized conservatively (Q11) so >99% of chunks fit
   in 2 GB RAM alone, no swap touched.
2. ~1% of chunks (the worst-case-amplification tail) spill into swap and
   complete slowly. Acceptable.
3. If a chunk OOMs even with swap (working set genuinely exceeds RAM +
   swap), the orchestrator **subdivides** the failing page range
   (10 → 2×5 → 2×2 → 2×1 pages) and re-dispatches.
4. Subdivision floor at single-page granularity. A single page that won't
   render in 2 GB RAM + 4 GB swap = 6 GB working set is genuinely
   pathological and dead-letters (see Q18).

**EKS operational requirements introduced by enabling swap:**

- **Node OS must support cgroupv2.** Bottlerocket and Amazon Linux 2023
  do; Amazon Linux 2 does not. Node group choice is now load-bearing.
- **Kubelet config:** `failSwapOn: false` and
  `memorySwap.swapBehavior: LimitedSwap` (NOT UnlimitedSwap — limiting
  per-pod swap to the memory-limit's worth prevents one pod from
  starving others on the same node).
- **Custom node bootstrap or AMI** required to enable and size swap on
  the node. Standard EKS-optimized AMIs don't ship with swap enabled.
- **Swap storage on local NVMe instance store**, not EBS. EBS-backed
  swap is orders of magnitude slower and would turn the "soft cushion"
  into "hard performance cliff". This pins the worker node group to
  instance types with local NVMe (e.g. `m5d`, `m6id`, `r5d`).
- **Sizing rule from the design doc:** "Undersized swap is worse than
  no swap." Recommend 2–4 GB swap per pod (1–2× memory limit). At N=4
  workers per node, that's 8–16 GB swap per node — small relative to
  any NVMe-backed instance.

**Observability cascade to Q17:** add `worker_swap_used_bytes` and
`worker_swap_in_pages_per_second` to the worker metric list. Chronic
swapping (sustained > 50% of swap capacity for > 5 min) is an alerting
condition; it signals the chunk planner's MB-bound is mis-estimating
amplification for some format and Q11 needs revisiting.

**Risk to flag in requirements.md:** swap is a soft cushion, not a
panacea. Single-page PPTX slides with very large embedded media may
still exceed 2 GB RAM + 4 GB swap even at minimum granularity.
Mitigation options for NFR design: (a) Aspose's per-format memory
knobs more aggressively (`MemoryOptimization`, `BlobManagementOptions`),
(b) media stripping pre-render, (c) carve out a single "fat worker"
pool as an exception. Document the tradeoff; don't silently widen the
ceiling.

### Question 11 — Chunk size policy defaults

Concrete chunk-bound defaults. The doc says "bounded by both page count and
estimated memory cost"; what defaults should we ship?

A) Conservative — 10 pages or 50 MB estimated rendered size, whichever is smaller
B) Balanced — 25 pages or 150 MB estimated rendered size
C) Aggressive — 50 pages or 500 MB estimated rendered size (fewer chunks, faster merge)
D) Per-format tuning — different defaults for DOCX vs PPTX vs XLSX vs PDF
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** Direct consequence of the 2 GB ceiling and Aspose's 20× worst-case
amplification. 50 MB input × 20 = 1 GB peak RAM, leaving ~800 MB headroom for OS,
runtime, and Aspose's own footprint — comfortable but not lavish. 10 pages is the
right page-count bound because the page-count check is what catches "small input
size, weirdly heavy single page" cases that the MB check misses. B (25/150) bets
that amplification stays near typical (5×), which is exactly the assumption that
breaks on the documents most likely to OOM. We'd rather pay the qpdf merge cost
(many small chunks) than the OOM cost (failed chunks → subdivision retry → wall-
time hit). Per-format tuning (D) is a defensible later optimization once we have
real telemetry on per-format amplification; ship Conservative as the default and
add format-specific overrides in the NFR-design stage if data warrants.

**Note on cross-question pressure:** Q2's relaxation to 15 min p95 *does not*
relax this answer. Chunk size is bound by *RAM*, not by latency. The temptation
under a generous latency budget is to widen chunks (fewer chunks → faster merge);
that temptation is wrong here because RAM is binding. The 15 min budget is spent
on cold-start and worker contention, not on chunking granularity.

### Question 12 — Format-specific split strategy

The doc lists "format-specific splitting" under Aspose's responsibilities. Confirm
the split strategy per format:

A) Use natural seams (Word section breaks, Excel sheets, PPT slide ranges, PDF page ranges) wherever possible
B) Use pure page-range splits everywhere (simpler, predictable; ignores semantic boundaries)
C) Use natural seams for Word/Excel/PPT but page-range for already-PDF inputs
D) Hybrid: natural seams *if* they produce balanced chunks, fall back to page-range
X) Other (please describe after [Answer]: tag below)

[Answer]: D

**Rationale:** Natural seams (A) are the right *first* choice — they tend to be the
boundaries Aspose itself handles cleanly internally — but they can produce wildly
unbalanced chunks (one Word section that's 80% of the document, or a single huge
Excel sheet). With a 2 GB ceiling, an unbalanced chunk is an OOM waiting to happen.
The hybrid policy: try natural seams first; if the largest resulting chunk exceeds
the Q11 bounds, fall back to page-range splitting within that region. This is
deterministic (same input → same plan) and produces the predictable upper bound
chunking needs. Document the fallback threshold in the chunk planner spec.

---

## E. Caching and Idempotency

### Question 13 — Cache scope and TTL

What gets cached and for how long?

A) Final output only — keyed by source SHA-256, TTL 30 days
B) Final output + per-chunk PDFs — chunk TTL 7 days, final TTL 30 days
C) Final output + per-chunk + probe results — probe TTL 90 days, chunk 7d, final 30d
D) No cache — every job re-renders (simplest; fine for low-throughput PoC)
X) Other (please describe after [Answer]: tag below)

[Answer]: B — Final output + per-chunk PDFs. **Scoped per-tenant by default.**

**Rationale:** Rendering is the expensive operation under a 2 GB ceiling — every
saved render directly translates into pod-hours saved. Final-output cache covers
the "same file submitted twice" case; per-chunk cache adds substantial value
because chunk hashes overlap across slightly-edited documents (common in
revision-heavy workflows). Probe cache (C) is cheap to compute and adds little;
defer to keep the cache key surface area small. 7d/30d TTLs match S3 lifecycle
policy ergonomics.

**Tenancy scope decision (cascade from Q4 = B):** cache keys include `tenant_id`
by default. Final-output key: `cache/<tenant_id>/final/<source_sha256>.pdf`.
Per-chunk key: `cache/<tenant_id>/chunks/<chunk_sha256>.pdf`. Identical document
submitted by two tenants renders twice; zero cross-tenant leak risk.

**Why per-tenant default rather than cross-tenant content-addressable:**

- A cross-tenant cache keyed by raw content SHA-256 is a probing oracle:
  tenant A submits document D, measures latency; if fast, infers tenant B has
  previously rendered D. Unacceptable for confidential inputs.
- The compute savings of cross-tenant sharing only matter for documents that
  multiple tenants converge on (shared templates, public forms). No such use
  case has been stated.

**Future opt-in path:** if a use case appears where inputs are demonstrably
public (e.g. shared template library), a per-tenant `cross_tenant_cache: true`
opt-in flag could move those inputs into a shared namespace. Out of scope for v1;
flagged here so the cache key structure is forward-compatible.

### Question 14 — Cache invalidation control

Can callers force a re-render?

A) Yes — `?nocache=true` (or equivalent flag) bypasses the cache and re-renders
B) No — cache lookup is unconditional; new render only on hash miss
C) Yes, but only via an admin/operator API, not per-caller
X) Other (please describe after [Answer]: tag below)

[Answer]: A — caller-level bypass. **Mechanism: `options.nocache: true` in the
SQS submit-message body** (cascade from Q5 = D; no query string to set).

**Rationale:** Hash-keyed caches occasionally get wedged by upstream bugs (bad
Aspose version, corrupted chunk in cache, mid-flight policy change). A caller-
level escape hatch is cheap, operationally invaluable during incidents, and
trivially auditable. Under Q5 = D the mechanism is a JSON field in the submit
message, not a URL parameter; semantics unchanged.

**Abuse risk under Q6 = X (no auth in v1):** Without per-tenant quotas
enforced, a caller setting `nocache: true` on every job has no per-tenant
rate limiter to stop them. Compensating control in v1 is the natural
ceiling imposed by Q9 = A — the static worker fleet processes at fixed
rate, so a runaway caller can only grow the queue, not consume more
worker capacity than is provisioned. A CloudWatch alarm on `nocache=true`
submission rate per `Attributes.SenderId` alerts ops if a single principal
exceeds a configurable threshold. v2 with auth restores per-tenant quota
enforcement and complements the static-fleet defense.

Operator override: an admin-only "global bypass" toggle (effectively
pretending every cache lookup missed) is a separate concern, not part of
the caller API. Implemented as a feature flag in the orchestrator config
rather than a message field. Flag for NFR-design discussion.

---

## F. Output Format and Post-Processing

### Question 15 — Output PDF profile

What output PDF flavor must the service produce?

A) Generic PDF 1.7, no post-processing
B) Linearized PDF 1.7 (fast web view) via qpdf post-process
C) PDF/A-2b (archival) for all outputs — requires a PDF/A-capable post-processor
D) Caller-selectable per job: generic / linearized / PDF/A-2b
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** No caller has asked for linearization or archival. Each post-process
step adds wall-time and another binary in the pipeline. Ship A; add B/C/D when a
concrete caller use case appears. qpdf is already on the merge path so adding
linearization later is a config flag, not a re-architecture.

### Question 16 — Digital signing

Is the service expected to sign output PDFs?

A) No signing
B) Optional caller-requested signing with a service-managed key
C) Caller supplies signing material; service signs with it
D) Required signing on every output
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** Not mentioned in the design doc, no caller has stated this need.
Signing adds key-management surface area, KMS dependencies, and per-job latency. If
required later, B is the right pattern (caller supplies key material via KMS grant);
defer until requested.

---

## G. Observability, Reliability, and Compliance

### Question 17 — Metrics / tracing stack

What observability stack does the existing platform use?

A) Prometheus + Grafana + OpenTelemetry traces (Tempo / Jaeger)
B) AWS CloudWatch Metrics + X-Ray
C) Datadog (metrics, traces, logs)
D) New Relic / Dynatrace / other APM
X) Other (please describe after [Answer]: tag below)

[Answer]: B

**Rationale:** EKS deployment with no other stack signal → AWS-native is the
zero-friction default. CloudWatch Container Insights is one-click on EKS, X-Ray
gives per-chunk trace spans for the parallel render fan-out, and we already
authenticate via IAM (Q6 = A) so the integration is trivial. If the existing
platform runs Prometheus (A), flip — Prom + OTel is the better technical answer,
just not the better default-without-context answer.

**Load-bearing dimensions and metrics (cascade from Q4 = B, Q5 = D, Q9 = A):**

- All custom metrics carry `tenant_id` as a CloudWatch dimension. Required for
  per-tenant alerting, observability, and cost attribution. Under Q6 = X (v1),
  `tenant_id` is caller-asserted — useful for organization and cost
  attribution but not authoritative for security decisions.
- **SQS queue metrics are first-class** — under Q9 = A (static fleet) they
  are *alerting* signals, not autoscaling signals: when
  `ApproximateNumberOfMessagesVisible` or `ApproximateAgeOfOldestMessage`
  on the chunk-dispatch queue exceeds a configurable threshold (suggest
  5 min dwell time), page the operator to manually scale. DLQ depth is
  alerting-critical (Q18).
- **Per-job custom metrics:** `job_chunks_total`, `job_chunks_completed`,
  `job_subdivision_retries`, `job_render_duration_seconds`,
  `job_end_to_end_duration_seconds`. All dimensioned by `tenant_id` and
  `input_size_bucket` (≤100 MB / 100 MB-1 GB / >1 GB — matches tiered SLO).
- **Aspose worker metrics:** `worker_chunk_render_seconds`,
  `worker_oom_events_total`, `worker_temp_folder_bytes_high_watermark`,
  `worker_swap_used_bytes`, `worker_swap_in_pages_per_second` (cascade
  from Q10 = X with swap). Dimensioned by `format` (DOCX/PPTX/XLSX/PDF)
  to detect format-specific amplification regressions. Chronic swap
  usage (>50% capacity sustained >5 min) is an alerting condition: it
  signals the chunk-planner MB-bound is mis-estimating amplification
  for some format and Q11 needs revisiting.
- **X-Ray trace spans:** SQS receive → orchestrator job span → N chunk render
  sub-spans → qpdf merge span → S3 multipart upload span → completion event
  publish. Tenant ID and correlation ID are trace annotations.

### Question 18 — Dead-letter destination

When chunk subdivision finally fails, where does the job dead-letter?

A) SQS dead-letter queue with operator runbook
B) S3 "failed jobs" bucket with original input + failure metadata + alert
C) Both A and B (queue for ops, bucket for forensic record)
D) Stack trace logged + page to on-call, no persistent DLQ
X) Other (please describe after [Answer]: tag below)

[Answer]: C — Both SQS DLQ + S3 forensic bucket, scoped per-tenant.

**Rationale:** Under a 2 GB ceiling, the dead-letter path is *expected* to fire
occasionally (see Q10 risk note about pathological single-page assets). We want
both halves, but the cascade from Q5 = D now splits responsibilities cleanly:

**SQS native redrive** (free with Q5 = D, no custom code):

- Per-tenant submit queue `aspose-jobs-<tenant-id>` configured with a redrive
  policy → `aspose-jobs-<tenant-id>-dlq` after `maxReceiveCount` (suggest 3)
- Handles **transient orchestrator-side failures**: pod crashes,
  DynamoDB throttling, downstream timeouts. Message reappears for redelivery
  automatically; only failures across multiple attempts end up in the DLQ.
- DLQ depth is an alerting metric (Q17) and drains via operator runbook.

**S3 forensic bucket** `s3://aspose-dlq/<tenant_id>/<correlation_id>/`:

- Captures **non-transient failures**: chunk subdivision floor reached (Q10),
  ingest validation rejection (Q3), input format unsupported, Aspose-license
  expired, etc.
- Contents per failed job: original input file, full chunk plan, per-chunk
  status, last 1 MB of orchestrator logs for the job, last 1 MB of worker
  logs for the failed chunk(s). Sufficient to reproduce in isolation.
- Tenant-scoped prefix so operator forensic access goes through an admin role
  that explicitly opts into cross-tenant read.

**Why both:** SQS DLQ alone (A) loses the forensic input — which is exactly
what we need to harden the chunk planner. S3 alone (B) loses the redrive
semantics (auto-retry on transient orchestrator failures). Stack-trace-only
(D) is fine for a PoC but loses the forensic input. C is the only choice that
gives both layers their natural job.

**Completion event on dead-letter:** the orchestrator publishes a terminal
`{correlation_id, status: "failed", failure_class, s3_forensic_uri}` event
to the caller's callback ARN (if any) and writes the same to the status
table. Callers learn about failures the same way they learn about successes.

### Question 19 — Region and DR posture

What region/DR posture is required?

A) Single region, single AZ — best for cost, lowest availability
B) Single region, multi-AZ — standard production posture
C) Active-active across two regions
D) Active-passive across two regions with failover
X) Other (please describe after [Answer]: tag below)

[Answer]: B

**Rationale:** Standard production posture; matches the default EKS deployment
pattern (node groups span AZs). Active-active (C) is overkill without a stated
RPO/RTO and doubles steady-state cost. Single-AZ (A) is a footgun on EKS — node-
group failures take the whole service down. B is the right floor; revisit on a
real DR requirement.

### Question 20 — Data residency / compliance

Are there compliance constraints on the input documents?

A) None declared — internal documents only
B) SOC 2 controls required (logging retention, access control)
C) HIPAA — input may contain PHI; encryption + BAA-eligible services only
D) GDPR — EU data must stay in EU regions; data-subject-deletion API needed
E) Multiple of the above (please describe after [Answer]: tag below)
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** None stated. Worth flagging explicitly to the user: compliance is
the kind of requirement that's expensive to retrofit (KMS-only encryption, log-
scrubbing, residency-aware S3 bucket selection), so we'll bake the *cheap* parts
in regardless (KMS-encrypted buckets, no PII in logs, S3 server-side encryption
by default) and flag the expensive parts as deferred. If any constraint applies,
say so and we revise.

---

## H. Build, Deploy, and Operations

### Question 21 — Aspose.Total C++ license model

How is the Aspose license provisioned?

A) Single floating license file mounted from Kubernetes Secret on every Aspose pod
B) Per-pod licensed nodes (license tied to MAC / instance ID)
C) License server / metering service called at pod startup
D) Trial / evaluation mode only for now (PoC)
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** Standard Aspose deployment pattern. Secret-mounted license is fleet-
size-agnostic (no per-pod registration), survives pod restarts cleanly, and rotates
via standard Secret-update workflows. B couples licensing to node identity, which
fights every autoscaler primitive in Kubernetes. C adds a startup dependency and a
single point of failure. D only fits a stated PoC scope, which has not been stated.

### Question 22 — CI/CD platform

What CI/CD platform builds and deploys this service?

A) GitHub Actions
B) GitLab CI
C) AWS CodePipeline + CodeBuild
D) Argo CD / Flux (GitOps) on top of one of the above for the build stage
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** Modal choice in 2026 AWS shops; integrates with ECR (Q23) and IAM
via OIDC without long-lived credentials. C (CodePipeline) is a defensible
alternative if the existing platform is heavily Code* family; A is the safer
default-without-context.

### Question 23 — Container registry

Where do the service images live?

A) Amazon ECR
B) GitHub Container Registry (ghcr.io)
C) Docker Hub (private)
D) Internal Harbor / Artifactory
X) Other (please describe after [Answer]: tag below)

[Answer]: A

**Rationale:** EKS + AWS → ECR is the only registry inside the same VPC, so pulls
are free, fast, and don't traverse the public internet. IAM auth from EKS node
role is native. No reason to pick anything else without an existing-platform
signal that contradicts.

---

## I. Property-Based Testing Targets

(You opted in to PBT as a blocking constraint. These questions tune what gets PBT
coverage.)

### Question 24 — PBT critical surfaces

Which surfaces should have the deepest property-based test coverage?

A) Chunk-planning algorithm only (determinism, page-range coverage, monotonic ordering)
B) Chunk planner + qpdf concat wrapper (round-trip: split N chunks → concat → page-count equals N)
C) Chunk planner + concat + S3 multipart streaming (offset alignment, retry idempotency)
D) All of the above plus probe-cost estimation (invariants on estimate function)
X) Other (please describe after [Answer]: tag below)

[Answer]: D — All of the above, expanded with SQS-driven surfaces (cascade from
Q5 = D).

**Rationale:** Each surface has crisp invariants worth proving across random
inputs. PBT is the right tool because the interesting failures are boundary
cases (single-page docs, empty sections, page counts that match chunk-size
exactly, retries at exactly the part boundary, duplicate SQS deliveries at
exactly the wrong state transition). Hand-written examples miss these
systematically. The 2 GB ceiling makes chunk-planner correctness load-bearing
(planner bug → OOM → dead-letter), so PBT pays off.

**Core surfaces from option D:**

- **Chunk planner**: total page coverage = input page count; non-overlapping
  ranges; monotonic ordering; chunk MB-estimate ≤ Q11 ceiling; subdivision
  halves page ranges deterministically.
- **qpdf concat wrapper**: page count of concat output = sum of input page
  counts; page-order preserved; concat is associative
  (`concat(a, concat(b, c)) = concat(concat(a, b), c)`).
- **S3 multipart**: byte offsets in upload parts align to part boundaries;
  retry of a part-upload is idempotent; checksums match on completion;
  output > 5 GB still uploads correctly (Q3's tier ceiling).
- **Probe-cost estimation**: monotonic in page count and uncompressed input
  size; `estimate(a) + estimate(b) ≥ estimate(a ∪ b)` (subadditivity catches
  planning bugs where a heavy seam gets miscredited).

**Surfaces added by Q5 = D (SQS-driven):**

- **SQS consumer idempotency**: any submit message redelivered (visibility
  timeout, network blip, manual redrive) MUST NOT cause duplicate work or
  duplicate completion events. Invariant: orchestrator's first action on a
  consumed message is a DynamoDB conditional write
  `(tenant_id, correlation_id) IF NOT EXISTS`; second delivery sees the row
  and skips. Test: random sequence of submits + redeliveries → at-most-once
  external side effects per correlation_id.
- **Status state machine** (DynamoDB-backed): only forward transitions
  permitted: `pending → in_progress → chunks_running → merging → completed`
  or `* → failed`. Concurrent writers (orchestrator pod restart mid-job)
  cannot regress state. Test: random interleaving of two consumer pods
  processing the same message → final state is consistent.
- **Completion-event delivery semantics**: at-least-once to caller callback
  ARN (SQS guarantee). Invariant: completion event is published *after* the
  DynamoDB row reaches a terminal state and the S3 output is fully uploaded;
  no event ever points to an output that doesn't exist. Test: orchestrator
  crash injected at every step → invariant holds.

**Surfaces explicitly NOT in scope for PBT** (covered by example-based tests):

- Aspose render correctness (visual fidelity) — PBT can't generate plausible
  Office documents
- IAM-principal-to-tenant mapping — example-based with golden test vectors

---

## J. Open / Free-Form

### Question 25 — Anything else?

Any other constraint, preference, or context I should capture (existing internal
libraries we should reuse, naming conventions, prior services to imitate, etc.)?

[Answer]: Items worth surfacing explicitly into requirements.md:

1. **The 2 GB ceiling is a hard, non-negotiable constraint** and should be
   recorded as a *requirement*, not an assumption. It propagates into the chunk
   planner (Q11), retry strategy (Q10), and dead-letter expectations (Q18).
2. **The chunk-planner subdivision policy needs an explicit floor.** Single-page
   chunks that still exceed 2 GB exist in the pathological tail (PPTX slides
   with huge embedded video; XLSX sheets with millions of rows on one logical
   "page"). The requirements doc should declare what the service does at that
   floor: dead-letter with diagnostic metadata (Q18), and *not* silently widen
   the memory ceiling. NFR design will weigh pre-render media stripping or an
   exception "fat worker" pool, but that is a downstream decision; the
   requirement is that the floor is well-defined and observable.
3. **Caller integration story is now a first-class requirements artifact**
   (cascade from Q5 = D). Because there is no HTTP endpoint, the only way a
   caller knows how to consume the service is through a documented
   integration guide. requirements.md should include:
   - Baseline AWS IAM policy template for `sqs:SendMessage` on the submit
     queue and `sqs:ReceiveMessage` on the caller's callback queue (if used)
     — needed for any AWS principal to talk to SQS at all, separate from
     application-layer auth
   - Example submit message (canonical JSON schema with all fields)
   - Example completion-event consumer (Python and CLI flavors)
   - DynamoDB status-table read pattern with example IAM policy
   - Expected error classes and failure-state semantics
4. **Cross-tenant cache opt-in is a forward-compatibility requirement.** The
   per-tenant cache key layout (Q13) must include a tenant prefix so a future
   `cross_tenant_cache: true` flag can move opt-in tenants into a shared
   namespace without re-keying existing entries.
5. **Tiered SLO must be documented in the public API contract** so callers
   know what to expect: ≤100 MB inputs get the 15 min p95; intermediate
   tier is measured but not committed; >1 GB is best-effort. Required
   because Q5 = D defers all of these signals to the status-table /
   callback path, so callers need to know how long to wait before treating
   silence as a problem.
6. **v1 → v2 auth migration is a top-line requirement, not a future-feature
   bullet** (cascade from Q6 = X). The migration plan must be in
   requirements.md so it's not silently forgotten:
   - **v1 acceptance:** `tenant_id` is caller-asserted; the data model
     supports per-tenant isolation but the orchestrator does not validate
     sender identity against the claim. Risks documented in operator
     runbook (cross-tenant pollution by misconfigured principal; no
     per-tenant quota enforcement).
   - **v2 acceptance:** orchestrator reads `Attributes.SenderId` from
     consumed SQS messages, consults a `principal → tenant_id` mapping
     table, and rejects messages where claimed `tenant_id` ≠ mapped
     `tenant_id` with `failed` status and `failure_class:
     'tenant_mismatch'`. Per-tenant quotas activate. Presigned URL TTL
     restored from v1's tightened 1 h to 24 h.
   - **Trigger for v2 work:** any of (a) compliance requirement appears
     (Q20 transitions away from A), (b) a second tenant onboards, (c)
     CloudWatch alarms on cross-message-source rate trip in production.

If any of the above is wrong about the actual existing-stack constraints
(Q8, Q17, Q22) or about caller assumptions, correct me and I'll re-derive
the dependent answers.

---

**When you're done**, reply "answered" (or just paste the file back), and I'll
analyze for contradictions and either ask follow-ups or proceed to generating
`requirements.md`.
