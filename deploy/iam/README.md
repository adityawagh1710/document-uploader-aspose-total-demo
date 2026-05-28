# IAM setup for the S3 integration (IRSA)

These two templates provision the IAM role the `office-convert` API pod
assumes (via IRSA) to read S3 inputs and write/presign S3 outputs. Helm
**cannot** safely create IAM, so this is an out-of-band operator step
(matches the existing dev05 pattern).

> **STATUS — PROVISIONED on dev05 (2026-05-27).** The buckets + role below
> already exist in account `537462380503` / `eu-west-1`:
> - `s3://office-convert-dev-sandbox-input` (public access blocked)
> - `s3://office-convert-dev-sandbox-output` (public access blocked + abort-incomplete-MPU lifecycle 1d)
> - role `arn:aws:iam::537462380503:role/office-convert-dev-s3` (inline policy `office-convert-s3`, trust bound to `system:serviceaccount:office-convert-dev:office-convert`)
>
> So Phase 6 just needs a deploy with the `HELM_EXTRA_ARGS` in step 4 below;
> the "Apply" steps are kept for re-provisioning / other clusters. The
> trust-policy template already has the real OIDC id baked in.

| File | What |
|---|---|
| `office-convert-s3-policy.json` | Permission policy: `s3:GetObject` on the input bucket; `s3:PutObject` + `s3:AbortMultipartUpload` + `s3:GetObject` on the output bucket (the output-bucket `GetObject` is what lets `/v1/downloads/presign` mint a working URL). **Also includes the cross-service grant** — see §Cross-service grant below. |
| `office-convert-s3-trust-policy.json` | OIDC trust policy binding the role to the `office-convert` ServiceAccount in namespace `office-convert-dev`. |

## Cross-service grant — classification-service fanout

Sids `ReadClassificationInputs` + `WriteClassificationOutputs` (added 2026-05-28) extend the role with read access to `classification-ui-dev05/ui/*` and write access to `classification-ui-dev05/converted/*`. This is what lets the classification-service's convert-worker pass `s3_input=s3://classification-ui-dev05/ui/<docId>/<name>` and `s3_output=s3://classification-ui-dev05/converted/<docId>.pdf` to `POST /v1/convert` — office-convert downloads the source from classification's bucket, converts, and writes the PDF back to the same bucket under `converted/`. Classification then mints the presigned download URL using its OWN IRSA (no presign-on-classification grant needed here).

Re-apply after this PR merges, on the dev05 profile:

```bash
aws iam put-role-policy --role-name office-convert-dev-s3 \
  --policy-name office-convert-s3 \
  --policy-document file://deploy/iam/office-convert-s3-policy.json \
  --profile opus2-dev
```

Then redeploy with the Helm overlay that adds `classification-ui-dev05` to both bucket allowlists:

```bash
HELM_EXTRA_ARGS="--values deploy/helm/office-convert/values-classification-fanout.yaml \
  --set serviceAccount.roleArn=arn:aws:iam::537462380503:role/office-convert-dev-s3 \
  --set s3.region=eu-west-1" \
  make deploy-dev
```

The companion change on the classification side (convert-worker + `/api/classify` fanout) lives in `adityawagh1710/document-uploader-classification-service-demo` branches `feat/02-…` through `feat/07-…`. Deploy this PR's IAM + Helm changes **before** the classification side starts producing SQS messages, otherwise the worker will get 403s from `s3_input`.

## Placeholders to fill

- `<CLUSTER_OIDC_ID>` in the trust policy — from:
  ```bash
  aws eks describe-cluster --name DEV05-EKS-CLUSTER --profile opus2-dev \
    --query 'cluster.identity.oidc.issuer' --output text
  # → https://oidc.eks.eu-west-1.amazonaws.com/id/THIS_PART
  ```
- Bucket names in the permission policy (`opus2-dev-office-convert-{in,out}`)
  if you use different names. They must match `s3.inputBucketsAllowlist` /
  `s3.outputBucketsAllowlist` / `s3.defaultOutputBucket` in the Helm values.
- The trust policy's `:sub` is `system:serviceaccount:<namespace>:<sa-name>`.
  `<sa-name>` defaults to the Helm release name (`office-convert`). Change it
  if you override `serviceAccount.name` or the release/namespace.

## Apply (operator, out of band)

```bash
PROFILE=opus2-dev
REGION=eu-west-1

# 1. Buckets
aws s3 mb s3://office-convert-dev-sandbox-input  --profile $PROFILE --region $REGION
aws s3 mb s3://office-convert-dev-sandbox-output --profile $PROFILE --region $REGION
# Recommended: lifecycle rule on the output bucket to abort incomplete
# multipart uploads after 1 day (backstop for pod-killed-mid-upload).

# 2. OIDC id → fill <CLUSTER_OIDC_ID> in the trust policy
OIDC=$(aws eks describe-cluster --name DEV05-EKS-CLUSTER --profile $PROFILE \
  --query 'cluster.identity.oidc.issuer' --output text | sed 's#https://##')
sed -i "s#<CLUSTER_OIDC_ID>#${OIDC##*/}#g" office-convert-s3-trust-policy.json

# 3. Role + policy
aws iam create-role --role-name office-convert-dev-s3 \
  --assume-role-policy-document file://office-convert-s3-trust-policy.json \
  --profile $PROFILE
aws iam put-role-policy --role-name office-convert-dev-s3 \
  --policy-name office-convert-s3 \
  --policy-document file://office-convert-s3-policy.json \
  --profile $PROFILE

# 4. Deploy with S3 enabled (role ARN + allowlists)
#    make deploy-dev passes extra --set flags through HELM_EXTRA_ARGS:
HELM_EXTRA_ARGS="\
  --set serviceAccount.roleArn=arn:aws:iam::537462380503:role/office-convert-dev-s3 \
  --set s3.enabled=true \
  --set s3.region=eu-west-1 \
  --set s3.inputBucketsAllowlist=office-convert-dev-sandbox-input \
  --set s3.outputBucketsAllowlist=office-convert-dev-sandbox-output \
  --set s3.defaultOutputBucket=office-convert-dev-sandbox-output" \
  make deploy-dev

# 4b. (Cross-service variant) — use the values overlay instead of the
#     explicit --set flags above when running the classification-service
#     fanout. The overlay includes classification-ui-dev05 in both
#     allowlists. See §Cross-service grant for details.
HELM_EXTRA_ARGS="\
  --values deploy/helm/office-convert/values-classification-fanout.yaml \
  --set serviceAccount.roleArn=arn:aws:iam::537462380503:role/office-convert-dev-s3 \
  --set s3.region=eu-west-1" \
  make deploy-dev

# 5. Verify IRSA inside the pod
kubectl -n office-convert-dev exec deploy/office-convert -- \
  python -c "import boto3;print(boto3.client('sts').get_caller_identity()['Arn'])"
# → .../office-convert-dev-s3/...  (the assumed role)
```

> **IRSA propagation lag:** the first 5–15 min after role creation, the SA
> token may not work yet. Bake that into the Phase 6 timeline.
