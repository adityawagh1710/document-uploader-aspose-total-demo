#!/usr/bin/env bash
# Apply a standard resource-tag set to the dev05 out-of-band resources that
# Helm cannot manage: the S3 input/output buckets, the IRSA role, and the two
# ECR repos. Mirrors the tag schema used across the org (see
# classification-service/deploy/scripts/tag-resources.sh). Tagging only —
# idempotent, no data/security impact. Run after the resources exist.
#
#   make tag-resources                       # uses the Makefile defaults
#   bash deploy/scripts/tag-resources.sh     # uses env vars / the defaults below
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-opus2-dev}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-537462380503}"
S3_INPUT_BUCKET="${S3_INPUT_BUCKET:-office-convert-dev-sandbox-input}"
S3_OUTPUT_BUCKET="${S3_OUTPUT_BUCKET:-office-convert-dev-sandbox-output}"
IRSA_ROLE_NAME="${IRSA_ROLE_NAME:-office-convert-dev-s3}"
ECR_REPO="${ECR_REPO:-office-convert}"
ECR_REPO_UI="${ECR_REPO_UI:-office-convert-ui}"

# Standard tag set (Service=office-convert; mirrors the org schema).
OWNER="${DEPLOY_TAG_OWNER:-platform-team}"
COSTCENTER="${DEPLOY_TAG_COSTCENTER:-tbd}"
SERVICE="${DEPLOY_TAG_SERVICE:-office-convert}"
ENVIRONMENT="${DEPLOY_TAG_ENV:-dev}"
COMPONENT="${DEPLOY_TAG_COMPONENT:-convert}"
MANAGEDBY="${DEPLOY_TAG_MANAGEDBY:-manual-dev05}"

# IAM / ECR shorthand: space-separated Key=,Value= pairs
PAIRS="Key=Owner,Value=$OWNER Key=CostCenter,Value=$COSTCENTER Key=Service,Value=$SERVICE Key=Environment,Value=$ENVIRONMENT Key=Component,Value=$COMPONENT Key=ManagedBy,Value=$MANAGEDBY"
# S3 TagSet shorthand: single bracketed list, no spaces
S3_TAGSET="TagSet=[{Key=Owner,Value=$OWNER},{Key=CostCenter,Value=$COSTCENTER},{Key=Service,Value=$SERVICE},{Key=Environment,Value=$ENVIRONMENT},{Key=Component,Value=$COMPONENT},{Key=ManagedBy,Value=$MANAGEDBY}]"

echo "Tagging office-convert dev05 out-of-band resources (Service=$SERVICE Environment=$ENVIRONMENT ManagedBy=$MANAGEDBY)"

for B in "$S3_INPUT_BUCKET" "$S3_OUTPUT_BUCKET"; do
  aws s3api put-bucket-tagging --bucket "$B" --tagging "$S3_TAGSET" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION"
  echo "  ✓ s3://$B"
done

# shellcheck disable=SC2086  # $PAIRS is intentionally word-split into args
aws iam tag-role --role-name "$IRSA_ROLE_NAME" --tags $PAIRS --profile "$AWS_PROFILE"
echo "  ✓ iam role $IRSA_ROLE_NAME"

for R in "$ECR_REPO" "$ECR_REPO_UI"; do
  # shellcheck disable=SC2086
  aws ecr tag-resource \
    --resource-arn "arn:aws:ecr:$AWS_REGION:$AWS_ACCOUNT_ID:repository/$R" \
    --tags $PAIRS --profile "$AWS_PROFILE" --region "$AWS_REGION" 2>/dev/null \
    && echo "  ✓ ecr $R" || echo "  ! ecr $R not found (skipped)"
done

echo "Verify:"
echo "  aws s3api get-bucket-tagging --bucket $S3_OUTPUT_BUCKET --profile $AWS_PROFILE --region $AWS_REGION"
echo "  aws iam list-role-tags --role-name $IRSA_ROLE_NAME --profile $AWS_PROFILE"
