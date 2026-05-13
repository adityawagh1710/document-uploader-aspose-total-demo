# Personas — office-converter (Local v1)

Three personas. Two are direct users of the service (Pipeline
Developer and DevOps Operator). One is indirect (Upstream End
User) and is named here so v2 cloud work has a clean extension
point.

---

## Persona 1: Pipeline Developer (Priya)

**Archetype**: backend or data engineer integrating the converter
into an automated pipeline.

**Role / Title**: Software Engineer, Data Engineer, or Platform
Engineer.

**Technical context**:

- Writes the calling code in Python, Go, Node.js, or shell.
- Runs the converter via Docker locally during development;
  deploys it as part of a larger pipeline in some production-like
  environment.
- Reads HTTP response codes, response headers, and structured
  error bodies. Writes retry logic and observability hooks.

**Goals**:

- Submit Office documents to the service and receive PDFs reliably.
- Understand exactly why a conversion failed when one fails.
- Correlate request-side logs with the converter's logs via a
  shared request ID.
- Avoid surprises: stable HTTP contract, documented error classes,
  no silent watermarking.

**Daily concerns**:

- "Will the API contract change?" — wants explicit version /
  stability commitments.
- "How do I tell a retry-able failure from a permanent one?"
- "What size inputs are safe?"
- "How long will a typical conversion take, and how do I time-bound
  it from my side?"

**Pain points to mitigate**:

- Vague error responses (avoided via structured Diagnostic JSON).
- Response timeouts on slow conversions (mitigated by streaming
  + documented client-side timeout requirement).
- Cache invalidation surprises (mitigated by `options.nocache`).

**Will NOT do in v1**:

- Authenticate. v1 has no app-layer auth; Priya submits messages
  on trust within the local trust boundary.
- Set per-tenant quotas. v1 has only global concurrency limits.

---

## Persona 2: DevOps Operator (Otto)

**Archetype**: the human who runs the container in some production-
like environment, manages secrets and licenses, watches the logs.

**Role / Title**: DevOps Engineer, SRE, Platform Operator.

**Technical context**:

- Runs `docker run` (or `docker compose up` / similar) with the
  appropriate bind-mounts for license and scratch.
- Configures env vars (`OFFICE_CONVERT_*`).
- Monitors logs (stdout via Docker logs or a sidecar).
- Periodically rotates the Aspose temp license (30-day expiry).
- Handles capacity tuning (`max_jobs`, `parallel`).

**Goals**:

- Keep the service healthy and available.
- Renew the Aspose temp license before requests start failing.
- Diagnose what's wrong when a caller complains.
- Scale up if the queue grows; scale down to save resources.
- Trust the security posture (non-root, read-only root, no
  capabilities).

**Daily concerns**:

- "Is the service ready RIGHT NOW?" — wants a `/health` they can
  poll.
- "When does the license expire?" — needs progressive warnings.
- "Why did this specific request fail?" — needs request_id
  correlation between caller logs and server logs.
- "Can I bump capacity without a restart?" — capacity is
  configured at startup; restart with new env vars.

**Pain points to mitigate**:

- Mysterious failures with no diagnostic (mitigated by structured
  failure-class JSON + request_id).
- License expiring without warning (mitigated by hybrid state
  machine: ≤7d WARN, ≤1d ERROR, EXPIRED 503).
- Disk filling up from the cache (documented operator concern;
  no auto-eviction in v1).

**Will NOT do in v1**:

- Manage multiple tenants. v1 is single-tenant.
- Configure metrics scraping. v1 has no `/metrics` endpoint; Otto
  derives metrics from logs.
- Hot-reload configuration. Env-var changes require restart.

---

## Persona 3: Upstream End User (Uma) — INDIRECT

**Archetype**: a person whose document flows through some upstream
system (a SaaS tool, an internal portal, a batch job) that uses
the converter under the hood. **Uma never touches the converter
directly.**

**Why named anyway**: v2 cloud work will turn Uma into a "Tenant
Admin" persona with auth, quotas, and direct API access. Naming
the indirect user in v1 keeps the v2 extension point clean.

**v1 expectations of Uma's experience** (mediated entirely through
Priya's pipeline):

- Submits an Office document via her upstream system.
- Receives a converted PDF some time later.
- Does not see request IDs, error codes, or HTTP responses;
  any failures are communicated by Priya's system in its own way.

**Goals (transitive — Priya's pipeline must serve these)**:

- The PDF matches the source document's content.
- Conversion completes in a reasonable time, even for large inputs.
- The service refuses inputs it can't handle, rather than silently
  producing bad output.

**No direct v1 stories**: every Uma concern is owned by a Priya
story (e.g., "I get back a faithful PDF" → Priya's "Submit and
receive PDF" story). Uma exists in this document purely as a
forward-compatibility marker.

---

## Persona Coverage Matrix

| FR / NFR  | Priya (Pipeline Dev) | Otto (Operator) | Uma (Indirect) |
| --------- | :------------------: | :-------------: | :------------: |
| FR-1 Convert endpoint           | ●  primary | ○ | ○ |
| FR-2 Health endpoint            |            | ● primary | |
| FR-3 Chunked render algorithm   | ●          |   | ○ |
| FR-4 Subdivision-on-OOM retry   | ●          |   |   |
| FR-5 Structured failure         | ● primary | ● |   |
| FR-6 Subprocess isolation       |            | ● |   |
| FR-7 Cache                      | ●          | ● |   |
| FR-8 License lifecycle          |            | ● primary | |
| FR-9 Concurrency control        | ●          | ● |   |
| FR-10 Structured logging        |            | ● primary | |
| NFR-1 Memory ceiling            |            | ● |   |
| NFR-3 Input size limit          | ● primary  |   |   |
| NFR-5 Determinism               | ●          |   |   |
| NFR-6 Testability               |            | ● |   |
| NFR-7 Packaging                 |            | ● primary | |
| NFR-8 Trust boundary            |            | ● primary | |

● = persona has a direct story for this requirement.
○ = persona has indirect interest; covered by another persona's
story or by composition.
