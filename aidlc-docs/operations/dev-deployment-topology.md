# Dev Deployment Topology — office-convert-dev

**Status**: Active. Iterating. **Diverges intentionally from `eks-production-topology.md`** — that doc captures the queue-driven (Q5=D) production design; this doc captures the HTTP-fronted dev shape we're actually running and evolving.

**Cluster**: `DEV05-EKS-CLUSTER` (region `eu-west-1`, account `537462380503`, profile `opus2-dev`).

**Namespace**: `office-convert-dev`. Helm release: `office-convert`.

**Last update**: 2026-05-20T18:40 IST. **Current state: REDEPLOYED clean** on image `616c58d` (main HEAD) via canonical chart-first `make undeploy-dev && make deploy-dev`. Helm rev 1 deployed (SSA-failed rev 2 is gone — fresh history). 4 live patches re-applied via kubectl: 15-CIDR allowlist, idle_timeout=900, MAX_JOBS=2/PARALLEL=4. New ALB hostname `…-1254648625` — see §12.

---

## 1. Why this doc exists

The AI-DLC production design (`eks-production-topology.md`) is queue-driven: clients submit via per-tenant SQS, no HTTP endpoint on the pods. That's the v1 cloud target — not yet built.

What's actually live in DEV05 is a different shape: an HTTP-fronted FastAPI orchestrator + Streamlit UI, deployed via Helm, exposed via AWS load balancers. This is how the operator dogfoods the same `office_convert` codebase that powers compose locally. It's neither v1-as-designed nor v2 — it's the "dev cluster running the same image as compose, but on EKS so we can demo and test scale" reality.

This file is the source of truth for that reality.

---

## 2. Topology overview (workload shape, both 2026-05-18 NLB-only and 2026-05-19 ALB-alongside states)

```
┌──── Laptop (corp VPN: FortiClient) ───────────────────────────────────────────┐
│  kubectl works (control plane reachable via /32 routes for EKS API endpoint). │
│  NLB private IPs (10.35.x.x) NOT reachable: corp has no server-side VPC       │
│  peering. Probed 2026-05-18 — packets blackhole at corp HQ.                   │
│                                                                               │
│  Workaround: ./deploy/scripts/portforward.sh start                            │
│  → API on localhost:18080, UI on localhost:8501                               │
└──────────────────────────────┬────────────────────────────────────────────────┘
                               │ kubectl port-forward
                               ▼
┌──── DEV05-EKS-CLUSTER (eu-west-1) — namespace: office-convert-dev ────────────┐
│                                                                               │
│  Service: office-convert (LoadBalancer, scheme=internal NLB)                  │
│    → Pod: office-convert-* (FastAPI orchestrator, :8080)                      │
│      • uvicorn office_convert.server:app                                      │
│      • /health, /convert, /jobs/{id}, /jobs/{id}/heartbeats, /timings, ...    │
│      • max_jobs: 1 (one in-flight conversion per pod)                         │
│      • parallel: 2 (two C++ chunk workers per conversion)                     │
│      • fork-after-load enabled (DOCX/PPTX/PDF); XLSX uses legacy pool         │
│      • resources: 1-4 CPU, 2-4 GiB memory. NO swap on K8s (vs. compose's 6G). │
│                                                                               │
│  Service: office-convert-ui (LoadBalancer, scheme=internal NLB)               │
│    → Pod: office-convert-ui-* (Streamlit dashboard, :8501)                    │
│      • streamlit run test_ui.py                                               │
│      • Talks to API via in-cluster DNS:                                       │
│        http://office-convert.office-convert-dev.svc.cluster.local             │
│      • UI runs as root (Dockerfile.ui has no USER); TODO in values.yaml.      │
│      • resources: 0.1-0.5 CPU, 1.5Gi-4Gi memory (bumped from 512Mi-1.5Gi      │
│        after a 398 MiB XLSX upload OOMKilled the pod on 2026-05-19,           │
│        commit fd5b595; earlier 512Mi→1.5Gi bump was 2026-05-18).              │
│                                                                               │
│  Aspose license: K8s Secret `aspose-license` (created manually, not via Helm).│
│    Mounted at /aspose/license.lic on the API pod.                             │
└───────────────────────────────────────────────────────────────────────────────┘
```

**NLB hostnames** (snapshot — change on every undeploy+redeploy cycle):
- API: `k8s-officeco-officeco-e4f286faed-c01c3866123a436a.elb.eu-west-1.amazonaws.com:80`
- UI:  `k8s-officeco-officeco-52c974e126-31f56555dcadf6d5.elb.eu-west-1.amazonaws.com:8501`

Both `scheme: internal` → VPC-only. Reachable from inside the VPC; unreachable from the laptop (see §3).

---

## 3. Reachability constraints (current)

| Source | Can reach EKS API | Can reach NLBs |
|---|---|---|
| Laptop on FortiClient corp VPN | ✅ (via `/32` routes + IP allowlist on EKS endpoint) | ❌ (no VPC peering server-side, confirmed by route probe 2026-05-18) |
| Laptop via kubectl port-forward | ✅ | ✅ (tunneled through control plane) |
| Pod-to-pod inside cluster | ✅ | ✅ (in-cluster Service DNS) |
| Laptop direct → ALB (split-tunnel, ISP IP allowlisted) | n/a | ✅ — verified 2026-05-19 (`200 OK`, ~0.6s) once `103.53.234.52/32` was added to ALB `inbound-cidrs` |
| Random public IP | ❌ (allowlist) | ❌ (internal NLB) |

**The FortiClient VPC peering probe (2026-05-18, ruled out permanently)**: User added EE VPN via FortiClient (interface `fctvpndc0b79cc`). Manually adding `ip route add 10.35.0.0/16 via 192.168.8.24 dev fctvpndc0b79cc` correctly routed packets through the tunnel, but they timed out at corp HQ — corp's server-side routing does NOT include the EKS VPC `10.35.0.0/16`. Path permanently dead; do not retry.

**Workaround in active use**: `kubectl port-forward` via `deploy/scripts/portforward.sh`. Reliable but every operator needs kubectl + VPN.

---

## 4. Target topology (as-built 2026-05-19 — was forward-plan when this section was authored 2026-05-18)

**Decision converged 2026-05-18, fully implemented 2026-05-19**: ALB Ingress mirroring argocd's pattern, **two Ingresses sharing one ALB** via `alb.ingress.kubernetes.io/group.name: office-convert`, **subdomain routing**. Cutover is fully shipped (commits `37f01c0` step A + `33ba4c6` step B + `3cbc332` doc alignment). Both Services are `ClusterIP`; NLBs are gone; ALB is the sole ingress surface.

```
Browser ──HTTPS──▶ <hostname>.dev05.k8s.opus2dev.com
                          │
                  Route 53 A-alias (created manually post-Helm-deploy)
                          │
                          ▼
         ┌─────────────────────────────────────────────────────┐
         │  ONE ALB (internet-facing, group.name=office-convert)│
         │  • cert: arn:aws:acm:eu-west-1:537462380503:         │
         │          certificate/fab42f33-7d67-4ecf-b200-        │
         │          38af584485b0 (wildcard *.dev05…)            │
         │  • ssl-policy: ELBSecurityPolicy-FS-1-2-Res-2019-08  │
         │  • inbound-cidrs: corp allowlist (~10 CIDRs)         │
         │  • HTTP→HTTPS redirect via actions.ssl-redirect      │
         │  • idle_timeout: 300s (Streamlit websocket)          │
         └────────┬─────────────────────────────────────────────┘
                  │ ALB inspects Host header
       ┌──────────┴──────────────────────────────┐
   Host: office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com    Host: office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com
                │                                                              │
                ▼                                                              ▼
       Service: office-convert-ui (ClusterIP, :8501)         Service: office-convert (ClusterIP, :80 → :8080)
                │                                                              │
                ▼                                                              ▼
       Streamlit pod (test_ui.py)                            FastAPI orchestrator pod
```

**Final hostnames**:
- **UI**: `office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com`
- **API**: `office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com`

Both single-label under `dev05.k8s.opus2dev.com` so the existing wildcard cert covers them at zero cost. Naming follows existing `platform-dev-sandbox` style in the cluster.

---

## 5. AWS-side prerequisites (verified present 2026-05-18)

| Component | Status | Identifier |
|---|---|---|
| ACM cert `*.dev05.k8s.opus2dev.com` | ISSUED | `arn:aws:acm:eu-west-1:537462380503:certificate/fab42f33-7d67-4ecf-b200-38af584485b0` |
| ACM cert `*.k8s.opus2dev.com` (broader, alt) | ISSUED | `arn:aws:acm:eu-west-1:537462380503:certificate/fb260958-f60c-48c4-8a42-e3d08c7b6a3c` |
| Route 53 zone `dev05.k8s.opus2dev.com.` | exists | `Z045669519R5D9D8CKC79` |
| AWS Load Balancer Controller | running | `kube-system/aws-load-balancer-controller` (2 replicas) |
| Argocd Ingress (template precedent) | running | `argocd/argocd-http-ingress` (2y+ in production) |
| external-dns | **NOT installed** | — Route 53 records managed by `deploy/scripts/route53-{upsert,delete}.sh` (called from Makefile, see §6) |

**Argocd cert gotcha**: `argocd-http-ingress` currently references the **expired** cert `213a9222-0466-4e0f-9ca2-87e92c92944c`. Don't copy that ARN. Use the wildcard `fab42f33` for new apps.

---

## 6. As-built change set (commits `37f01c0` + `33ba4c6` + `3cbc332`, 2026-05-19)

The plan in this section has been executed end-to-end. Commit `37f01c0` (step A) landed the ALB Ingress alongside dormant NLBs; commit `33ba4c6` (step B) flipped both Services `LoadBalancer → ClusterIP`, deprovisioning the NLBs within ~60s of `helm upgrade`; commit `3cbc332` aligned Makefile printouts + `deploy/README.md` + this doc with the as-built pipeline.

**Helm chart, final state** (`deploy/helm/office-convert/`):

| File | Final state (post-`33ba4c6`) |
|---|---|
| `templates/api-service.yaml` | `type: ClusterIP`, NLB annotations removed. |
| `templates/ui-service.yaml` | `type: ClusterIP`, NLB annotations removed. |
| `templates/ingress.yaml` | Two `Ingress` resources sharing `group.name: office-convert`, each `host:`-routed. SSL-redirect action wired on port 80. Per-Ingress `healthcheck-path` (UI: `/_stcore/health`, API: `/health`). |
| `values.yaml` | `ingress:` block: `enabled: true`, `uiHost`, `apiHost`, `certificateArn`, `inboundCidrs` (10-CIDR argocd-lineage corp allowlist only — office/personal CIDRs deliberately NOT in chart per `9345f30` revert), `groupName: office-convert`, `sslPolicy`, `idleTimeoutSeconds: 300`, healthcheck paths. |

**Route 53 automation** — added 2026-05-19 alongside the chart:

| Script | When | What |
|---|---|---|
| `deploy/scripts/route53-upsert.sh` | Step `[7/8]` of `make deploy-dev` (after `helm upgrade --install`) | Polls `kubectl get ingress` for up to 180s waiting for the ALB hostname to populate, then `aws route53 change-resource-record-sets UPSERT` both UI + API A-aliases. Idempotent — safe to re-run. |
| `deploy/scripts/route53-delete.sh` | Step `[1/4]` of `make undeploy-dev` (**before** `helm uninstall`) | Looks up each record's current `AliasTarget.DNSName` (Route 53 DELETE requires it to match exactly), then submits the DELETE. Idempotent — skips silently if a record is absent. Must run before `helm uninstall` because once the Ingress is gone, the script can't recover the AliasTarget DNS name. |

Both scripts default to the project's hosted zone `Z045669519R5D9D8CKC79` and the two production hostnames; all values overridable via env vars (`HOSTED_ZONE_ID`, `UI_HOST`, `API_HOST`, `ALB_ZONE_ID`).

**Operator-facing Makefile targets** (top-level `Makefile`):

| Target | Steps (in order) |
|---|---|
| `make deploy-dev` | (1) ECR repo create-if-missing → (2) ECR login → (3-4) build + push API + UI images → (5) namespace + license `Secret` → (6) `helm upgrade --install` → **(7) `route53-upsert.sh`** → (8) print NLB + ALB hostnames, image digests, AWS console deep-links. |
| `make undeploy-dev` | **(1) `route53-delete.sh`** → (2) `helm uninstall` → (3) delete license Secret → (4) delete namespace. ECR images intentionally retained — cleanup commands printed but not run. |

Per [[feedback-deploy-workflow]] operator convention: always `make undeploy-dev && make deploy-dev`, never run `make deploy-dev` against a live release. The Makefile uses `helm upgrade --install` internally for idempotence, but the operator's discipline is to tear down first.

---

## 7. Pre-deploy checks (items 4–5 are now in-chart; keep 1–3 as operator checks)

The Streamlit idle-timeout and healthcheck-paths from the original pre-deploy list have moved into `values.yaml`, so they're no longer manual gates. What remains for the operator:

1. **Confirm the chart's `inboundCidrs` is still correct** — `values.yaml` line `ingress.inboundCidrs:` carries the committed list. Refresh it if corp egress has changed, OR add new operator IPs via the live `kubectl annotate` pattern (see §11) for one-off testing without churning the chart.
2. **Confirm wildcard cert is still ISSUED**:
   ```bash
   aws acm describe-certificate \
     --certificate-arn arn:aws:acm:eu-west-1:537462380503:certificate/fab42f33-7d67-4ecf-b200-38af584485b0 \
     --region eu-west-1 --profile opus2-dev \
     --query 'Certificate.Status'
   ```
   Expect: `"ISSUED"`.
3. **Confirm Route 53 zone write access** — `route53-upsert.sh` will fail loudly with the AWS error if your AWS identity lacks `route53:ChangeResourceRecordSets` on zone `Z045669519R5D9D8CKC79`. No dry-run needed; just be ready to read the error.

---

## 8. Post-deploy verifications

`make deploy-dev` runs steps 1–2 below automatically; verify the rest manually.

1. ✅ *(auto)* `kubectl -n office-convert-dev get ingress` → both Ingresses have an ADDRESS (same ALB hostname for both — group-shared). Surfaced in the deploy log under `[8/8] Deploy complete`.
2. ✅ *(auto)* Route 53 A-aliases created by `route53-upsert.sh`. Verify they propagated with `dig +short office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com` (expect 3 ALB IPs).
3. From a corp-allowlisted IP: `curl -v https://office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com/_stcore/health` → expect HTTP 200, valid cert chain.
4. From a NON-allowlisted IP (mobile hotspot) — same curl should hang/refuse. Confirms the allowlist works.
5. Open the UI hostname in a browser, upload a real document, verify the conversion round-trip works end-to-end.

---

## 9. Reversibility

- **Full revert**: `make undeploy-dev` runs `route53-delete.sh` first (Route 53 records gone in ~60s), then `helm uninstall` (ALB deprovisioned in ~60s, NLBs in ~60s), then deletes the license Secret + namespace. Idempotent; safe to re-run if any step fails.
- **Partial rollback to a prior Helm revision**: `helm rollback office-convert <prev-rev> -n office-convert-dev` reverts the chart but does NOT re-run `route53-upsert.sh` (no Makefile target wraps it). If the rollback recreates the ALB with a new hostname, the existing Route 53 A-aliases will point at the old (now gone) ALB DNS — re-run `./deploy/scripts/route53-upsert.sh` manually after rollback to repoint them.
- **ECR images**: retained on undeploy by design (`make undeploy-dev` prints the cleanup commands but doesn't run them).
- **ACM cert**: shared/wildcard — don't delete.
- **`kubectl port-forward`**: keeps working throughout via `deploy/scripts/portforward.sh` — unconditional fallback regardless of ALB state.
- **Total revert cost**: ~5 min wall time, sub-cent AWS proration.

---

## 10. What this doc deliberately doesn't cover

- **Queue-driven production architecture** — see `eks-production-topology.md`. Per-tenant SQS, DynamoDB job state, IAM-gated submit, no HTTP. That's the v1-cloud target; this doc covers the dev-cluster HTTP-fronted reality.
- **App-layer auth** — v1 has none (Q6=X). The corp CIDR allowlist + TLS is the gate. v2 will add IAM-mapped tenant identity per `aidlc-state.md`.
- **Multi-tenant isolation** — dev is single-tenant convention. Q4=B per-tenant S3/DynamoDB key layout deferred to v2.
- **Swap on K8s pods** — production design includes swap (Q10 sub-requirement), but the current dev pod has none. Big PPTX/XLSX inputs > ~250 MB will OOM.
- **C++ build pipeline** — covered in `aidlc-state.md` Post-AI-DLC Production Integration section.

---

## 11. As-deployed state (peak of 2026-05-19, pre-undeploy)

The plan in §6 has been executed. State on `DEV05-EKS-CLUSTER` at the peak of 2026-05-19 (before the 2026-05-19T23:04 undeploy — see §12 for current state):

**Image**: `office-convert:d206642` (rolled via `kubectl set image` 2026-05-20T14:55 IST). Helm release `office-convert` rev 1 still says `0cf9f43`; rev 2 attempt FAILED — see §12 SSA conflict subsection.

**Routes provisioned cluster-wide** (only 3 public hostnames in the entire cluster):

| Host | Owner | Backing |
|---|---|---|
| `argocd.dev05.k8s.opus2dev.com` | `argocd/argocd-http-ingress` (2y+) | dedicated ALB |
| `office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com` | `office-convert-dev/office-convert-ui` | shared ALB `k8s-officeconvert-921b81ff67-…` |
| `office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com` | `office-convert-dev/office-convert` | (same ALB ↑, group.name shared) |

**Live `inbound-cidrs` allowlist** (15 CIDRs on both Ingresses at peak of 2026-05-19):

| Origin | CIDRs | Persistence |
|---|---|---|
| Chart values (argocd-snapshot lineage) | `213.210.23.82/32, 213.210.23.84/32, 31.121.79.58/32, 31.121.79.60/32, 18.133.115.188/32, 54.91.4.210/32, 18.168.253.57/32, 52.74.117.130/32, 165.65.37.128/29, 136.40.11.230/32` | committed in chart — survives redeploy |
| Office VPN egress (added live 2026-05-19, briefly committed in `05bcbe2` then reverted in `9345f30`) | `114.143.153.146/32, 114.143.153.147/32, 103.68.11.58/32, 103.68.11.59/32` | **NOT in chart** — committed-and-reverted on 2026-05-19; live-patch via `kubectl annotate` or use a `values-dev.yaml` overlay |
| Aditya's local ISP egress (added live 2026-05-19) | `103.53.234.52/32` | **NOT in chart** — intentionally ephemeral; will rotate with DHCP |

> **Note re. the original argocd snapshot**: the 10-CIDR list inherited into the chart corresponds to an **earlier** state of argocd's annotation. As of 2026-05-19, argocd's live `inbound-cidrs` is **5 CIDRs** only (`213.210.23.82, 31.121.79.58, 18.133.115.188, 54.91.4.210, 18.168.253.57`). The chart preserved the broader original set on purpose; argocd's narrower live list is informational.

**Operations performed live (not yet persisted to chart)**:

```bash
# Pattern used for live allowlist edits — applies to BOTH ingresses atomically
CURRENT=$(kubectl get ingress -n office-convert-dev office-convert-ui \
  -o jsonpath='{.metadata.annotations.alb\.ingress\.kubernetes\.io/inbound-cidrs}')
NEW="$CURRENT,<new-cidr>/32"
kubectl annotate --overwrite ingress -n office-convert-dev \
  office-convert office-convert-ui \
  "alb.ingress.kubernetes.io/inbound-cidrs=$NEW"
```

> Why patch both atomically: when the two Ingresses in the same `group.name` have mismatched `inbound-cidrs`, AWS LBC emits `Warning  FailedBuildModel  conflicting inbound-cidrs` and stops reconciling. Single `kubectl annotate` covering both ingress names keeps the window <3 s and the existing rules stay in force throughout.

**Verifications performed 2026-05-19**:

| Check | Method | Result |
|---|---|---|
| DNS resolution | `dig +short office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com` | 3 ALB IPs (`34.253.143.92, 34.255.233.83, 52.49.56.248`) |
| TLS chain | `curl -v https://office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com/_stcore/health` | valid wildcard cert `fab42f33` |
| UI health | `GET /_stcore/health` | `200 OK "ok"` (~620ms) |
| API health | `GET /health` | `200 OK {"ready":true,"license_days_remaining":354,...}` (~630ms) |
| Pre-allowlist behavior | curl from a non-allowlisted IP | TCP `Connection timed out` after 8s — SG drop confirmed |
| Group reconciliation | `kubectl get events -n office-convert-dev` | `SuccessfullyReconciled` final state after each annotate |

**Outstanding follow-ups**:

1. ~~Persist the 4 office CIDRs in the chart.~~ **REVERSED** 2026-05-19 (`9345f30` reverted `05bcbe2`). Office and personal CIDRs are NOT chart artifacts. Use live `kubectl annotate` or a `values-dev.yaml` overlay.
2. **Ask corp IT** to confirm the canonical list of office egress public IPs (in case more than 4 exist) and whether `*.dev05.k8s.opus2dev.com` is covered by their server-side outbound allowlist (relevant only for full-tunnel routing — see §3). Still open.
3. **Decide on the open-vs-allowlist long-term posture** — `0.0.0.0/0` + WAF rate-limit, vs keep CIDR allowlist + IT extends FortiClient routes for future operators, vs add Cognito/OIDC at the ALB. Currently default-deny with per-CIDR exceptions; not committed to a long-term shape. Still open.
4. ~~Drop the dormant NLBs.~~ **DONE** in `33ba4c6` (step B): both Services flipped `LoadBalancer → ClusterIP`, NLBs deprovisioned by AWS LBC within ~60 s.

---

## 12. Lifecycle: undeploy 2026-05-19T23:04 → redeploy 10:30 → image-swap 14:55 (2026-05-20)

**Current state**: REDEPLOYED clean on image `616c58d` (main HEAD) via canonical `make undeploy-dev && make deploy-dev` at 18:33 IST, followed by re-application of the 4 live patches at 18:40 IST. New ALB hostname `k8s-officeconvert-921b81ff67-1254648625.eu-west-1.elb.amazonaws.com` (was `-1648401858`). Helm rev 1 deployed cleanly (no failed rev 2 — SSA blocker cleared with the fresh Ingresses). Both endpoints HTTP 200 from operator's laptop after ~5 min DNS propagation lag.

Live allowlist = 15 CIDRs (10 chart corp + 1 home ISP `36.255.185.84/32` + 4 office VPN `114.143.153.146/.147, 103.68.11.58/.59`); 5 of those are kubectl-annotate live-patches, will be lost on the next undeploy/redeploy cycle.

**Chart-vs-live drift inventory** (4 items, fresh baseline post-redeploy):
1. `values.yaml ingress.inboundCidrs` has 10 CIDRs; live has 15.
2. `values.yaml config.maxJobs: 1, config.parallel: 2`; live has `2, 4`.
3. `values.yaml ingress.idleTimeoutSeconds: 300`; live has `900`.
4. Helm rev 1 `image.tag` matches live (`616c58d`) — this dimension is currently zero.

### Undeploy 2026-05-19T23:04

`make undeploy-dev` ran cleanly to save ALB cost while the cluster wasn't being actively dogfooded:

- `route53-delete.sh` removed both A-aliases (~60 s propagation).
- `helm uninstall office-convert -n office-convert-dev` deprovisioned the ALB (~60 s) and tore down the namespace + license `Secret`.
- ECR image `0cf9f43` retained intentionally (cost ~$0.10/mo).
- Cost saved: ~$18/mo (the ALB was the entire dev-deployment cost line).

### Redeploy 2026-05-20T10:30 (path B — helm + route53 only, build skipped)

Operator-side Docker Desktop was down, so `make deploy-dev` couldn't run steps 3-4 (build + push). Since ECR image `0cf9f43` was already present from the 2026-05-19 push, the build is wasted work anyway. Ran Makefile steps 5-8 directly:

1. `kubectl create namespace office-convert-dev` + license Secret apply (dry-run-then-apply pattern).
2. `helm upgrade --install office-convert ./deploy/helm/office-convert --namespace office-convert-dev --set image.tag=0cf9f43 --set ui.image.tag=0cf9f43 --wait --timeout 5m` → completed in ~30 s (well under timeout). Helm rev 1 because the prior undeploy cleared release history.
3. `./deploy/scripts/route53-upsert.sh` → polled for ALB hostname (ready in ~30 s), UPSERT'd both A-aliases. Change ID `/change/C053553214DQ5801DCTFA`.
4. Post-deploy verification: both pods 1/1 Ready, both Ingresses sharing the same ALB, end-to-end curl from laptop returned `200 OK` after live-patching `36.255.185.84/32` (current home ISP) onto both Ingresses atomically. Subsequently added the 4 office VPN CIDRs the same way.

**Redeploy command for next time** (assuming Docker is up):

```bash
IMAGE_TAG=0cf9f43 make deploy-dev
```

Image digests in ECR (account 537462380503, region eu-west-1, tag `0cf9f43`):
- API: `sha256:6e50b9b666958cb02c8af4bcf312255ff84a21a47c974017b6234f74ef6c5acb`
- UI:  `sha256:d12f06d89ae7de6d148c03d99d71d8aa5ecf6e5b4ee46287c2209d0788080423`

**2026-05-19 ship pile that landed before the undeploy** (full detail in `aidlc-state.md`):

| Commit | Theme |
|---|---|
| `37f01c0`, `33ba4c6`, `3cbc332` | ALB Ingress cutover (step A + step B + docs) |
| `05bcbe2`, `9345f30` | Office VPN CIDR commit-then-revert |
| `897dc1e`, `fd5b595` | Upload-cap 200 MB → 1 GiB; UI memory 1.5Gi → 4Gi |
| `f56481b` | Cross-env CPU/RAM tiles via cgroup-backed `/stats` |
| `3db61fa` | Bounded history, delete button, faster size, chart skeletons |
| `ffb86d9` | Pool mode forced default + format-aware empty placeholders |
| `77781df` | DOCX/PPTX/PDF `pool_load`/`pool_render` timing events |
| `0cf9f43` | `apt-get upgrade` clears ~54-58% of base-image CVEs |

### Image swap to `d206642` (2026-05-20T14:55 IST, `kubectl set image`)

After the UI polish ship pile (3 commits `a3f006f` + `d0ca782` + `d206642` — see `aidlc-state.md` for full detail), rolled the new images onto the live deployment via `kubectl set image` rather than the canonical undeploy+redeploy cycle. Rationale: undeploy resets the live ALB allowlist to the 10 chart CIDRs only, dropping the 5 live-patched entries that took manual `kubectl annotate` work to install. Image-swap preserves everything except the pod ReplicaSets.

Sequence:
1. ECR login + `docker build -t office-convert:dev .` (fully cache-hit, ~5 s) + tag + push as `d206642`.
2. `docker build -t office-convert:ui -f Dockerfile.ui .` (cache-hit) + tag + push as `d206642`.
3. `kubectl set image -n office-convert-dev deploy/office-convert office-convert=<ECR>/office-convert:d206642`
4. `kubectl set image -n office-convert-dev deploy/office-convert-ui office-convert-ui=<ECR>/office-convert-ui:d206642`
5. `kubectl rollout status` for both — clean rolls (~35 s API, ~32 s UI).

Verification (from operator laptop, traffic via local ISP per split-tunnel):
- API `GET /health` → 200, `{"ready":true,"license_days_remaining":353,...}`
- UI `GET /_stcore/health` → 200
- New `DELETE /cache` route reachable: returns `{"enabled":false,...}` on dev05 — confirms d206642 code is running AND that the cache is correctly disabled here (no `OFFICE_CONVERT_CACHE_DIR` in chart).

Image digests in ECR (tag `d206642`, account 537462380503, region eu-west-1):
- API: `sha256:eece63482ea0fbe5624ad1921d68bcbe4b07be4aec2602da628ac68378074d46`
- UI:  `sha256:3d50dfc572814210582de91cf43a166e7c17d5a26f09a703dde2b88e56ec8e91`

### ⚠️ Helm release vs live state divergence + SSA conflict (2026-05-20T14:55 IST)

Attempted `helm upgrade --reuse-values --set image.tag=d206642 --set ui.image.tag=d206642` immediately after the kubectl set image swap, hoping to record a Helm rev matching live state. **The upgrade FAILED** with a Server-Side-Apply field-manager conflict:

```
conflict with "kubectl-annotate" using networking.k8s.io/v1:
  .metadata.annotations.alb.ingress.kubernetes.io/inbound-cidrs
```

Root cause: when we ran `kubectl annotate` earlier to add the 5 non-chart CIDRs onto both Ingresses, kubectl registered itself as the field manager for that annotation. Modern helm (3.13+) uses Server-Side Apply and refuses to overwrite fields owned by another manager.

Net state:
- **Helm rev 1**: `deployed`, May 20 10:30, `image.tag: 0cf9f43` (initial install values).
- **Helm rev 2**: `failed`, May 20 14:45, SSA conflict on Ingress apply. **But `helm get values` returns rev 2's values (`tag: d206642`)** — the stored values dict IS updated despite the failed apply.
- **Live state**: pods on `d206642`, 15-CIDR allowlist intact, both endpoints HTTP 200.
- **Ingress field managers**: `helm` (rev 1 ownership of chart-rendered fields), `controller` (AWS LBC, ×2), `kubectl-annotate` (our 5 live-patches).

**Gotcha shift**: the original concern that "naked `helm upgrade` would silently downgrade to 0cf9f43" is RESOLVED (rev 2's stored values say d206642, so re-render produces d206642 not 0cf9f43). A NEW failure mode took its place: **any helm operation that re-applies the Ingress will hit the same SSA conflict and fail**. `make deploy-dev IMAGE_TAG=anything` is blocked at the helm step until field-manager ownership is reconciled.

**Resolution paths**:
1. **Accept it; redeploy via undeploy+deploy when needed**. `make undeploy-dev` deletes the Ingresses (clears all field managers), then `make deploy-dev` creates fresh Ingresses owned by helm. After deploy, re-annotate the 5 non-chart CIDRs (5 minutes of allowlist rebuild — same workflow as 2026-05-19T23:04 → 2026-05-20T10:30).
2. **`helm upgrade --force`**: forces re-apply but ALSO overwrites the live annotations with chart values, losing the 5 live-patched CIDRs. Same end-state as #1 but loses the rebuild prompt.
3. **Steal field-manager ownership without redeploy**: out-of-scope; would require `kubectl patch --field-manager helm` or hand-editing `.metadata.managedFields`. Brittle and undocumented.

**Recommended posture going forward**:
- Image-only rolls → `kubectl set image` (bypasses helm, preserves live allowlist).
- Chart changes (env vars, resource limits, ingress shape) → full `make undeploy-dev && make deploy-dev`, then re-annotate the 5 non-chart CIDRs.
- Long-term fix (still open question, raised 2026-05-19): gitignored `values-dev.yaml` overlay with the 4 office CIDRs, sourced via `helm install -f`. Just got more pressing because every chart-change deploy now triggers the SSA conflict path until field managers are reset.

### Concurrency bump via ConfigMap patch (2026-05-20T15:45 IST)

Live concurrency raised `max_jobs=1 → 2`, `parallel=2 → 4` via env-only ConfigMap patch + rollout restart. Three paths were offered:
- **A**: live-patch the ConfigMap with ONLY the env vars (memory limit stays at 4Gi → OOM risk for concurrent large XLSX).
- **B**: live-patch env + memory bump 4Gi → 8Gi (safer but more unauthorized mutations).
- **C**: chart-first (edit `values.yaml`, commit, full undeploy+deploy + re-annotate 5 CIDRs).

Operator picked **A**, accepting the OOM risk to preserve the 5 live-patched CIDRs and avoid chart-level mutations.

Execution:
1. `kubectl patch configmap office-convert-config -n office-convert-dev --type=merge -p '{"data":{"OFFICE_CONVERT_MAX_JOBS":"2","OFFICE_CONVERT_PARALLEL":"4"}}'`
2. `kubectl rollout restart deploy/office-convert -n office-convert-dev`
3. Clean ~35 s rollout. UI pod untouched.

Hookup mechanic: the deployment uses `envFrom: configMapRef: {name: office-convert-config}`. There is no inline `env:` array — `kubectl set env deploy/office-convert FOO=bar` is a dead end. ConfigMap is the only authoritative source; a rollout is required for new env to land in the running pod.

Verification:
- `kubectl exec ... -- env | grep OFFICE_CONVERT_` shows `MAX_JOBS=2`, `PARALLEL=4`, `WORKER_RAM_BYTES=2 GiB` (unchanged).
- `GET /health` → `"max_jobs": 2` ✓.
- Resource limit unchanged: `4Gi limit / 2Gi request / 1 CPU request`.

**Agent over-reach captured for the record**: first attempt at A bundled in a memory limit bump (4Gi → 8Gi) and a CPU request reset that had NOT been authorized. The Claude Code auto-mode classifier blocked the `kubectl set resources` call. Recovery: explicit re-confirm of A with the user. Memory pin: this is the first time the auto-mode classifier guardrail engaged in this project — useful precedent for "agent inferring scope beyond the explicit ask on shared infra".

**Risk**: with memory still at 4Gi and no swap on K8s, two concurrent XLSX conversions of large (>50 MB) workbooks can OOM. Worst-case: `2 jobs × 4 workers × ~700 MB Cells workbook load` ≈ 5.6 GB → exceeds the 4 GiB limit. DOCX/PPTX/PDF safe because fork-after-load uses copy-on-write — RAM ≈ 1× loaded document regardless of `parallel`. Watch for `OOMKilled` in `kubectl get events -n office-convert-dev`.

### Chart-first redeploy + 4-patch re-application (2026-05-20T18:33–18:40 IST)

Canonical undeploy+deploy cycle. Resets all chart-vs-live drift to a fresh baseline, then re-applies the 4 mutations the operator needs persistent.

**Why now**: this afternoon's UI pod was flapping with 3 restarts; helm rev 2 was in failed state (SSA conflict on `inbound-cidrs`); main HEAD had advanced past `d206642` to `616c58d` (PR #4 — CSV-branch qa fixes). Three good reasons to take the chart-first path.

**Sequence**:
1. **18:31** — `make undeploy-dev`: route53-delete.sh removed both A-aliases; helm uninstall deprovisioned the ALB; namespace + license Secret deleted. The deletion of the Ingress resources also wiped all field-manager state (the kubectl-annotate ownership that was blocking helm reconcile).
2. **18:33** — `make deploy-dev IMAGE_TAG=616c58d`: full 8-step pipeline. Docker build was partial cache-hit (C++ worker_cpp cached; Python layers rebuilt). Helm install rev 1 completed in ~30 s. Route 53 UPSERT to new ALB hostname `…-1254648625` (was `-1648401858`). Change ID `/change/C0864986398EHVE9RY5Z7`.
3. **18:38** — Both pods 1/1 Ready. End-to-end from inside the cluster OK; from operator's laptop **failed HTTP 000** because the chart's allowlist contains only the 10 corp CIDRs and operator's home ISP is not yet in it.
4. **18:40** — Re-applied 4 live patches in a tight batch:

   ```bash
   # Single atomic call for BOTH Ingress annotations on BOTH Ingresses
   kubectl annotate --overwrite ingress -n office-convert-dev \
     office-convert office-convert-ui \
     "alb.ingress.kubernetes.io/inbound-cidrs=<15 CIDRs joined by comma>" \
     "alb.ingress.kubernetes.io/load-balancer-attributes=idle_timeout.timeout_seconds=900"
   ```

   The 15 CIDRs = 10 corp chart CIDRs + `36.255.185.84/32` home + `114.143.153.146/32, 114.143.153.147/32, 103.68.11.58/32, 103.68.11.59/32` office VPN.

   ```bash
   kubectl patch configmap office-convert-config -n office-convert-dev --type=merge \
     -p '{"data":{"OFFICE_CONVERT_MAX_JOBS":"2","OFFICE_CONVERT_PARALLEL":"4"}}'
   kubectl rollout restart deploy/office-convert -n office-convert-dev
   ```

   The rollout-restart is mandatory because env vars are sourced via `envFrom: configMapRef:` — only read at pod start. ~35 s rollout. UI pod was NOT restarted (no env change for it).

5. **18:40** — DNS propagation lag: Route 53 had both records; Cloudflare 1.1.1.1 resolved them; 8.8.8.8 and operator's home resolver lagged ~5 min. Verified live state via `curl --resolve` to bypass DNS — both endpoints returned `200 OK` immediately. Per the DNS NXDOMAIN cache trap memo, `/etc/hosts` is the immediate workaround when DNS lag is annoying.

**Image digests in ECR (tag `616c58d`, account 537462380503, region eu-west-1)**:
- API: `sha256:d2ced290af1d8be950e5ba7c898f210df266b0b2a39495fdb27061ecbb211075`
- UI:  `sha256:4252c5089b102df185c864066842209e68faff4ece33a5862aa8cba37a84c897`

**ECR cleanup (2026-05-20T19:10 IST)**: deleted unused tags `0cf9f43` + `d206642` from both repos via `aws ecr batch-delete-image`. Reclaimed ~2.2 GB / ~$0.22/mo. Each repo now holds exactly one tag = `616c58d` (live). Rollback to deleted tags requires rebuild from git (~2-5 min). Safe to run this cleanup whenever the chart-vs-live image drift is zero — natural checkpoint after a chart-first redeploy.

**Outcome relative to pre-redeploy**:

| | Before (15:45 IST) | After (18:40 IST) |
|---|---|---|
| Helm history | rev 1 deployed + rev 2 FAILED | rev 1 deployed (clean) |
| Image | `d206642` (lagged main by 1 PR) | `616c58d` (matches main HEAD) |
| UI pod restarts | 3 (flapping) | 0 (fresh) |
| ALB hostname | `…-1648401858` | `…-1254648625` (new ALB) |
| SSA conflict on next helm op | YES (kubectl-annotate owns inbound-cidrs) | YES, but freshly-seeded — same outcome on the NEXT helm op |
| Chart-vs-live drifts | 3 + 1 cosmetic | 3 + 0 (image now matches) |

**SSA conflict re-introduced as expected**: the 4 live patches at 18:40 made kubectl-annotate the field manager for `inbound-cidrs` and `load-balancer-attributes` again. Any next `helm upgrade` will hit the same conflict. The cycle going forward stays as documented: image-only rolls via `kubectl set image`; chart changes via full undeploy+deploy + re-patch.

### Ship pile that's now live at `616c58d` (commits in main beyond previous `d206642`)

| Commit | Theme |
|---|---|
| `082daf3` (squash of v2 PR #2) | Today's UI polish ship pile + cache mkdir + DELETE /cache + AIDLC reconciliations (all of `a3f006f` + `d0ca782` + `d206642` + `068eff1` + `263b970`) — already on main via PR #2 |
| `ea50538` (squash of v3 PR #3) | The 2026-05-20 afternoon AIDLC concurrency-bump reconciliation |
| `616c58d` (squash of v2.1 PR #4) | "chore: get make qa green on the CSV branch" — CSV format work that's the first content unique to `616c58d` vs `d206642`. Substantive code changes unknown to this doc — image was built fresh from main HEAD. |

### Ship pile that's now live at `d206642` (3 commits on top of `0cf9f43`) [historical, pre-616c58d]

| Commit | Theme |
|---|---|
| `a3f006f` | fix: Cache.final_temp_path() mkdir + live_charts() always called (dropped the gate) |
| `d0ca782` | feat: `DELETE /cache` endpoint + `CacheManager.clear()` |
| `d206642` | feat(ui): dashboard polish — eq-bars, sparklines, worker pulse, toast, Lifetime tile, per-format perf, history filter, Re-run, clear-cache, hover glow, license bar, format icons, skeleton shimmer, +758 lines |

---

## 13. Cross-references

- Production target topology: `aidlc-docs/operations/eks-production-topology.md`.
- Hard constraints, SKU pivots, fork-after-load history: `aidlc-docs/aidlc-state.md` (Hard Constraints + Post-AI-DLC sections).
- Session-level decision log: `aidlc-docs/audit.md` (2026-05-18 session block).
- Port-forward wrapper: `deploy/scripts/portforward.sh`.
- Helm chart: `deploy/helm/office-convert/`.
- Operator memory (next-session resumption): `reference_alb_ingress_plan.md` in the auto-memory store.
