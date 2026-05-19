# Dev Deployment Topology — office-convert-dev

**Status**: Active. Iterating. **Diverges intentionally from `eks-production-topology.md`** — that doc captures the queue-driven (Q5=D) production design; this doc captures the HTTP-fronted dev shape we're actually running and evolving.

**Cluster**: `DEV05-EKS-CLUSTER` (region `eu-west-1`, account `537462380503`, profile `opus2-dev`).

**Namespace**: `office-convert-dev`. Helm release: `office-convert`.

**Last update**: 2026-05-19.

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
│      • resources: 0.1-0.5 CPU, 512Mi-1.5Gi memory (bumped from 512Mi after    │
│        OOMKills on 2026-05-18).                                               │
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

**Decision converged 2026-05-18, implemented 2026-05-19**: ALB Ingress mirroring argocd's pattern, **two Ingresses sharing one ALB** via `alb.ingress.kubernetes.io/group.name: office-convert`, **subdomain routing**. Currently coexists with the dormant internal NLBs (commit A "alongside NLBs"); the dormant NLB drop is local commit `33ba4c6`, unpushed.

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

## 6. As-built change set (commit A — `37f01c0`, 2026-05-19)

The plan in this section has been executed. Commit A landed the ALB Ingress **alongside the dormant NLBs** (both still provisioned per request — full cutover is the unmerged local commit `33ba4c6` "step B").

**Helm chart, as-shipped at `37f01c0`** (`deploy/helm/office-convert/`):

| File | State at commit A | Cutover (commit B, local-only) |
|---|---|---|
| `templates/api-service.yaml` | Unchanged — `type: LoadBalancer` + internal-NLB annotations (NLB still provisioned alongside ALB). | Switch to `type: ClusterIP`, drop NLB annotations. |
| `templates/ui-service.yaml` | Unchanged — `type: LoadBalancer` + internal-NLB annotations. | Switch to `type: ClusterIP`, drop NLB annotations. |
| `templates/ingress.yaml` | **NEW**. Two `Ingress` resources sharing `group.name: office-convert`, each `host:`-routed. SSL-redirect action wired on port 80. Per-Ingress `healthcheck-path` (UI: `/_stcore/health`, API: `/health`). | No change. |
| `values.yaml` | New `ingress:` block: `enabled: true`, `uiHost`, `apiHost`, `certificateArn`, `inboundCidrs` (10-CIDR argocd-lineage), `groupName: office-convert`, `sslPolicy`, `idleTimeoutSeconds: 300`, healthcheck paths. Service `type:` left at `LoadBalancer`. | Service `type:` flipped to `ClusterIP`, NLB annotations removed. |

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

## 11. As-deployed state (2026-05-19)

The plan in §6 has been executed. Live state on `DEV05-EKS-CLUSTER`:

**Image**: `office-convert:37f01c0` (digest `sha256:b5910a7…`). Helm release `office-convert`, rev 1 at this image tag.

**Routes provisioned cluster-wide** (only 3 public hostnames in the entire cluster):

| Host | Owner | Backing |
|---|---|---|
| `argocd.dev05.k8s.opus2dev.com` | `argocd/argocd-http-ingress` (2y+) | dedicated ALB |
| `office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com` | `office-convert-dev/office-convert-ui` | shared ALB `k8s-officeconvert-921b81ff67-…` |
| `office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com` | `office-convert-dev/office-convert` | (same ALB ↑, group.name shared) |

**Live `inbound-cidrs` allowlist** (15 CIDRs on both Ingresses):

| Origin | CIDRs | Persistence |
|---|---|---|
| Corp-egress lineage (originally from argocd) | `213.210.23.82/32, 213.210.23.84/32, 31.121.79.58/32, 31.121.79.60/32, 18.133.115.188/32, 54.91.4.210/32, 18.168.253.57/32, 52.74.117.130/32, 165.65.37.128/29, 136.40.11.230/32` | committed in chart — survives redeploy |
| Office VPN egress | `114.143.153.146/32, 114.143.153.147/32, 103.68.11.58/32, 103.68.11.59/32` | committed in chart 2026-05-19 — survives redeploy |
| Personal local-ISP egress | `103.53.234.52/32` | **NOT in chart** — intentionally ephemeral; rotates with DHCP |

> See `deploy/helm/office-convert/values.yaml` `ingress.inboundCidrs` for the committed 14; the 1 personal-ISP CIDR is applied via the `kubectl annotate` recipe below.

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

1. **Persist the 4 office CIDRs in the chart** — `deploy/helm/office-convert/values.yaml` `ingress.inboundCidrs:`. The personal `103.53.234.52/32` must NOT be committed (it will rotate with the ISP's DHCP).
2. **Ask corp IT** to confirm the canonical list of office egress public IPs (in case more than 4 exist) and whether `*.dev05.k8s.opus2dev.com` is covered by their server-side outbound allowlist (relevant only for full-tunnel routing — see §3).
3. **Decide on the open-vs-allowlist long-term posture** — `0.0.0.0/0` + WAF rate-limit, vs keep CIDR allowlist + IT extends FortiClient routes for future operators, vs add Cognito/OIDC at the ALB. Currently default-deny with per-CIDR exceptions; not committed to a long-term shape.
4. **Drop the dormant NLBs** — `office-convert` + `office-convert-ui` Services are still `type: LoadBalancer` with internal NLB hostnames, alongside the ALB. Local commit `33ba4c6` cuts them over to `ClusterIP`. Safe to deploy now that the ALB is verified.

---

## 12. Cross-references

- Production target topology: `aidlc-docs/operations/eks-production-topology.md`.
- Hard constraints, SKU pivots, fork-after-load history: `aidlc-docs/aidlc-state.md` (Hard Constraints + Post-AI-DLC sections).
- Session-level decision log: `aidlc-docs/audit.md` (2026-05-18 session block).
- Port-forward wrapper: `deploy/scripts/portforward.sh`.
- Helm chart: `deploy/helm/office-convert/`.
- Operator memory (next-session resumption): `reference_alb_ingress_plan.md` in the auto-memory store.
