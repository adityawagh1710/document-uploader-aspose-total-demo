# User Stories — office-converter (Local v1)

Stories organized by persona. Each carries Gherkin acceptance
criteria and cross-references to FR/NFR numbers in
`requirements.md`. INVEST criteria observed: stories are
Independent, Negotiable, Valuable, Estimable, Small (within v1
scope), Testable.

---

## Pipeline Developer (Priya)

### US-PD-01: Convert an Office document to PDF

**Story**: As Priya, I want to submit an Office document via HTTP
multipart upload and receive a streamed PDF response so that I can
integrate document conversion into my pipeline without managing
the conversion process myself.

**Traceability**: FR-1, FR-3, NFR-1

**Acceptance criteria**:

```gherkin
Scenario: small DOCX converts successfully
  Given the converter is running with a valid Aspose license
  And I have a 10-page DOCX file under 5 MB
  When I POST it to /convert with multipart Content-Type
  Then I receive 200 OK
  And the response Content-Type is application/pdf
  And the response Transfer-Encoding is chunked
  And the response body is a valid PDF with 10 pages
  And the response carries an X-Request-ID header
  And the response carries X-Chunks-Rendered, X-Subdivision-Retries,
      X-Cache-Hits, X-Duration-Seconds headers

Scenario: each supported format converts
  Given the converter is running
  When I submit one of: DOCX, PPTX, XLSX, PDF
  Then I receive 200 OK with a valid PDF body
```

---

### US-PD-02: Receive structured failure responses

**Story**: As Priya, I want every failure to return a structured
JSON diagnostic with a stable failure class so that I can write
correct retry logic and surface meaningful errors upstream.

**Traceability**: FR-5

**Acceptance criteria**:

```gherkin
Scenario: unsupported format is rejected
  Given the converter is running
  When I POST a .png image to /convert
  Then I receive 400 Bad Request
  And the response body is JSON with shape
      {"error": "unsupported_format", "detail": ..., "request_id": ...}
  And the X-Request-ID response header matches detail.request_id

Scenario: oversized input is rejected
  Given the converter is configured with max_input_bytes=1073741824
  When I POST a 2 GB file to /convert
  Then I receive 400 with failure_class "input_too_large"

Scenario: corrupt input is rejected
  When I POST a malformed DOCX (zip header but invalid OOXML)
  Then I receive 422 with failure_class "input_unprocessable"

Scenario: server at capacity returns 503
  Given the converter has max_jobs=1 and one request is in flight
  When I POST a second request to /convert
  Then I receive 503 with failure_class "busy"
  And the response carries a Retry-After header

Scenario: license expired returns 503
  Given the Aspose license has expired
  When I POST any document to /convert
  Then I receive 503 with failure_class "license_expired"
  And detail.expired_on is the license's expiry date
```

---

### US-PD-03: Correlate client and server logs via request ID

**Story**: As Priya, I want every request to carry a unique
request ID visible in response headers AND in server logs so that
I can diagnose failures by joining my client logs with the server
logs.

**Traceability**: FR-1 (X-Request-ID), FR-10

**Acceptance criteria**:

```gherkin
Scenario: request ID is in headers and logs
  Given the converter is running with log_format=json
  When I POST a document to /convert
  Then the response carries X-Request-ID: <uuid>
  And every server log line emitted for that request carries the
      same request_id field

Scenario: failure JSON carries the same request ID
  When my request fails with any failure_class
  Then the response body's detail.request_id equals the
      X-Request-ID header
```

---

### US-PD-04: Bypass the cache for a specific request

**Story**: As Priya, I want to force a fresh render via a request
option so that I can verify a fix or work around a wedged cache
entry without operator intervention.

**Traceability**: FR-7

**Acceptance criteria**:

```gherkin
Scenario: cache bypass produces a fresh render
  Given a document has been converted and cached previously
  When I POST it with options={"cache": false}
  Then the converter renders chunks from scratch
  And response header X-Cache-Hits is 0
```

---

### US-PD-05: Submit large documents and receive streamed responses

**Story**: As Priya, I want large input documents to be processed
without the server buffering the merged PDF in memory so that my
pipeline can convert near-1 GB inputs without the server OOMing.

**Traceability**: FR-3, NFR-1, NFR-3

**Acceptance criteria**:

```gherkin
Scenario: large DOCX streams a multi-hundred-MB PDF
  Given the converter has max_input_bytes=1073741824
  And I have a 500 MB DOCX
  When I POST it to /convert
  Then the response body begins streaming via chunked transfer
      encoding within reasonable time
  And the server's memory does NOT spike by the full size of the
      output PDF (verified by monitoring server process RSS during
      the request)
  And the response is a valid PDF

Scenario: input above the ceiling is rejected before disk write
  Given max_input_bytes=1073741824
  When I POST a 2 GB stream
  Then I receive 400 input_too_large
  And the rejection occurs before the full 2 GB has been buffered
      to disk (size check is incremental)
```

---

### US-PD-06: Detect unsupported formats fast

**Story**: As Priya, I want unsupported file types rejected fast
(by magic bytes, not by extension) so that I don't waste time
uploading the full file just to learn my pipeline served the wrong
input.

**Traceability**: FR-1, NFR-3

**Acceptance criteria**:

```gherkin
Scenario: format detection happens on the first 512 bytes
  Given the converter is running
  When I begin POSTing a binary file whose first bytes do not
      match DOCX, PPTX, XLSX, or PDF magic bytes
  Then the server returns 400 unsupported_format
  And the rejection happens before the full body is buffered to
      disk

Scenario: extension does not determine format
  When I POST a file named "report.docx" whose content is actually
      a PDF
  Then the server detects format as PDF (from %PDF- magic) and
      converts successfully (or 400s if it can't), but NEVER trusts
      the .docx extension
```

---

### US-PD-07: Predictable behavior on chunked rendering

**Story**: As Priya, I want a single failed chunk inside a multi-
chunk document to either be retried by subdivision or surfaced as
a clear final failure, but never to silently produce a partial
PDF so that I never ship an incomplete output to my downstream
users.

**Traceability**: FR-3, FR-4, FR-5

**Acceptance criteria**:

```gherkin
Scenario: subdivision recovers an OOM-prone chunk
  Given a document where one chunk exceeds 2 GB worker RAM
  When the converter renders the document
  Then the failing chunk is subdivided (10 → 5 → 2 → 1 page)
  And the response is 200 with a valid PDF and
      X-Subdivision-Retries > 0

Scenario: subdivision floor failure surfaces cleanly
  Given a single-page that exceeds 2 GB worker RAM even on its own
  When the converter attempts to render that document
  Then the response is 500 with failure_class
      "subdivision_floor_exceeded"
  And detail.failing_page_range names the offending page
  And NO partial PDF is returned
```

---

## DevOps Operator (Otto)

### US-OP-01: Verify service readiness via /health

**Story**: As Otto, I want a `/health` endpoint that reports
whether the service is ready to accept conversion requests so that
I can wire it into load balancers and operator dashboards.

**Traceability**: FR-2

**Acceptance criteria**:

```gherkin
Scenario: healthy service reports ready
  Given the converter is running with a valid license
  And the worker binary, qpdf binary, Aspose .so, and scratch dir
      are all available
  When I GET /health
  Then I receive 200 OK
  And the response JSON has "ready": true
  And the response includes license_days_remaining, active_jobs,
      max_jobs

Scenario: missing dependency reports not ready
  Given the qpdf binary is missing
  When I GET /health
  Then I receive 503
  And response.ready is false
  And response.problems contains "qpdf_missing"
```

---

### US-OP-02: Monitor Aspose license expiry

**Story**: As Otto, I want progressive warnings as the Aspose temp
license approaches expiry so that I can request and install a new
license before requests start failing.

**Traceability**: FR-8

**Acceptance criteria**:

```gherkin
Scenario: license days_remaining is visible in /health
  Given the license expires in N days
  When I GET /health
  Then response.license_days_remaining equals N

Scenario: license in WARN window logs WARN per request
  Given the license has 5 days remaining
  When a request is processed
  Then the server emits a log event at level WARN with the
      days_remaining value

Scenario: license in CRITICAL window logs ERROR per request
  Given the license has 1 day remaining
  When a request is processed
  Then the server emits a log event at level ERROR
  And /health continues to return ready: true (still functional)

Scenario: license expired causes /health to flip
  Given the license expired yesterday
  When I GET /health
  Then response.ready is false
  And response.problems contains "license_expired"

Scenario: post-expiry requests fail with 503
  Given the license expired
  When I POST a document to /convert
  Then I receive 503 with failure_class "license_expired"
  And the server does NOT silently fall back to evaluation mode
      (no watermarked PDFs are produced)
```

---

### US-OP-03: Renew the license without restarting

**Story**: As Otto, I want to replace the Aspose `.lic` file at
the bind-mount path and have the new expiry recognized without
restarting the container so that I can rotate licenses without
downtime.

**Traceability**: FR-8

**Acceptance criteria**:

```gherkin
Scenario: hot license rotation
  Given the converter is running with a license expiring today
  And I overwrite /aspose/license.lic with a new license valid
      for 30 more days
  When I GET /health
  Then response.license_days_remaining is approximately 30
  And subsequent /convert requests succeed without container
      restart
```

---

### US-OP-04: Tune concurrency without code changes

**Story**: As Otto, I want to configure `max_jobs` and `parallel`
via env vars at container start so that I can scale capacity to
host resources without rebuilding the image.

**Traceability**: FR-9, NFR-8

**Acceptance criteria**:

```gherkin
Scenario: max_jobs env var caps concurrent requests
  Given the container starts with OFFICE_CONVERT_MAX_JOBS=2
  When I POST three concurrent requests
  Then two are served and the third receives 503 busy with
      Retry-After

Scenario: parallel env var caps per-job chunk concurrency
  Given OFFICE_CONVERT_PARALLEL=4
  And a job has 8 chunks
  When the job runs
  Then at most 4 chunk renders are in flight simultaneously
      (verified by counting concurrent worker subprocesses)

Scenario: invalid config fails fast at startup
  Given OFFICE_CONVERT_MAX_JOBS=0
  When the container starts
  Then the container exits non-zero before serving any request
  And the container's stderr contains a Pydantic validation error
```

---

### US-OP-05: Diagnose failures via structured logs

**Story**: As Otto, I want every failed request to log a
structured JSON event with the request_id, failure_class, and
relevant detail so that I can search logs by request ID and
understand failure causes without inspecting binaries.

**Traceability**: FR-10

**Acceptance criteria**:

```gherkin
Scenario: failed request logs a request_failed event
  When a request fails with any failure_class
  Then the server emits a log event with:
      event = "request_failed"
      request_id = <the UUID>
      failure_class = the failure class
      detail = the structured diagnostic
      level = "error"

Scenario: chunk subdivision is logged
  When a chunk OOMs and is subdivided
  Then the server emits a "subdivision_retry" event with:
      request_id, chunk_index, page_range_before, page_range_after

Scenario: log format is selectable
  Given the container starts with OFFICE_CONVERT_LOG_FORMAT=human
  When events are emitted
  Then logs are single-line human-readable, not JSON

  Given the container starts with OFFICE_CONVERT_LOG_FORMAT=json
  Then logs are JSON-lines, each event one parseable JSON object
```

---

### US-OP-06: Run the container with defense-in-depth

**Story**: As Otto, I want the image to be compatible with non-
root execution, read-only root filesystem, and dropped Linux
capabilities so that I can deploy with strong default security
posture.

**Traceability**: NFR-7, NFR-8

**Acceptance criteria**:

```gherkin
Scenario: container runs as non-root by default
  When I inspect the running container's process tree
  Then no process runs as UID 0

Scenario: container works with --read-only root
  When I run with `--read-only --tmpfs /tmp --tmpfs /var/run`
  Then the container starts and serves /convert successfully

Scenario: container works with --cap-drop=ALL
  When I run with `--cap-drop=ALL`
  Then the container starts and serves /convert successfully

Scenario: license is not in the image
  When I `docker run` the image without bind-mounting a license
  Then the container starts but /health reports
      license_path_missing OR license_invalid; no license is
      embedded in the image
```

---

### US-OP-07: Build the image with operator-supplied Aspose SDK

**Story**: As Otto, I want to build the runtime image by dropping
the Aspose.Total C++ tarball into the build context so that I
don't need network credentials or runtime downloads to produce a
deployable image.

**Traceability**: NFR-7

**Acceptance criteria**:

```gherkin
Scenario: image builds with operator-supplied tarball
  Given I have placed aspose-total-cpp.tar.gz next to the Dockerfile
  When I run `docker build .`
  Then the build completes successfully
  And the resulting image contains /usr/local/bin/office-convert-worker
  And the resulting image contains /usr/local/lib/aspose/*.so

Scenario: image build fails fast without tarball
  Given there is no aspose-total-cpp.tar.gz in the build context
  When I run `docker build .`
  Then the build fails with a clear error from the COPY step
```

---

### US-OP-08: Manage the on-disk cache

**Story**: As Otto, I want the cache directory to be optional, bind-
mountable, and content-addressable so that I can choose where to
put it, how big to let it grow, and when to delete it without
service-side coordination.

**Traceability**: FR-7

**Acceptance criteria**:

```gherkin
Scenario: cache disabled when env var unset
  Given OFFICE_CONVERT_CACHE_DIR is unset
  When I POST the same document twice
  Then the second request renders from scratch (no cache hit)

Scenario: cache enabled via env var
  Given OFFICE_CONVERT_CACHE_DIR=/cache and a writable /cache bind-mount
  When I POST the same document twice
  Then the second request reports X-Cache-Hits >= 1
  And the cache directory contains files keyed by Aspose version
      and SHA-256 hash

Scenario: cache survives container restart
  Given the cache directory is bind-mounted
  When I restart the container
  Then existing cache entries are still recognized
```

---

## Indirect: Upstream End User (Uma)

Uma has NO direct stories in v1. Her concerns are owned by Priya's
stories:

- "I want my PDF to match the source" → US-PD-01, US-PD-07
- "I don't want bad output" → US-PD-07 (no partial PDFs)
- "I want reasonable conversion times" → US-PD-05 (streaming)
- "I don't want my document silently lost" → US-PD-02 (structured
  failure)

This persona exists in `personas.md` purely as a v2 extension point.

---

## Explicit Non-Goals (v1)

What v1 explicitly does NOT promise. Pipeline developers and
operators should plan around these:

### NG-1: No application-layer authentication or authorization

The converter has no `Authorization` header check, no API key, no
JWT verification. Anyone with network access to the listening port
can submit requests. v1 trusts the local host boundary.

**Implication for Priya**: do not send credentials; the server
will ignore them. If your pipeline needs auth, put a reverse
proxy in front (the converter does not provide one).

**Implication for Otto**: bind the container's port to localhost
(`--publish 127.0.0.1:8080:8080`) or to a private network only.

### NG-2: No per-tenant or per-caller quotas

v1 has only global concurrency limits (`max_jobs`). A runaway
caller can saturate the service for other callers.

### NG-3: No metrics endpoint

There is no `/metrics`, no Prometheus client, no OpenTelemetry
trace exporter. Operators derive metrics by stream-processing the
structured logs.

### NG-4: No automatic cache eviction

The cache grows until the operator deletes files. There is no
TTL, no LRU eviction, no size cap.

### NG-5: No hot-reload of configuration

`max_jobs`, `parallel`, `cache_dir`, etc. are read at startup.
Changing them requires a container restart. Exception: the
license file IS re-read per request.

### NG-6: No HA, replication, or failover

A single container is the entire service. If the host dies,
in-flight requests die with it.

### NG-7: No SLO commitment

Performance is best-effort. There is no committed p95 latency,
no throughput guarantee, no availability target.

### NG-8: No PDF post-processing

v1 produces "generic PDF 1.7" output only. No linearization, no
PDF/A conversion, no digital signing. (See `requirements.md`
Out-of-Scope.)

### NG-9: No support for non-x86_64 hosts

Aspose.Total C++ is x86_64-Linux only. The image won't run
natively on ARM64 (it runs under emulation on Docker Desktop for
Mac, which is acceptable for dev).

---

## Story → Requirements Traceability

| Story    | Title                                  | Covers                            |
| -------- | -------------------------------------- | --------------------------------- |
| US-PD-01 | Convert document to PDF                | FR-1, FR-3, NFR-1                 |
| US-PD-02 | Structured failure responses           | FR-5                              |
| US-PD-03 | Request-ID correlation                 | FR-1 (X-Request-ID), FR-10        |
| US-PD-04 | Cache bypass per request               | FR-7                              |
| US-PD-05 | Streamed responses for large inputs    | FR-3, NFR-1, NFR-3                |
| US-PD-06 | Fast unsupported-format rejection      | FR-1, NFR-3                       |
| US-PD-07 | Predictable chunked-render behavior    | FR-3, FR-4, FR-5                  |
| US-OP-01 | /health endpoint                       | FR-2                              |
| US-OP-02 | License expiry monitoring              | FR-8                              |
| US-OP-03 | Hot license rotation                   | FR-8                              |
| US-OP-04 | Concurrency tuning via env vars        | FR-9, NFR-8                       |
| US-OP-05 | Structured failure logs                | FR-10                             |
| US-OP-06 | Container security posture             | NFR-7, NFR-8                      |
| US-OP-07 | Operator-supplied Aspose SDK build     | NFR-7                             |
| US-OP-08 | Cache lifecycle management             | FR-7                              |

15 stories total. Every FR has at least one direct story; every
NFR with caller- or operator-observable behavior has at least one
direct story.
