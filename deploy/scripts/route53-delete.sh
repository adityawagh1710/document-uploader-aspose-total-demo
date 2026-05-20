#!/usr/bin/env bash
# Delete the A-alias records for the UI + API hostnames. Idempotent — skips
# silently if a record doesn't exist.
#
# Must run BEFORE `helm uninstall` tears down the Ingress, because Route 53
# DELETE requires the exact current ResourceRecordSet (including the ALB
# DNSName in AliasTarget). After the Ingress is gone the ALB hostname is
# already torn down, but the record itself still resolves to that gone
# hostname — and we still have to send its current DNSName in the DELETE
# payload. So look up the record FIRST.
set -euo pipefail

HOSTED_ZONE_ID=${HOSTED_ZONE_ID:-Z045669519R5D9D8CKC79}
UI_HOST=${UI_HOST:-office-convert-dev-sandbox-v1.dev05.k8s.opus2dev.com}
API_HOST=${API_HOST:-office-convert-api-dev-sandbox-v1.dev05.k8s.opus2dev.com}
ALB_ZONE_ID=${ALB_ZONE_ID:-Z32O12XQLNTSW2}

aws_profile_flag=()
if [ -n "${AWS_PROFILE:-}" ]; then
  aws_profile_flag=(--profile "$AWS_PROFILE")
fi

delete_alias() {
  local host="$1"
  local alb_dns
  alb_dns=$(aws route53 list-resource-record-sets \
    "${aws_profile_flag[@]}" \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --query "ResourceRecordSets[?Name=='${host}.' && Type=='A'].AliasTarget.DNSName | [0]" \
    --output text 2>/dev/null || echo "None")

  if [ "$alb_dns" = "None" ] || [ -z "$alb_dns" ]; then
    echo "[route53-delete] $host: no A record found, skipping"
    return 0
  fi

  local change_batch
  change_batch=$(cat <<EOF
{
  "Comment": "office-convert ALB Ingress DELETE $(date -Iseconds)",
  "Changes": [
    {
      "Action": "DELETE",
      "ResourceRecordSet": {
        "Name": "${host}.",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "${ALB_ZONE_ID}",
          "DNSName": "${alb_dns}",
          "EvaluateTargetHealth": true
        }
      }
    }
  ]
}
EOF
)

  local change_id
  change_id=$(aws route53 change-resource-record-sets \
    "${aws_profile_flag[@]}" \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --change-batch "$change_batch" \
    --query 'ChangeInfo.Id' \
    --output text)
  echo "[route53-delete] $host: deleted (change $change_id)"
}

delete_alias "$UI_HOST"
delete_alias "$API_HOST"
