#!/usr/bin/env bash
# Upsert A-alias records in Route 53 pointing the UI + API hostnames at the
# ALB hostname created by the Ingress. Idempotent — safe to re-run after
# every `make deploy-dev`. Polls kubectl until the Ingress has an ALB
# hostname (takes ~30-90 s after a fresh deploy).
#
# All values overridable via env vars; defaults match this project's
# `deploy/helm/office-convert/values.yaml` ingress block.
set -euo pipefail

NAMESPACE=${NAMESPACE:-office-convert-dev}
HOSTED_ZONE_ID=${HOSTED_ZONE_ID:-Z045669519R5D9D8CKC79}
UI_HOST=${UI_HOST:-office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com}
API_HOST=${API_HOST:-office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com}
UI_INGRESS=${UI_INGRESS:-office-convert-ui}
# Static AWS-published hosted zone ID for ALBs in eu-west-1.
# Source: https://docs.aws.amazon.com/general/latest/gr/elb.html
ALB_ZONE_ID=${ALB_ZONE_ID:-Z32O12XQLNTSW2}
WAIT_SECONDS=${WAIT_SECONDS:-180}

aws_profile_flag=()
if [ -n "${AWS_PROFILE:-}" ]; then
  aws_profile_flag=(--profile "$AWS_PROFILE")
fi

echo "[route53-upsert] Waiting up to ${WAIT_SECONDS}s for Ingress ${UI_INGRESS}/${NAMESPACE} to have an ALB hostname..."

ALB_HOST=""
for _ in $(seq 1 "$WAIT_SECONDS"); do
  ALB_HOST=$(kubectl -n "$NAMESPACE" get ingress "$UI_INGRESS" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  if [ -n "$ALB_HOST" ]; then
    break
  fi
  sleep 1
done

if [ -z "$ALB_HOST" ]; then
  echo "[route53-upsert] ERROR: Ingress ${UI_INGRESS} has no ALB hostname after ${WAIT_SECONDS}s." >&2
  echo "[route53-upsert] Diagnostics:" >&2
  kubectl -n "$NAMESPACE" describe ingress "$UI_INGRESS" >&2 || true
  exit 1
fi

echo "[route53-upsert] ALB hostname: $ALB_HOST"

CHANGE_BATCH=$(cat <<EOF
{
  "Comment": "office-convert ALB Ingress UPSERT $(date -Iseconds)",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${UI_HOST}.",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "${ALB_ZONE_ID}",
          "DNSName": "${ALB_HOST}",
          "EvaluateTargetHealth": true
        }
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${API_HOST}.",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "${ALB_ZONE_ID}",
          "DNSName": "${ALB_HOST}",
          "EvaluateTargetHealth": true
        }
      }
    }
  ]
}
EOF
)

CHANGE_ID=$(aws route53 change-resource-record-sets \
  "${aws_profile_flag[@]}" \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "$CHANGE_BATCH" \
  --query 'ChangeInfo.Id' \
  --output text)

echo "[route53-upsert] Submitted change: $CHANGE_ID"
echo "[route53-upsert] DNS records (UPSERT'd, propagation ~60 s):"
echo "  UI:  https://${UI_HOST}"
echo "  API: https://${API_HOST}"
