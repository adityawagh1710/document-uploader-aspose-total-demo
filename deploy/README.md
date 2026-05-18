# deploy/

EKS dev-cluster deployment artifacts for `office-convert`. The Compose
workflow under the repo root (`compose.yaml`) is the canonical local
runtime; this directory is for the **shared dev cluster** install on
`DEV05-EKS-CLUSTER` (namespace `office-convert-dev`).

For the full topology, ingress decisions, and the in-flight ALB Ingress
migration plan, read
[`aidlc-docs/operations/dev-deployment-topology.md`](../aidlc-docs/operations/dev-deployment-topology.md).

## Layout

```
deploy/
├── README.md           ← this file
├── helm/
│   └── office-convert/ ← Helm chart (API + UI; values.yaml is the entry point)
├── scripts/
│   ├── portforward.sh  ← idempotent kubectl port-forward wrapper for API + UI
│   └── eks-vpn-routes.sh  ← adds /32 routes for the EKS API endpoint after VPN reconnect
└── logs/               ← timestamped deploy / undeploy / port-forward logs (gitignored)
```

## Prerequisites

- `kubectl` configured against `DEV05-EKS-CLUSTER` (account `537462380503`, region `eu-west-1`, profile `opus2-dev`).
- `helm` v3.16+.
- `aws` CLI with `opus2-dev` SSO credentials.
- Corp VPN active. EKS API endpoint reaches via `/32` routes pushed by `eks-vpn-routes.sh`. NLB private IPs are NOT reachable from the laptop today (corp does not peer with the VPC) — use `portforward.sh` for browser access.

## Common workflows

All commands run from the repo root unless noted.

### Install / update

```bash
AWS_ACCOUNT_ID=537462380503 AWS_REGION=eu-west-1 \
    IMAGE_TAG=$(git rev-parse --short HEAD) \
    make deploy-dev
```

`make deploy-dev` performs a **full undeploy + redeploy** cycle (not
`helm upgrade` in place — this is the project's deliberate workflow).
Pushes API + UI images to ECR if missing, then `helm install`s the chart.
Logs land in `deploy/logs/deploy-<timestamp>.log`; the rendered manifest
lands in `deploy/logs/manifest-<timestamp>.yaml`.

### Tear down

```bash
make undeploy-dev
```

`helm uninstall` + namespace cleanup. AWS Load Balancer Controller
deprovisions the NLBs on the way out (~1 min). Re-running `make deploy-dev`
later allocates fresh NLB hostnames — AWS doesn't recycle ELB names.

### Browser access (port-forward)

```bash
./deploy/scripts/portforward.sh start
# → API on http://localhost:18080  (FastAPI + Swagger at /docs)
# → UI  on http://localhost:8501   (Streamlit dashboard)

./deploy/scripts/portforward.sh status     # show PIDs + endpoint health
./deploy/scripts/portforward.sh restart
./deploy/scripts/portforward.sh stop
```

The script auto-picks free local ports (API base 18080, UI base 8501,
walks 10 consecutive ports if needed), kills its own previous instances,
re-runs `eks-vpn-routes.sh add` if kubectl can't reach the cluster, and
health-probes both endpoints. Logs land in
`deploy/logs/portforward-{api,ui}.log`. Tracked state lives in
`/tmp/officeconvert-portforward-{api,ui}.{pid,port}`.

### VPN route maintenance

```bash
./deploy/scripts/eks-vpn-routes.sh add      # restore /32 routes after VPN reconnect
./deploy/scripts/eks-vpn-routes.sh remove   # roll back
```

`portforward.sh start` calls this automatically when `kubectl` can't reach
the cluster, so direct invocation is only needed for ad-hoc kubectl
sessions outside the port-forward flow.

## What's in the chart

`helm/office-convert/` ships:

- **API Deployment** — FastAPI orchestrator (`uvicorn office_convert.server:app`), `replicaCount: 1`, `max_jobs: 1`, `parallel: 2`. Resources: `1–4 CPU`, `2–4 GiB memory`. **No swap on K8s** (vs. compose's 6 GiB cushion); big PPTX/XLSX inputs > ~250 MB will OOM the pod.
- **UI Deployment** — Streamlit dashboard (`test_ui.py`). Resources: `0.1–0.5 CPU`, `512Mi–1.5Gi memory` (bumped from 512Mi after OOMKills on 2026-05-18). Talks to the API via in-cluster DNS.
- **Two LoadBalancer Services** — both `scheme: internal` NLBs today. **Migration to ALB Ingress + ACM TLS planned** — see `aidlc-docs/operations/dev-deployment-topology.md` §4 for the target shape and §6 for the change list.
- **ConfigMap** — `OFFICE_CONVERT_*` env vars; consumed by Pydantic's `env_prefix` in `office_convert/config.py`.
- **License via existing Secret** — `aspose-license` Secret must exist before `helm install`. Not chart-managed; create with:
  ```bash
  kubectl create secret generic aspose-license \
      --from-file=license.lic=./Aspose.TotalforC++.lic \
      --namespace office-convert-dev
  ```

## Known constraints

- **NLB reachability**: corp FortiClient VPN gives `/32` routes for the EKS API endpoint (kubectl works) but does NOT tunnel VPC private CIDRs. NLB private IPs in `10.35.0.0/16` are unreachable from the laptop. Verified by route probe 2026-05-18 — adding `ip route add 10.35.0.0/16 via 192.168.8.24 dev fctvpndc0b79cc` does NOT help (corp's server-side routing has no path to the VPC). **The workaround is `portforward.sh`; the migration is ALB Ingress with a corp-CIDR-allowlisted internet-facing URL.**
- **UI runs as root**: `Dockerfile.ui` doesn't set `USER`. Explicit TODO in `values.yaml`. Pod's `securityContext` is empty (the shared `podSecurityContext.runAsNonRoot: true` would block the UI from starting otherwise).
- **No application-layer auth in v1**: Q6 = X in `aidlc-docs/aidlc-state.md`. Whatever sits in front of the LB is the only gate. Today that's "VPC-internal only"; post-ALB-migration it will be "corp CIDR allowlist + TLS".

## Cross-references

- Current state + open decisions: `aidlc-docs/operations/dev-deployment-topology.md`.
- Cluster topology + AWS prerequisites: `~/.claude` operator memory (`reference_eks_cluster_topology`, `reference_alb_ingress_plan`).
- Hard constraints, SKU pivots, fork-after-load history: `aidlc-docs/aidlc-state.md`.
- Session decision log: `aidlc-docs/audit.md`.
