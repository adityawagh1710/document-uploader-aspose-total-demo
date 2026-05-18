# Dev Deployment Topology — office-convert-dev

**Status**: Active. Iterating. **Diverges intentionally from `eks-production-topology.md`** — that doc captures the queue-driven (Q5=D) production design; this doc captures the HTTP-fronted dev shape we're actually running and evolving.

**Cluster**: `DEV05-EKS-CLUSTER` (region `eu-west-1`, account `537462380503`, profile `opus2-dev`).

**Namespace**: `office-convert-dev`. Helm release: `office-convert`.

**Last update**: 2026-05-18.

---

## 1. Why this doc exists

The AI-DLC production design (`eks-production-topology.md`) is queue-driven: clients submit via per-tenant SQS, no HTTP endpoint on the pods. That's the v1 cloud target — not yet built.

What's actually live in DEV05 is a different shape: an HTTP-fronted FastAPI orchestrator + Streamlit UI, deployed via Helm, exposed via AWS load balancers. This is how the operator dogfoods the same `office_convert` codebase that powers compose locally. It's neither v1-as-designed nor v2 — it's the "dev cluster running the same image as compose, but on EKS so we can demo and test scale" reality.

This file is the source of truth for that reality.

---

## 2. Current topology (as of 2026-05-18, pre-ALB)

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
| Random public IP | ❌ (allowlist) | ❌ (internal NLB) |

**The FortiClient VPC peering probe (2026-05-18, ruled out permanently)**: User added EE VPN via FortiClient (interface `fctvpndc0b79cc`). Manually adding `ip route add 10.35.0.0/16 via 192.168.8.24 dev fctvpndc0b79cc` correctly routed packets through the tunnel, but they timed out at corp HQ — corp's server-side routing does NOT include the EKS VPC `10.35.0.0/16`. Path permanently dead; do not retry.

**Workaround in active use**: `kubectl port-forward` via `deploy/scripts/portforward.sh`. Reliable but every operator needs kubectl + VPN.

---

## 4. Target topology (next, pre-implementation)

**Decision converged 2026-05-18**: ALB Ingress mirroring argocd's pattern, **two Ingresses sharing one ALB** via `alb.ingress.kubernetes.io/group.name: office-convert`, **subdomain routing**.

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
| external-dns | **NOT installed** | — Route 53 records must be created manually |

**Argocd cert gotcha**: `argocd-http-ingress` currently references the **expired** cert `213a9222-0466-4e0f-9ca2-87e92c92944c`. Don't copy that ARN. Use the wildcard `fab42f33` for new apps.

---

## 6. Exact change plan (Path 1)

**Helm chart edits** (`deploy/helm/office-convert/`):

| File | Change |
|---|---|
| `templates/api-service.yaml` | Drop `aws-load-balancer-*` annotations; switch default `type: LoadBalancer` → `ClusterIP` via values.yaml. |
| `templates/ui-service.yaml` | Same — drop NLB annotations, switch to ClusterIP. |
| `templates/ingress.yaml` | **NEW**. Two `Ingress` resources sharing `group.name: office-convert`, each with `spec.rules[].host:` set. SSL-redirect action, healthcheck-path per service. |
| `values.yaml` | Switch service `type` defaults; add `ingress:` block with `enabled`, `uiHost`, `apiHost`, `certificateArn`, `inboundCidrs`, `groupName`, `sslPolicy`, `idleTimeoutSeconds`. |

**Outside Helm (one-time, manual)**:
1. After `make deploy-dev`, get the ALB hostname:
   ```bash
   kubectl -n office-convert-dev get ingress -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'
   ```
2. Create two A-alias records pointing at it via `aws route53 change-resource-record-sets --hosted-zone-id Z045669519R5D9D8CKC79 ...` (one per hostname).

---

## 7. Pre-deploy verifications

Before running `make deploy-dev` with the new chart:

1. **Refresh corp CIDR allowlist** — argocd's snapshot has `136.40.11.230/32` added since 2024 baseline. Verify with network admin that the current corp egress + your FortiClient IP are in the list, otherwise you'll create an ALB you can't reach.
2. **Confirm wildcard cert is still ISSUED**:
   ```bash
   aws acm describe-certificate \
     --certificate-arn arn:aws:acm:eu-west-1:537462380503:certificate/fab42f33-7d67-4ecf-b200-38af584485b0 \
     --region eu-west-1 --profile opus2-dev \
     --query 'Certificate.Status'
   ```
   Expect: `"ISSUED"`.
3. **Confirm Route 53 zone write access** — try a dry-run via `aws route53 change-resource-record-sets` with minimal payload to surface any permission gap.
4. **Streamlit websocket idle timeout** — set `alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=300` on the UI Ingress (default 60s will hang Streamlit's persistent websocket). Group-shared LB attributes are merged across Ingresses; setting on both with the same value is safest.
5. **Health-check paths** — UI: `/_stcore/health`; API: `/health` (default).

---

## 8. Post-deploy verifications

1. `kubectl -n office-convert-dev get ingress` → both Ingresses have an ADDRESS (same ALB hostname for both — group-shared).
2. Create both Route 53 A-aliases.
3. From a corp-allowlisted IP: `curl -v https://office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com/_stcore/health` → expect HTTP 200, valid cert chain.
4. From a NON-allowlisted IP (mobile hotspot) — same curl should hang/refuse. Confirms the allowlist works.
5. Open the UI hostname in a browser, upload a real document, verify the conversion round-trip works end-to-end (the original "real UI test" still pending since 2026-05-18 — VPN flapped, then port-forward distractions).

---

## 9. Reversibility

- `helm rollback office-convert <prev-rev> -n office-convert-dev` OR the preferred `make undeploy-dev + make deploy-dev` cycle reverses the Helm parts cleanly.
- AWS LBC reconciles: ALB removed (~1 min), NLBs recreated with new random hostnames (~2 min).
- ACM cert is shared/wildcard — don't delete.
- Route 53 records survive Helm rollback; remove manually with `aws route53 change-resource-record-sets --action DELETE` if you want them gone.
- `kubectl port-forward` keeps working throughout — unconditional fallback.
- Total revert cost: ~5 min wall time, sub-cent AWS proration.

---

## 10. What this doc deliberately doesn't cover

- **Queue-driven production architecture** — see `eks-production-topology.md`. Per-tenant SQS, DynamoDB job state, IAM-gated submit, no HTTP. That's the v1-cloud target; this doc covers the dev-cluster HTTP-fronted reality.
- **App-layer auth** — v1 has none (Q6=X). The corp CIDR allowlist + TLS is the gate. v2 will add IAM-mapped tenant identity per `aidlc-state.md`.
- **Multi-tenant isolation** — dev is single-tenant convention. Q4=B per-tenant S3/DynamoDB key layout deferred to v2.
- **Swap on K8s pods** — production design includes swap (Q10 sub-requirement), but the current dev pod has none. Big PPTX/XLSX inputs > ~250 MB will OOM.
- **C++ build pipeline** — covered in `aidlc-state.md` Post-AI-DLC Production Integration section.

---

## 11. Cross-references

- Production target topology: `aidlc-docs/operations/eks-production-topology.md`.
- Hard constraints, SKU pivots, fork-after-load history: `aidlc-docs/aidlc-state.md` (Hard Constraints + Post-AI-DLC sections).
- Session-level decision log: `aidlc-docs/audit.md` (2026-05-18 session block).
- Port-forward wrapper: `deploy/scripts/portforward.sh`.
- Helm chart: `deploy/helm/office-convert/`.
- Operator memory (next-session resumption): `reference_alb_ingress_plan.md` in the auto-memory store.
