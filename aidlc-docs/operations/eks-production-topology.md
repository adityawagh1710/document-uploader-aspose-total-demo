# EKS Production Topology — office-convert

**Status**: Design reference. **Not implemented.** v1 (the AI-DLC-completed deliverable) is the local PoC under `compose.yaml`. This document captures the production deployment shape that the v1 constraint set was designed against, so future sessions resume with the full picture in view.

**Source of truth for constraints**: `aidlc-docs/aidlc-state.md` (Hard Constraints section + Q4/Q5/Q6/Q9 decisions). When this document conflicts with `aidlc-state.md`, `aidlc-state.md` wins — it's the load-bearing decision log.

**Last update**: 2026-05-12

---

## 1. Why this doc exists

The full cloud architecture was designed during Requirements Analysis (Q1–Q25) and is recorded in `aidlc-state.md` as a set of accepted answers and load-bearing constraints. But the AI-DLC workflow short-circuited to a **local v1 PoC** when the user said "keep it simple for now — we'll get it working locally first (no EKS)". That pivot deferred all cloud surfaces but preserved their requirements. This document reassembles those scattered decisions into one production deployment view, so the next time someone returns to the cloud target they don't have to re-derive the topology from 25 question answers.

---

## 2. Topology at a glance

```
┌─────────────────────── Client (any IAM-authenticated principal) ──────────────┐
│                                                                               │
│  1. Upload input → s3://aspose-inputs/<tenant>/<correlation_id>/in.docx       │
│  2. sqs:SendMessage → aspose-jobs-<tenant>                                    │
│  3. Poll DynamoDB row (tenant_id, correlation_id) for status transitions      │
│  4. GET presigned S3 URL from row → download output.pdf                       │
│  (Optional) Listen on caller-supplied SNS/SQS for completion event            │
└────────────────────────────────────┬──────────────────────────────────────────┘
                                     │ sqs:SendMessage (IAM-gated)
                                     ▼
                            ┌────────────────┐
                            │ per-tenant SQS │  + redrive → -dlq queue
                            │ submit queue   │  maxReceiveCount = 3
                            └────────┬───────┘
                                     │ aiobotocore long-poll
                                     ▼
┌──── EKS cluster — worker node group (m6id / r5d, Bottlerocket or AL2023) ─────┐
│                                                                               │
│  ┌──── office-convert Deployment (replicas: 4, static — Q9=A) ─────────────┐  │
│  │  Pod spec:                                                               │ │
│  │    resources.limits.memory  = 2Gi  ← load-bearing ceiling                │ │
│  │    resources.requests.memory = 2Gi                                       │ │
│  │    securityContext.runAsNonRoot = true (uid 1000)                        │ │
│  │    Kubelet (node): failSwapOn:false, memorySwap.swapBehavior:LimitedSwap │ │
│  │    Swap: 2–4 GB on local NVMe per pod (~1–2× memory limit)               │ │
│  │                                                                          │ │
│  │  ┌──── Python orchestrator container ─────────────────────────────────┐  │ │
│  │  │  uvicorn office_convert.server:app                                 │  │ │
│  │  │  • aiobotocore SQS long-poll (consumes submit message)             │  │ │
│  │  │  • probe.py → format + page count                                  │  │ │
│  │  │  • chunk_planner.py → chunks (≤90 MB worst case; 200–400 MB typ.)  │  │ │
│  │  │  • for each chunk: spawn worker subprocess with prlimit AS=2G      │  │ │
│  │  │    OFFICE_CONVERT_PARALLEL=2 (two concurrent chunks per pod)       │  │ │
│  │  │  • qpdf concat_streaming → upload PDF to S3 (multipart)            │  │ │
│  │  │  • DynamoDB conditional write → status row update                  │  │ │
│  │  │  • Publish completion event to caller-supplied ARN (if any)        │  │ │
│  │  └─────────────────────────────────────────────────────────────────────┘ │ │
│  │       │ fork + execve + prlimit RLIMIT_AS = 2 GiB                        │ │
│  │       ▼                                                                  │ │
│  │  ┌──── office-convert-worker (C++ binary, /usr/local/bin/) ──────────┐   │ │
│  │  │  • Lazy product activation: Aspose::{Words|Cells|Slides|Pdf}      │   │ │
│  │  │    ::License().SetLicense() — only the namespace this chunk needs │   │ │
│  │  │  • Link tree:                                                     │   │ │
│  │  │      /opt/aspose/Words/lib/libAspose.Words.Cpp.so                 │   │ │
│  │  │      /opt/aspose/Cells/lib/libAspose.Cells.Cpp.so                 │   │ │
│  │  │      /opt/aspose/Slides/lib/libAspose.Slides.Cpp.so               │   │ │
│  │  │      /opt/aspose/PDF/lib/libAspose.Pdf.Cpp.so                     │   │ │
│  │  │    Each subdir has its own CodePorting framework sibling          │   │ │
│  │  │  • Render assigned page range → PDF on stdout (or scratch path)   │   │ │
│  │  │  • Exit codes per business-rules.md §2: 0/1/2/3/137               │   │ │
│  │  └───────────────────────────────────────────────────────────────────┘   │ │
│  │                                                                          │ │
│  │  Mounts:                                                                 │ │
│  │   /aspose/license.lic  ← Secrets Manager CSI driver (read-only)          │ │
│  │   /tmp                 ← emptyDir.medium=Memory (capped via sizeLimit)   │ │
│  │   IRSA ServiceAccount  ← IAM role for SQS/S3/DynamoDB                    │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌─ VPC endpoints (no NAT cost, traffic stays in-VPC) ───────────────────┐    │
│  │  • S3 Gateway endpoint                                                │    │
│  │  • DynamoDB Gateway endpoint                                          │    │
│  │  • SQS Interface endpoint                                             │    │
│  │  • Secrets Manager Interface endpoint (for license sync)              │    │
│  │  • CloudWatch Logs Interface endpoint                                 │    │
│  └───────────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Pod model

### 3.1 Container composition

One pod = one container, multi-process inside (orchestrator forks worker subprocesses). Two-container "sidecar" not used — the worker is a binary, not a service.

| Layer | Path in image | Origin |
| --- | --- | --- |
| Python interpreter + uvicorn | `/usr/local/bin/python3.12` | `python:3.12-slim-bookworm` base |
| Orchestrator code | `/app/office_convert/` | COPY from repo |
| C++ worker binary | `/usr/local/bin/office-convert-worker` | multi-stage builder |
| Aspose Words .so + headers | `/opt/aspose/Words/{lib,include}/` | builder stage, from `vendor/aspose/Words/` |
| Aspose Cells .so + headers | `/opt/aspose/Cells/{lib,include}/` | builder stage |
| Aspose Slides .so + headers | `/opt/aspose/Slides/{lib,include}/` | builder stage |
| Aspose PDF .so + headers | `/opt/aspose/PDF/{lib,include}/` | builder stage |
| qpdf binary | `/usr/bin/qpdf` | apt |
| util-linux (`prlimit`) | `/usr/bin/prlimit` | apt |

Each Aspose product lives in its own subdirectory because each ships its own copy of `libcodeporting.translator.cs2cpp.framework_x86_64_libstdcpp_libc2.23.so`. Per-product RUNPATH `$ORIGIN:$ORIGIN/../lib` keeps siblings from clashing.

### 3.2 Resource model

| Resource | Value | Rationale |
| --- | --- | --- |
| `requests.memory` | 2Gi | Match limit; pin scheduling |
| `limits.memory` | 2Gi | Hard ceiling per pod (load-bearing constraint, `aidlc-state.md`) |
| `requests.cpu` | 1 | Two concurrent chunks per pod (`OFFICE_CONVERT_PARALLEL=2`) |
| `limits.cpu` | 4 | Burst for Aspose internal threading + qpdf concat |
| Ephemeral storage | 4Gi | Chunk scratch + tmpfs cap |
| Swap (node-level) | 2–4 GiB per pod, LimitedSwap | OOM cushion for borderline-amplification chunks |

### 3.3 Lifecycle

- `terminationGracePeriodSeconds: 900` — orchestrator drains in-flight chunks before SIGKILL
- `lifecycle.preStop`: stop polling SQS, complete current chunks, mark job rows as `RETRYABLE` if not finished
- `readinessProbe`: `GET /health` returns 200 once orchestrator + license are loaded
- `livenessProbe`: `GET /health` with `initialDelaySeconds: 30` (license parse + Aspose lazy activation is slow on first invocation)

---

## 4. Node group requirements

### 4.1 Why standard EKS node groups don't work

Standard EKS-optimized AMIs don't support swap. The constraint set (`aidlc-state.md` → "Swap enabled on Aspose worker pods") forces a node OS with cgroupv2 swap support.

| Requirement | Why |
| --- | --- |
| **cgroupv2** | `failSwapOn:false` + `LimitedSwap` need cgroupv2 |
| **Bottlerocket or AL2023** | AL2 lacks cgroupv2; not viable |
| **Instance with local NVMe (m6id, r5d, m5d)** | Swap must NOT be on EBS — page-in over network is a performance cliff |
| **Custom node bootstrap** | Mount NVMe, create swap file/partition, kubelet config patch |
| **Kubelet config**: `failSwapOn: false`, `memorySwap.swapBehavior: LimitedSwap` | Required for per-pod swap budgeting |

### 4.2 Recommended instance type

`m6id.large` per pod (2 vCPU / 8 GiB / 118 GB NVMe). Four pods → one `m6id.2xlarge` node fits four pods comfortably. Or scale node count for N=4 fleet via two `m6id.xlarge` (one pod per node, better failure isolation).

---

## 5. Container image build pipeline

```
Developer push → GitHub                                       (or CodeCommit)
       │
       ▼
GitHub Actions (or CodeBuild) — ci.yaml
  1. checkout
  2. download Aspose libs via curl into vendor/aspose/{W,C,S,P}/ from
     - https://releases.aspose.com/words/cpp/ → vendor/aspose/Words/
     - https://releases.aspose.com/cells/cpp/ → vendor/aspose/Cells/
     - https://releases.aspose.com/slides/cpp/ → vendor/aspose/Slides/
     - https://releases.aspose.com/pdf/cpp/   → vendor/aspose/PDF/
     SHA256-verify each against pinned hashes in Makefile.
  3. docker build (multi-stage):
       Stage 1 (builder): debian:bookworm + gcc-12 + cmake → office-convert-worker
       Stage 2 (runtime): python:3.12-slim-bookworm + qpdf + util-linux + libfontconfig1
                          + COPY --from=builder /opt/aspose + worker binary + office_convert/
  4. trivy scan + ECR vulnerability scan
  5. docker push to ECR (one tag per commit SHA, one moving "latest")
       │
       ▼
ArgoCD (or Flux) watches the image tag in the manifests repo
  → rolls out new ReplicaSet (maxSurge:1, maxUnavailable:0)
```

**Image size**: ~1.5–2 GB (Aspose libs dominate; python:slim base is ~150 MB; worker binary is ~5 MB stripped).

**Aspose vendor sources**: per session 2026-05-12, the decision is to use **4 separate libraries** rather than the Aspose.Total bundle, because the Linux Total ZIP omits Words. See `aidlc-state.md` "Aspose SKU pivot" entry (under revision) for the current state.

---

## 6. License handling

License is **never baked into the image**. Lifecycle:

| Step | Where | What |
| --- | --- | --- |
| 1. Storage | AWS Secrets Manager | Secret `office-convert/aspose-license` → contents of `Aspose.TotalforC++.lic` |
| 2. Mount | Secrets Manager CSI driver | `volumeAttributes.objects` declares secret → mounted as file at `/aspose/license.lic` |
| 3. Activation | C++ worker subprocess | `Aspose::Words::License().SetLicense("/aspose/license.lic")` (lazy, only the namespace needed for this chunk) |
| 4. Rotation | Update Secret + rolling restart | CSI driver auto-syncs on a poll interval (default 60s); rolling restart picks up new file path mount |

**Current license is temporary** — expires 2026-06-08. Production needs a non-temp Aspose license. The `<Product>` field is `Aspose.Total for C++` (umbrella) — covers Words, Cells, Slides, PDF as separate libs.

**Lazy product activation** (Tier-1 perf optimization from `aidlc-state.md`): the C++ worker inspects its argv to determine which namespace it needs, then calls `SetLicense` on that one only. Skips ~150–600 ms of per-spawn license-parse work across the other three namespaces.

---

## 7. IAM model

### 7.1 IRSA (IAM Roles for Service Accounts)

Pod identity is bound via IRSA — no static AWS credentials in the container.

```yaml
# manifests/serviceaccount.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: office-convert
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCT:role/office-convert-pod
```

### 7.2 Pod IAM role policy

| Service | Action | Resource scope |
| --- | --- | --- |
| SQS | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility` | `aspose-jobs-*` (all tenant queues) |
| SQS | `sqs:SendMessage` | `aspose-jobs-*-dlq` (DLQ writes from app) |
| S3 | `s3:GetObject` | `arn:aws:s3:::aspose-inputs/*` |
| S3 | `s3:PutObject` | `arn:aws:s3:::aspose-outputs/*` |
| S3 | `s3:PutObject` | `arn:aws:s3:::aspose-failed-jobs/*` (forensic bucket) |
| DynamoDB | `dynamodb:UpdateItem`, `dynamodb:Query` | `aspose-jobs` table |
| Secrets Manager | `secretsmanager:GetSecretValue` | `office-convert/aspose-license` |
| SNS | `sns:Publish` | caller-supplied completion topic ARN (resolved per-request) |
| CloudWatch | `cloudwatch:PutMetricData` | namespace `Office-Convert` |

### 7.3 Caller (client) IAM

Tenant principal needs:

| Service | Action | Resource scope |
| --- | --- | --- |
| SQS | `sqs:SendMessage` | `aspose-jobs-<their-tenant-id>` |
| S3 | `s3:PutObject`, `s3:GetObject` | `arn:aws:s3:::aspose-inputs/<tenant-id>/*`, `aspose-outputs/<tenant-id>/*` |
| DynamoDB | `dynamodb:Query` | `aspose-jobs` table (key condition: `tenant_id = <theirs>`) |

In v1, `tenant_id` is **caller-asserted** in the SQS message body (Q6=X, no app-layer auth). The IAM scope above prevents cross-tenant SQS submission, but cross-tenant pollution by a misconfigured AWS principal in your own account is possible. Risk accepted; documented in `aidlc-state.md`. v2 adds `Attributes.SenderId` validation server-side.

---

## 8. Networking

### 8.1 VPC endpoints

All AWS service traffic stays in-VPC via endpoints — no NAT Gateway needed.

| Service | Endpoint type | Why |
| --- | --- | --- |
| S3 | Gateway | Free; in-region inputs/outputs at 10 GB scale |
| DynamoDB | Gateway | Free; status reads/writes |
| SQS | Interface | Long-poll consumer + DLQ writes |
| Secrets Manager | Interface | License sync |
| CloudWatch Logs | Interface | Container logs egress |
| STS | Interface | IRSA token exchange |

### 8.2 Pod ingress

In v1 (Q5=D, queue-driven) **the orchestrator has no public HTTP endpoint**. There's no Ingress, no NLB, no ALB targeting the pods. Health checks are kubelet-internal only. Pods only consume SQS and write to S3 + DynamoDB.

A `ClusterIP` Service on port 8080 exists for the kubelet readiness/liveness probes, scoped to the cluster — never exposed.

### 8.3 NetworkPolicy

```yaml
egress:
  - to:
      - namespaceSelector: {} # kube-system for CoreDNS
        podSelector: {matchLabels: {k8s-app: kube-dns}}
    ports:
      - port: 53
        protocol: UDP
  - to:
      - ipBlock:
          cidr: 0.0.0.0/0
          except: [10.0.0.0/8] # pin VPC CIDR
    ports: [443] # AWS API + VPC endpoint traffic
ingress: []     # no inbound; queue-driven
```

---

## 9. Scaling model

### 9.1 v1 — static fleet (Q9=A, accepted)

`replicas: 4` in the Deployment manifest. Manual operator scaling. No HPA. No KEDA. Explicitly NOT on the roadmap unless duty cycle data shows mis-sizing.

### 9.2 Sizing math (per `aidlc-state.md` Q1=A)

- Target: ≤10 concurrent jobs, <100 jobs/hour
- Per pod: 2 parallel chunks (`OFFICE_CONVERT_PARALLEL=2`)
- N=4 pods × 2 chunks = 8 concurrent chunks → 2–4× headroom over peak
- Peak queue dwell time at 100 jobs/hour ≈ 2 min, well inside the p95 ≤ 15 min SLO

### 9.3 PodDisruptionBudget

```yaml
spec:
  minAvailable: 3       # tolerate 1 pod loss during node drain
  selector:
    matchLabels: {app: office-convert}
```

---

## 10. Failure handling

### 10.1 Per-chunk OOM (the load-bearing failure mode)

Cascade order (from `aidlc-state.md`):

1. **Typical-amplification chunks fit RAM** → succeed
2. **~1% borderline chunks spill into swap** → succeed slowly (~5–30 s slower per chunk)
3. **True OOM-with-swap** → worker exits 137 (SIGKILL) → orchestrator catches → subdivision retry (chunk halved, then quartered, then page-floor)
4. **Single-page-with-swap OOM** → dead-letter to `s3://aspose-failed-jobs/<tenant>/<correlation_id>/page-N.docx` + DynamoDB status `failed:chunk_floor`

### 10.2 Orchestrator-crash recoverability

SQS native redrive — `maxReceiveCount: 3`, visibility timeout matched to p99 chunk render time. Crashed jobs reappear on the queue, picked up by another pod. DynamoDB job state allows resumption (orchestrator skips chunks already in `S3 outputs/<tenant>/<correlation_id>/chunk-N.pdf`).

### 10.3 Dead-letter queue + forensic bucket

- **SQS DLQ**: `aspose-jobs-<tenant>-dlq` — orchestrator-crash retries beyond maxReceiveCount
- **S3 forensic bucket**: `aspose-failed-jobs` — chunk-floor failures, license-expired, ingest rejections (>10 GB / >50K pages)
- **Operator alert**: CloudWatch alarm on `DLQ ApproximateNumberOfMessagesVisible > 0`

---

## 11. Observability

### 11.1 Metrics (CloudWatch, namespace `Office-Convert`)

All metrics carry a `tenant_id` dimension.

| Metric | Type | Purpose |
| --- | --- | --- |
| `jobs_submitted` | Counter | Demand signal |
| `jobs_completed` | Counter | Success rate (with status dimension: ok/failed) |
| `chunk_render_seconds` | Histogram | Per-chunk perf |
| `chunk_oom_count` | Counter | OOM cascade trigger |
| `chunk_subdivision_count` | Counter | OOM cushion stress |
| `worker_swap_used_bytes` | Gauge | Borderline-amplification frequency |
| `worker_swap_in_pages_per_second` | Gauge | Chronic swap = chunk planner needs revisiting |
| `pod_concurrent_chunks` | Gauge | Capacity headroom |
| `sqs_queue_dwell_seconds` | Gauge (from queue ApproximateAgeOfOldestMessage) | Backlog age |

### 11.2 Alerting conditions

| Condition | Severity |
| --- | --- |
| `chronic_swap` — `worker_swap_in_pages_per_second > 100` for 5 min | warn — chunk planner mis-sized |
| `dlq_non_empty` — `DLQ messages > 0` | warn — orchestrator crash backlog |
| `jobs_failed_rate > 1%` over 5 min | page |
| `chunk_floor_failures > 0` | page (forensic investigation needed) |
| `license_expiry < 14 days` | page (license rotation overdue) |

### 11.3 Tracing

X-Ray traces from orchestrator. Spans:

- `convert_job` (root, per submit message)
  - `probe` (format + page count)
  - `plan_chunks` (chunk planner)
  - per-chunk: `spawn_worker` → `aspose_render` → `s3_upload`
  - `qpdf_concat` (streaming merge)
  - `dynamodb_status_update`

---

## 12. Cross-reference index — what's already designed

| Topic | Source |
| --- | --- |
| 2 GB RAM ceiling | `aidlc-state.md` → Hard Constraints |
| Swap on local NVMe | `aidlc-state.md` → Q10 refinement (swap enabled) |
| Tier-1 C++ perf optimizations | `aidlc-state.md` → C++ worker perf optimizations |
| Multi-tenant data layout | `aidlc-state.md` → Q4 (answered B) |
| Queue-driven API (no HTTP ingress) | `aidlc-state.md` → Q5 (answered D, revised) |
| No app-layer auth in v1 | `aidlc-state.md` → Q6 (answered X) |
| Static fleet sizing | `aidlc-state.md` → Q9 (answered A, final) |
| 10 GB / 50K page input ceiling | `aidlc-state.md` → Q3 (answered C, revised) |
| Tiered SLO (p95 ≤ 15 min for ≤100 MB) | `aidlc-state.md` → Q2 (revised) |
| Aspose SKU decision | `aidlc-state.md` → "Aspose SKU pivot" (under revision per session 2026-05-12) |
| Business logic / chunk-render contract | `aidlc-docs/construction/office-converter/functional-design/business-rules.md` |
| NFR patterns (lazy activation, etc.) | `aidlc-docs/construction/office-converter/nfr-design/` |
| Build + test instructions | `aidlc-docs/construction/build-and-test/` |

---

## 13. Implementation gap (what's NOT yet built)

This is the work between "v1 deliverables complete" and "running in EKS production":

### 13.1 Infrastructure-as-code

- **Terraform/CDK module** for: VPC + 3 AZ subnets + VPC endpoints, SQS queues + DLQs (per-tenant), DynamoDB `aspose-jobs` table, S3 buckets (inputs, outputs, failed-jobs), ECR repo, Secrets Manager secret, IAM roles (pod + caller template)
- **Custom node bootstrap** for swap-on-NVMe + cgroupv2 kubelet patch (Bottlerocket settings TOML or AL2023 userdata)

### 13.2 Kubernetes manifests

- Deployment, ServiceAccount (IRSA-annotated), Service (ClusterIP probe target), NetworkPolicy, PodDisruptionBudget
- SecretProviderClass for the Secrets Manager CSI driver mount
- Helm chart wrapping the above with values for `tenant_list`, `replica_count`, `image_tag`

### 13.3 CI/CD

- GitHub Actions (or CodeBuild) for: download Aspose libs (SHA256-pinned), docker build, trivy + ECR scan, push, manifest bump in GitOps repo
- ArgoCD (or Flux) Application pointing at the manifests path
- Image-tag promotion: dev → staging → prod via separate Applications

### 13.4 Operator runbook

- License rotation procedure
- DLQ drain procedure
- Chunk-floor forensic investigation steps (download from `aspose-failed-jobs`, reproduce locally, file Aspose ticket)
- Node group rotation (custom AMI bake)
- License expiry response (currently 2026-06-08 — temp license)

### 13.5 Application changes needed beyond v1

- `office_convert/server.py` — replace FastAPI HTTP routes with `aiobotocore` SQS consumer loop
- `office_convert/orchestrator.py` — emit DynamoDB `UpdateItem` status writes per state transition; emit completion SNS publishes on caller-supplied ARN
- `office_convert/cache.py` — back the per-tenant cache with S3 instead of local filesystem
- New module `office_convert/sqs_consumer.py` — message dedup via `correlation_id` + DynamoDB conditional write
- Replace `health` endpoint with kubelet-only TCP probe

---

## 14. Open questions / decisions deferred to v2

| Topic | v1 stance | v2 decision needed |
| --- | --- | --- |
| Caller authentication | None (caller-asserted `tenant_id`) | IAM `Attributes.SenderId` validation in SQS consumer |
| Per-tenant quotas | None (global ceiling only) | Per-tenant concurrent-jobs + jobs/hour at submit endpoint |
| Autoscaling | None (static N=4) | KEDA on SQS queue depth (only if duty cycle data demands) |
| Cross-tenant cache opt-in | Per-tenant only | Forward-compatible key layout exists; opt-in flag deferred |
| Presigned URL TTL | 1 hour | Restore to 24 h once auth lands |
| Compliance regime | None declared | SOC2/HIPAA/etc gate on Q20 + auth |

---

## 15. Where this fits in the AI-DLC framework

This document lives in **`aidlc-docs/operations/`** — the OPERATIONS phase that `CLAUDE.md` describes as `PLACEHOLDER` for the current workflow generation. v1 ended at the CONSTRUCTION phase (`Build and Test`). When a future session executes the OPERATIONS phase formally, this document becomes the starting point.

To progress this from "design reference" to "operational deployment", the OPERATIONS phase would need to run through (rough order):

1. Detailed deployment design (this doc, fleshed out per environment: dev/staging/prod)
2. Infrastructure-as-code generation (Terraform module per §13.1)
3. CI/CD pipeline generation (per §13.3)
4. Operator runbook generation (per §13.4)
5. Production readiness checklist + go/no-go review
6. Incident response procedures
7. SLA/SLO formalization

Each of those is a separate authoring session; none is in the v1 deliverable.
