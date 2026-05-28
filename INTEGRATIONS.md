# Integrations — office-convert

How office-convert plugs into the broader Opus 2 system. Maintained as a peer-of-peers index so anyone touching the deploy/IAM/Helm surface knows which other services depend on those contracts.

> If you change anything in this file's "Provides to" sections, also update the consuming repo's `INTEGRATIONS.md`. Both sides MUST stay in sync.

---

## Consumed by → `classification-service-demo`

**Use case:** Auto-convert pipeline. When the classifier emits `category=convert` for a document (DOC/DOCX/XLSX/PPTX/RTF/TIFF…), classification-service drops a claim-check on its convert SQS queue. A worker there picks it up and calls **our `POST /v1/convert`** with `s3_input` + `s3_output` pointing at classification's S3 bucket. We read the input from their bucket, convert, and write the PDF back to their bucket. Classification then mints the presigned download URL using its own IRSA (no presign needed from us).

**Counterpart doc:** [classification-service-demo/deploy/INTEGRATIONS.md](https://github.com/adityawagh1710/document-uploader-classification-service-demo/blob/main/deploy/INTEGRATIONS.md)

### What classification sends us

| Field | Example | Note |
|---|---|---|
| HTTP target | `http://office-convert.office-convert-dev.svc.cluster.local/v1/convert` | In-cluster Service DNS (in dev05) |
| Method | `POST` (multipart/form-data) | Same shape as a UI-initiated convert |
| `s3_input` | `s3://classification-ui-dev05/ui/<documentId>/<filename>` | Original upload, written by classification-ui |
| `s3_output` | `s3://classification-ui-dev05/converted/<documentId>.pdf` | Where we write the converted PDF |
| `file` | (omitted — `s3_input` mutually exclusive) | |

**Response contract we promise:**
- `200 + application/pdf` body streamed back (worker drains the stream to confirm the S3 write completed before treating as success)
- `X-Request-ID` header — opaque correlation id classification stores on its DDB row for later progress lookups
- `X-S3-Output-Bucket` + `X-S3-Output-Key` headers — bucket + key the PDF actually landed at (worker uses these to set `convertS3Key` on the DDB row)
- 4xx with `{failure_class, detail}` JSON for caller errors (unsupported format, etc.) — terminal, classification marks the row `failed`
- 5xx / timeout — transient, worker doesn't mark terminal so SQS redrives

### What we provide

| Surface | File | Detail |
|---|---|---|
| IRSA role | `deploy/iam/office-convert-s3-policy.json` Sids `ReadClassificationInputs` + `WriteClassificationOutputs` | Grants `s3:GetObject` on `classification-ui-dev05/ui/*` + `s3:PutObject` + `s3:AbortMultipartUpload` on `classification-ui-dev05/converted/*`. Role: `office-convert-dev-s3`. |
| Helm values | `deploy/helm/office-convert/values-classification-fanout.yaml` | Overlay that adds `classification-ui-dev05` to both `s3.inputBucketsAllowlist` and `s3.outputBucketsAllowlist`. Apply via `HELM_EXTRA_ARGS=--values …` on `make deploy-dev`. |
| Live-progress endpoint | `office_convert/server.py` `GET /v1/jobs/<request_id>/progress` | Already shipped pre-integration. Returns `{current_chunk, total_chunks, pages_rendered, eta_seconds}`. Classification's UI polls this through its own `/api/runs/<docId>/progress` proxy while `convertStatus=converting`. |
| Heartbeats endpoint | `GET /v1/jobs/<request_id>/heartbeats` | Per-worker RSS/swap/CPU/phase. Not used yet by classification but available. |

### Operator runbook reference

`deploy/iam/README.md` §"Cross-service grant — classification-service fanout" has the exact `aws iam put-role-policy` + `make deploy-dev` commands. **Re-apply both whenever this Sid list or the overlay changes.**

### Tested against

| Date | classification HEAD | office-convert HEAD | Result |
|---|---|---|---|
| 2026-05-28 | `feat/auto-convert-integration` (branches 02-07 merged) | `feat/01-cross-service-s3-grant-classification` + dev05 deploy | IAM simulator green for all 4 access matrix cells; pod env has both allowlists |

---

## Consumes from → (none yet)

office-convert today only reads S3 + writes S3 (its own buckets + classification's via the grant above). No upstream SQS/HTTP dependencies.

If a future integration adds one — e.g., office-convert consuming from a job queue — add a "Consumes from" section here AND a "Provides to" section in the producer's `INTEGRATIONS.md`.

---

## Adding a new integration

1. Edit this file: add a new "Consumed by" or "Consumes from" section.
2. Edit the other repo's `INTEGRATIONS.md` to mirror.
3. The technical contract details (request/response shapes, IAM Sids, env vars, bucket prefixes) live HERE. The implementation lives in the linked files (IAM JSON, Helm chart, server code).
4. Whenever someone changes anything in those linked files, re-read this doc to confirm the cross-service contract isn't silently breaking.
