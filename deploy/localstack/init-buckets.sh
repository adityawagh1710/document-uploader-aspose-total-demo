#!/bin/bash
# LocalStack init hook — runs once S3 is ready (mounted into
# /etc/localstack/init/ready.d/). Creates the dev buckets the office-convert
# S3 integration expects. See compose.yaml and
# aidlc-docs/construction/plans/s3-source-integration-plan.md.
set -e
awslocal s3 mb s3://office-convert-in || true
awslocal s3 mb s3://office-convert-out || true
echo "office-convert: localstack buckets ready (office-convert-in, office-convert-out)"
