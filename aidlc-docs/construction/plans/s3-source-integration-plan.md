# S3 Source Integration Plan — office-convert

**Status**: Scoped, not yet implemented
**Author**: Aditya Wagh + Claude
**Created**: 2026-05-26
**Pattern**: Flow A2 (tee — stream PDF back to caller AND store in S3)

## Goal

Add S3 as a first-class input/output for `/v1/convert` without breaking the
existing multipart-upload contract.

After this lands, callers can:

1. **Direct upload (current)** — `POST /v1/convert -F file=@local.docx`
2. **S3 source** — `POST /v1/convert -F s3_input=s3://in/key.docx`
3. **S3 sink** — add `-F s3_output=s3://out/key.pdf` to either of the above

The endpoint URL stays `/v1/convert`. Input mode is selected by which form
field is present (exactly one of `file` / `s3_input` is required).

---

## Architecture overview

```
┌───────────────────────────────────────────────────────────────────────────┐
│                      CLIENT (e.g. Opus2 ingestion job)                     │
└───────────────────────────────────┬───────────────────────────────────────┘
                                    │  POST /v1/convert
                                    │
                ┌───────────────────┴────────────────────┐
                │                                        │
            MODE A: multipart upload              MODE B: S3-source
                │                                        │
   -F file=@local.docx                    -F s3_input=s3://in/key.docx
   -F s3_output=s3://out/key.pdf          -F s3_output=s3://out/key.pdf
        (s3_output optional)                 (s3_output optional)
                │                                        │
                └───────────────────┬────────────────────┘
                                    ▼
        ┌───────────────────────────────────────────────────────┐
        │             office-convert pod (FastAPI)              │
        │                                                       │
        │  ① validate (exactly one of file / s3_input)          │
        │  ② if s3_input: stream-download to scratch_dir        │
        │     (with SHA-256 computed during stream)             │
        │     if file:     write multipart body to scratch_dir  │
        │  ③ detect_format → existing dispatch logic            │
        │  ④ probe + chunk_planner + render in C++ workers      │
        │  ⑤ qpdf concat_streaming → PDF bytes flow to:         │
        │       sink 1: HTTP response                           │
        │       sink 2: disk cache (existing tee-to-cache)      │
        │  ⑥ AFTER stream completes: if s3_output, upload the   │
        │     cached PDF to S3 via boto3.upload_file (which     │
        │     auto-multiparts for files ≥8 MB internally)       │
        └───────────┬───────────────────────────┬──────────────┘
                    │                           │
                    │ HTTP byte stream          │ post-stream upload
                    ▼                           ▼
        ┌────────────────────┐    ┌─────────────────────────────────┐
        │  Client receives   │    │   S3 OUTPUT BUCKET (write)      │
        │  PDF bytes inline  │    │                                 │
        │                    │    │   s3://out/key.pdf  ←─── new    │
        │  Response headers: │    │                                 │
        │   X-Request-ID     │    │   Readable by downstream        │
        │   X-S3-Output-Key  │    │   consumers (search-indexer,    │
        │   X-S3-Output-Bkt  │    │   archive, audit) without       │
        │   (no ETag — see   │    │   coming back through           │
        │    notes below)    │    │   office-convert                │
        └────────────────────┘    └─────────────────────────────────┘
```

**Key design choice — simple upload after render (not multipart streaming).**
Rationale in §"Why not multipart streaming" below.

---

## Request shape

| Form field | Required? | Type | Notes |
|---|---|---|---|
| `file` | conditional | UploadFile | Mode A: direct upload (current behavior preserved) |
| `s3_input` | conditional | string | Mode B: `s3://bucket/key` URL |
| `s3_output` | optional | string | `s3://bucket/key`. If present, the PDF is also stored at this S3 location. Works in BOTH input modes. |
| `options` | optional | string | JSON; unchanged |

**Validation**: exactly one of `file` or `s3_input` must be provided. `s3_output`
is independent. Allowlist enforcement (`OFFICE_CONVERT_S3_INPUT_BUCKETS_ALLOWLIST`)
runs before any S3 call.

## Response shape

- **Body**: PDF bytes (streaming, chunked encoding — unchanged from current)
- **Headers** (when `s3_output` was provided):
  - `X-S3-Output-Key: <key>`
  - `X-S3-Output-Bucket: <bucket>`
  - **No `X-S3-Output-ETag`** — would require waiting for upload before sending
    headers, defeating streaming. Caller can `HEAD s3://out/key.pdf` if needed.
- **Standard headers preserved**: `X-Request-ID`, `Content-Type: application/pdf`

---

## What's stored in S3 (and what isn't)

### Stored

```
s3://opus2-incoming/    ← INPUT BUCKET  (READ-only from office-convert)
  └─ 2026/05/26/
      ├─ Q2-financials.xlsx     ← put here by upstream (sFTP, partner, etc.)
      └─ contract.docx              office-convert NEVER writes here

s3://opus2-pdfs/        ← OUTPUT BUCKET (WRITE from office-convert)
  └─ 2026/05/26/
      ├─ Q2-financials.pdf      ← teed here via boto3.upload_file
      └─ contract.pdf              (after the HTTP stream completes)
```

### NOT stored

- ❌ **Original input** — already in input bucket (S3-source) or transient
  (multipart-upload by caller's choice). No duplicate write.
- ❌ **Intermediate files** — MHTML for emails, chunk PDFs for large files.
  Transient in pod `scratch_dir`, cleaned by orchestrator `finally`.
- ❌ **Failed conversions** — no partial PDF object. boto3 handles abort.
- ❌ **Logs/telemetry** — stdout JSON → cluster logging. `/jobs/{id}/...`
  endpoints are in-memory ring buffers, 30-min retention.
- ❌ **License file** — K8s Secret, different security domain.
- ❌ **Disk cache** — pod-local, lost on restart by design.

---

## Why not multipart streaming (design decision)

Initial scope called for a streaming `S3MultipartUploadSink` running in
parallel with the HTTP response. The simpler approach won out because:

| Property | Multipart streaming sink | boto3.upload_file after render |
|---|---|---|
| Lines of S3-client code | ~150 | ~30 |
| Lifecycle rule for orphan parts | required | not required |
| Abort logic | hand-rolled | implicit via boto3 |
| Pod RAM | ~5 MB (part buffer) | 0 extra (reads from disk) |
| Total wall time | `max(render, upload)` | `render` then `upload` (~1-5s extra) |
| Test surface | abort + partial + complete | upload-ok + upload-fails |

`boto3.upload_file` automatically uses multipart **internally** for files
≥8 MB and handles retries + abort transparently. We get multipart's
resilience without writing multipart code. The 1-5s of extra wall time
on the request is acceptable for a conversion that already takes seconds.

The PDF is **already on disk** in the cache after the qpdf concat stream
completes, so the upload reads from there. No extra disk pass, no RAM
buffer.

---

## File-level diff scope (simplified)

| File | Change | LOC |
|---|---|---|
| **NEW** `office_convert/s3_client.py` | **boto3** wrapper (sync calls wrapped in `asyncio.to_thread`): `parse_s3_url`, `download_to_path` (streams `get_object` body to disk with SHA), `upload_file` (thin wrapper over `boto3.upload_file`), `generate_presigned_get_url`. | ~110 |
| `office_convert/server.py` | `file: UploadFile \| None`, add `s3_input`/`s3_output` form fields, validate "exactly one input source", route S3-source to download helper, append `X-S3-Output-*` headers. **Generic `_tee_to_s3` wrapper** around all 3 streaming paths. **New `GET /v1/downloads/presign` route.** | +130 |
| `office_convert/orchestrator.py` | **Unchanged** — tee-to-S3 lives in `server.py`'s `_tee_to_s3` wrapper, not here (covers LibreOffice + EML paths too, and decouples from the cache-only temp file). | 0 |
| `office_convert/errors.py` + `types.py` | New `FailureClass` members + `S3InputNotFoundError` (404), `S3InputForbiddenError` (400), `S3OutputForbiddenError` (400), `S3OutputUploadFailedError` (500), `S3DisabledError` (400), `S3InvalidUrlError` (400). Map to JSON Diagnostic. | +50 |
| `office_convert/config.py` | `s3_enabled: bool`, `s3_default_output_bucket: str \| None`, `s3_output_key_template: str = "pdf/{request_id}.pdf"`, `s3_input_buckets_allowlist`, `s3_output_buckets_allowlist`, `s3_presign_ttl_seconds: int = 900`, `s3_region`. | +35 |
| **NEW** `deploy/helm/office-convert/templates/serviceaccount.yaml` | ServiceAccount with `eks.amazonaws.com/role-arn` annotation (IRSA). Deployment template references the SA by name. | ~20 |
| `deploy/helm/office-convert/values.yaml` | `serviceAccount.create: true`, `serviceAccount.roleArn: ""` (operator override). New `s3.*` config keys piped into the ConfigMap. | +15 |
| **NEW** `deploy/iam/office-convert-s3-policy.json` | IAM policy template: `s3:GetObject` on input bucket, `s3:PutObject` + `s3:AbortMultipartUpload` on output bucket. | ~30 |
| **NEW** `deploy/iam/office-convert-s3-trust-policy.json` | OIDC trust policy template — parameterized with the cluster's OIDC issuer URL. | ~20 |
| `pyproject.toml` | Add `boto3` to runtime deps (~30 MB image growth); `moto[s3]` + `boto3-stubs[s3]` to `[dev]`; mypy override for `boto3.*`/`botocore.*`/`moto.*`. | +4 |
| `office_convert_ui/app.py` | `do_conversion` sends `s3_output` + captures `X-S3-Output-*` headers; history record carries `s3_bucket`/`s3_key`; history row adds "☁️ Download from S3" `st.link_button` that calls `/v1/downloads/presign`. | +60 |
| `compose.yaml` | Add `localstack` service + `AWS_ENDPOINT_URL_S3` env on the office-convert service. | +25 |
| `tests/unit/test_s3_client.py` | `moto`-mocked S3. Parse URL, download with SHA, upload-then-head, error mapping. | ~100 |
| `tests/integration/test_convert_endpoint.py` | New cases: file-only / s3_input-only / s3_input+s3_output / file+s3_output / both-inputs-error / no-inputs-error / s3_input 404 / allowlist-rejection. | +120 |
| `README.md` | Section "S3 integration — usage + IAM role setup" | +40 |
| `aidlc-docs/.../business-rules.md` | Document new request shape + allowlist + headers | +30 |

**Total**: ~550 LOC across ~15 files. Down ~200 LOC from the original
multipart-streaming scope.

---

## Implementation phases

### Phase 1 — Foundation (no S3 yet)

- Make `file` optional in `/v1/convert`; current behavior unchanged.
- Add stub form-field params for `s3_input` / `s3_output` with validation
  that rejects them with HTTP 501 "not yet implemented".
- Gate: full test suite green (141 unit + 13 PBT + 15 integration).
- **LOC**: ~50. **Time**: ~half day.

### Phase 2 — S3 client + LocalStack in compose

- `office_convert/s3_client.py` with `parse_s3_url`, `download_to_path`,
  `upload_file_to_s3`.
- Add `localstack` service to `compose.yaml`.
- Unit tests via `moto` (in-process; no Docker dependency for pytest).
- Gate: unit tests pass; `make up` brings LocalStack up cleanly; manual
  `awslocal s3 ls` works from inside the localstack container.
- **LOC**: ~180. **Time**: ~1 day.

### Phase 3 — Endpoint wiring

- Replace Phase-1 stubs with real dispatch:
  - `s3_input` → stream-download to `scratch_dir` then enter normal pipeline.
  - `s3_output` → after stream completes, `boto3.upload_file` from cache.
- Append `X-S3-Output-Key` / `X-S3-Output-Bucket` headers.
- Typed errors map to JSON Diagnostic.
- Gate: integration tests via `moto` for the 8 must-pass scenarios listed
  below.
- **LOC**: ~210. **Time**: ~1 day.

### Phase 4 — Config + allowlist + feature flag

- `Settings` additions; allowlist enforcement BEFORE any S3 call.
- `s3_enabled=False` causes endpoint to reject `s3_input`/`s3_output` with
  HTTP 400 even if a caller tries them.
- Gate: allowlist + feature-flag-off tests.
- **LOC**: ~50. **Time**: ~half day.

### Phase 5 — IRSA + Helm + IAM template

- `serviceaccount.yaml` template with `eks.amazonaws.com/role-arn` annotation.
- `values.yaml` exposes `serviceAccount.roleArn`, `s3.*` config keys.
- IAM policy + trust policy templates in `deploy/iam/`.
- Operator playbook section in README.
- Gate: `helm template` renders cleanly; smoke test `kubectl exec ... aws
  sts get-caller-identity` returns the IRSA role ARN.
- **LOC**: ~85 (chart + policies + docs). **Time**: ~half day.

### Phase 6 — Dev05 deploy + real-S3 smoke

- Operator creates IAM role + policy via Terraform/CDK/console (out of band).
- `make deploy-dev` with `--set serviceAccount.roleArn=<arn>` + S3 config.
- End-to-end smoke: real DOCX in S3 → POST `/v1/convert` → verify HTTP PDF
  + S3 object + SHA equality between stream and stored bytes.
- **Time**: ~half day (mostly IAM propagation wait).

**Total effort: 3-4 days of focused engineering** (Phase 1-4 stack
quickly; Phase 5-6 are blocked on IAM-team coordination).

### Phase 7 — Presigned-download API + UI Conversion-History wiring (added 2026-05-27)

The convert response only tells a caller *where* the PDF landed
(`X-S3-Output-Bucket` / `X-S3-Output-Key`); it does not hand back a way to
fetch it. A presigned-URL **API** closes that loop as a first-class
microservice endpoint — every client (Streamlit UI, Opus2 ingestion job,
`curl`) calls the same endpoint, and the IRSA credentials never leave the
API pod.

**New endpoint:**

```
GET /v1/downloads/presign?bucket=<bucket>&key=<key>
→ 200 {"download_url","bucket","key","expires_in_seconds","expires_at"}
→ 400 failure_class=s3_output_forbidden   (bucket not in output allowlist)
→ 400 failure_class=s3_disabled           (s3_enabled=False)
```

Implementation is one boto3 call (`generate_presigned_url("get_object", …)`)
— stateless, no fetch, no DB. **A fresh URL is minted per click** because
presigned URLs expire; that is exactly why a callable endpoint is required
rather than returning the URL once from `/v1/convert`.

**Security guardrails (load-bearing):**
- Enforce the **output-bucket allowlist BEFORE signing** — otherwise the
  endpoint is a presigning oracle for anything the pod's role can read.
- Optional key-prefix restriction (default presign key template prefix `pdf/`).
- Short TTL: `s3_presign_ttl_seconds`, default **900s**.

**UI wiring (`office_convert_ui/app.py`):**
- `do_conversion` (≈ line 1088) currently sends `files={"file": …}` only and
  **discards `resp.headers`**. Add an `s3_output` field (built from a
  UI-side toggle + default output bucket) and capture
  `resp.headers["X-S3-Output-Bucket"]` / `["X-S3-Output-Key"]`.
- Thread those through `holder → s["results"] entry → st.session_state.history`
  as `s3_bucket` / `s3_key` keys (record built ≈ line 1198).
- In the history row (≈ line 2541) add a **"☁️ Download from S3"** control
  beside the existing local `st.download_button`: on click it `GET`s
  `/v1/downloads/presign?bucket=&key=` and opens the returned `download_url`
  (`st.link_button`). The local-bytes button stays as the offline path.

**Design decision — boto3 + `asyncio.to_thread`, NOT aioboto3.** The plan's
original `aioboto3` choice is dropped. All S3 calls (download, upload,
presign) use the synchronous `boto3` client wrapped in `asyncio.to_thread`.
Rationale: (a) eliminates the moto/aiobotocore compatibility risk flagged in
§Risks; (b) `asyncio.to_thread` for blocking I/O is already an established
pattern in this codebase; (c) `boto3.upload_file` still auto-multiparts
internally. `pyproject.toml` adds `boto3` (runtime) + `moto[s3]` (dev), not
`aioboto3`.

**Tee placement correction.** S3 output applies to **all** formats, but
`server.py` has three separate streaming paths (Aspose orchestrator,
LibreOffice for odg/images, Aspose.Email for eml). The upload is therefore a
**generic `_tee_to_s3` async wrapper in `server.py`** that wraps any
`AsyncIterator[bytes]`, tees each block to its **own** temp file (independent
of the cache temp, which only exists when `options.cache and cache.enabled()`,
and independent of `scratch_dir`, which the inner generators delete in their
`finally`), and uploads after the last byte is yielded. Not a `finally` block
inside `orchestrator.py` as originally drafted.

- **LOC**: ~140 (endpoint + presign client fn + UI wiring + tests).
- **Time**: ~half day.

**Phase 7 must-pass scenarios (added to the table below):**

| # | Setup | Expected |
|---|---|---|
| 9 | `GET /v1/downloads/presign` for allowlisted output bucket+key | 200, `download_url` is a valid presigned GET that fetches the object |
| 10 | presign for a bucket NOT in the output allowlist | 400 `failure_class=s3_output_forbidden` |
| 11 | presign while `s3_enabled=False` | 400 `failure_class=s3_disabled` |
| 12 | UI conversion with `s3_output` set | history record carries `s3_bucket`/`s3_key`; S3 button calls presign endpoint |

---

## Recommended defaults (the 5 open questions, resolved)

| Question | Default | Reasoning |
|---|---|---|
| Output key strategy | `pdf/{request_id}.pdf` | Opaque, no info-leak via key. Caller overrides per-request via `s3_output`. |
| Cache key with S3 input | SHA-256 computed during the download stream | One pass, no extra IO, plugs into existing CacheManager. |
| Allowlist granularity | Bucket-level | Simpler ops; prefix-level is over-configured for the threat model. |
| Cross-account input buckets | Out of scope for v1 | Document as future work — needs role-assumption chain; ship single-account first. |
| IAM role provisioning | Manual JSON template in `deploy/iam/`, operator-applied | Helm can't safely create IAM. Matches existing dev05 pattern. |

---

## Test strategy

**Three layers, three tools, three purposes:**

```
┌───────────┬──────────────────┬─────────────┬───────────────────────────┐
│ Layer     │ Tool             │ Where       │ Catches                   │
├───────────┼──────────────────┼─────────────┼───────────────────────────┤
│ Unit      │ moto (in-proc)   │ pytest      │ S3 client correctness     │
│ Integ.    │ moto + TestClient│ pytest      │ Endpoint dispatch +       │
│           │                  │             │ headers + error JSON      │
│ Dogfood   │ LocalStack       │ make up     │ Real boto3 protocol +     │
│           │ (compose)        │             │ multipart auto-thresholds │
│ IRSA      │ kubectl exec     │ dev05       │ Role trust + SA mount     │
│ E2E S3    │ curl + aws s3    │ dev05 ALB   │ Real AWS + tee correctness│
└───────────┴──────────────────┴─────────────┴───────────────────────────┘
```

### 8 must-pass scenarios for Phase 3 integration tests

| # | Setup | Expected |
|---|---|---|
| 1 | `file` only | 200, no S3 headers |
| 2 | `s3_input` only | 200, no S3 headers |
| 3 | `s3_input` + `s3_output` | 200 + S3 headers + object in mock S3 |
| 4 | `file` + `s3_output` | 200 + S3 headers + object in mock S3 |
| 5 | both `file` AND `s3_input` | 400 "exactly one input source" |
| 6 | neither | 400 "input required" |
| 7 | `s3_input` → non-existent object | 404 JSON `failure_class=s3_input_not_found` |
| 8 | `s3_input` bucket NOT in allowlist | 400 JSON `failure_class=s3_input_forbidden` |

### LocalStack compose snippet

```yaml
services:
  localstack:
    image: localstack/localstack:3.8       # pin major; "latest" drifts
    ports:
      - "127.0.0.1:4566:4566"
    environment:
      SERVICES:       s3                   # S3-only for v1
      DEFAULT_REGION: us-east-1
      PERSISTENCE:    1                    # buckets survive restart
      DEBUG:          0
    volumes:
      - localstack-data:/var/lib/localstack
    healthcheck:
      test:     ["CMD", "curl", "-sf", "http://localhost:4566/_localstack/health"]
      interval: 5s
      retries:  12

  office-convert:
    environment:
      AWS_ACCESS_KEY_ID:     test
      AWS_SECRET_ACCESS_KEY: test
      AWS_REGION:            us-east-1
      AWS_ENDPOINT_URL_S3:   http://localstack:4566
      OFFICE_CONVERT_S3_ENABLED: "true"
      OFFICE_CONVERT_S3_INPUT_BUCKETS_ALLOWLIST: "test-in"
    depends_on:
      localstack:
        condition: service_healthy

volumes:
  localstack-data:
```

### LocalStack dogfood workflow

```bash
make up

# Create buckets (three equivalent options):
aws --endpoint-url=http://localhost:4566 s3 mb s3://test-in
aws --endpoint-url=http://localhost:4566 s3 mb s3://test-out
# Or via awslocal wrapper:  pip install awscli-local && awslocal s3 mb s3://test-in
# Or:  docker compose exec localstack awslocal s3 mb s3://test-in

# Upload a test doc
awslocal s3 cp tests/corpus/medium.docx s3://test-in/sample.docx

# Run conversion against LocalStack-backed S3
curl -sS -X POST http://localhost:8080/v1/convert \
  -F "s3_input=s3://test-in/sample.docx" \
  -F "s3_output=s3://test-out/sample.pdf" \
  -o /tmp/result.pdf \
  -D - 2>&1 | grep -E "HTTP|X-S3"

# Verify object landed
awslocal s3 ls s3://test-out
```

### Dev05 smoke test (post-deploy)

```bash
# 1. Pre-deploy: create real buckets + IAM role + attach policy (out of band).

# 2. Drop a test doc into the input bucket
aws s3 cp tests/corpus/medium.docx s3://opus2-dev-office-convert-in/test.docx \
  --profile opus2-dev

# 3. POST via --resolve (DNS cache trap workaround per memory)
ALB_IP=$(dig @1.1.1.1 +short office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com | tail -1)
curl -sS -X POST \
  --resolve "office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com:443:${ALB_IP}" \
  https://office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com/v1/convert \
  -F "s3_input=s3://opus2-dev-office-convert-in/test.docx" \
  -F "s3_output=s3://opus2-dev-office-convert-out/test.pdf" \
  -o /tmp/dev05_s3.pdf -D -

# 4. Tee correctness check — HTTP bytes == S3 object bytes
aws s3 cp s3://opus2-dev-office-convert-out/test.pdf /tmp/from_s3.pdf --profile opus2-dev
diff <(sha256sum /tmp/dev05_s3.pdf | awk '{print $1}') \
     <(sha256sum /tmp/from_s3.pdf  | awk '{print $1}')
# → empty diff = the streamed bytes and the S3 bytes are identical
```

---

## Environment routing — same image, two backends

```
                    ┌─────────────────────────────────────┐
                    │   office_convert/s3_client.py       │
                    │   boto3.client("s3")                │
                    └────────────┬────────────────────────┘
                                 │ reads env at runtime
              ┌──────────────────┴──────────────────────┐
              ▼                                         ▼
   AWS_ENDPOINT_URL_S3 set                AWS_ENDPOINT_URL_S3 unset
              │                                         │
              ▼                                         ▼
   ┌──────────────────────┐               ┌──────────────────────────┐
   │  http://localstack:  │               │  Real AWS endpoint:      │
   │  4566                │               │  s3.eu-west-1.           │
   │  (compose only)      │               │  amazonaws.com           │
   └──────────────────────┘               └──────────────────────────┘
       LOCAL                                  DEV05 + PROD
```

| Concern | Local (compose) | Dev05 / Prod (EKS) |
|---|---|---|
| S3 endpoint | `http://localstack:4566` | Real AWS |
| Auth | env vars `test/test` | IRSA — Service Account → OIDC → IAM role |
| Buckets | `test-in`, `test-out` | `opus2-dev-office-convert-{in,out}` |
| Persistence | LocalStack volume | S3 durability (11 nines) |

**Application code is environment-agnostic — boto3 routes itself based on
the env. The Helm chart never sets `AWS_ENDPOINT_URL_S3`; only `compose.yaml` does.**

---

## IRSA setup (Phase 5 detail)

### ServiceAccount template

```yaml
# deploy/helm/office-convert/templates/serviceaccount.yaml
{{- if .Values.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "office-convert.serviceAccountName" . }}
  namespace: {{ .Release.Namespace }}
  annotations:
    eks.amazonaws.com/role-arn: {{ required "serviceAccount.roleArn required" .Values.serviceAccount.roleArn }}
{{- end }}
```

### IAM policy template

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadInputs",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::opus2-dev-office-convert-in/*"
    },
    {
      "Sid": "WriteOutputs",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::opus2-dev-office-convert-out/*"
    },
    {
      "Sid": "ReadOutputsForPresign",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::opus2-dev-office-convert-out/*"
    }
  ]
}
```

> **Amendment (2026-05-27) — `s3:GetObject` on the output bucket.** The
> presigned-download API (Phase 7 below) signs a `GetObject` request against
> the output bucket. A presigned URL only works if the *signing principal*
> (the IRSA role) itself holds `s3:GetObject` on that object — presigning does
> not grant new permissions, it delegates existing ones. So the role needs
> `GetObject` on `…-out/*`, scoped to the output prefix only. Without this the
> minted URL returns `AccessDenied` when the browser follows it.

### OIDC trust policy template

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::537462380503:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/<CLUSTER-OIDC-ID>"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.eu-west-1.amazonaws.com/id/<CLUSTER-OIDC-ID>:sub": "system:serviceaccount:office-convert-dev:office-convert",
          "oidc.eks.eu-west-1.amazonaws.com/id/<CLUSTER-OIDC-ID>:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

`<CLUSTER-OIDC-ID>` is obtained from `aws eks describe-cluster --name
DEV05-EKS-CLUSTER --query 'cluster.identity.oidc.issuer'`.

---

## Failure modes — S3 side effects

| Failure | S3 side effect |
|---|---|
| `s3_input` bucket forbidden by allowlist | nothing (rejected before any S3 call) |
| `s3_input` key 404 | nothing (rejected after GetObject 404) |
| Conversion crash mid-pipeline | no upload attempted; nothing in output bucket |
| Pod killed during boto3 upload | boto3's internal abort fires; backstop = bucket lifecycle "abort incomplete multipart uploads after 1d" |
| `s3_output` bucket write-denied | nothing in bucket; client gets 500 JSON `failure_class=s3_output_upload_failed` |
| HTTP response drops mid-stream | S3 upload **hasn't started yet** (it runs after the stream); if cache file exists, retry is possible — but for simplicity v1 treats this as "request failed, nothing in S3" |

---

## Risks

- **moto + aiobotocore compatibility**: pin moto >= 5.0. Verify in Phase 2.
- **IRSA propagation lag**: first deploy after IAM role creation can take
  5-15 min for the SA token to start working. Bake into Phase 6 timeline.
- **Bucket-name typos in allowlist**: caller errors will surface as 400
  "bucket not allowed" — needs a clear error message so people don't think
  it's a service bug.
- **Cache file size on disk**: PDFs accumulate in `/cache/`. Existing
  cache-cleanup behavior unchanged; just be aware S3 uploads read from
  this dir.
- **`asyncio.to_thread` for boto3 upload**: boto3 is sync; wrap in
  `asyncio.to_thread` so the event loop isn't blocked. Already a known
  pattern in the codebase.

---

## Out of scope (v1)

- Cross-account input buckets (needs role-assumption chain)
- Pre-signed URL mode (Flow D in earlier exploration)
- Event-driven mode (Flow C — S3 PUT → SQS → consumer; future scope, would
  add `office_convert/s3_consumer.py` + KEDA)
- Server-side encryption (SSE-KMS) configuration — relies on bucket defaults
- Storing the input file (opt-in `store_input` flag deferred until use case appears)
- `X-S3-Output-ETag` header — caller does `HEAD` if needed

---

## Open questions

1. **Output key collision behavior**: if `s3_output` key already exists,
   should we overwrite, fail, or version? Default in v1: **overwrite**
   (S3's `PutObject` default). Document explicitly.
2. **Default output bucket vs caller-specified**: if caller omits
   `s3_output` but config has `s3_default_output_bucket`, do we
   auto-store? Default in v1: **no** — caller must opt in by sending
   `s3_output`. Future: config flag `s3_always_store_output: bool`.
3. **Retry on S3 upload failure**: boto3 handles transient retries
   internally. For non-transient failures (403, 404 bucket), surface
   immediately. Default in v1: trust boto3's defaults; no extra retry
   logic.

---

## References

- [[project-ingestion-processors-integration]] — Opus2 ingestion context
  that motivates S3 source
- [[reference-eks-cluster-topology]] — dev05 ALB + IRSA precedents
- [[feedback-deploy-workflow]] — chart-first deploy convention
- `office_convert/orchestrator.py:296` — existing tee-to-cache pattern
- `office_convert/qpdf.py` — existing `concat_streaming` async generator
- `office_convert/cache.py:60` — existing `tee-to-cache` impl this design
  reuses
