#!/usr/bin/env bash
#
# Package the AWS Lambda function, upload it to Amazon S3, and deploy the stack.
#
# Usage:
#   ./deploy.sh <ARTIFACTS_BUCKET> <STACK_NAME> <SOURCE_CIDR> <NOTIFICATION_EMAIL> [FROZEN_FRAME_THRESHOLD] [AWS_PROFILE]
#
#   ARTIFACTS_BUCKET        S3 bucket used to stage the packaged Lambda code.
#                           Created automatically if it does not already exist.
#   STACK_NAME              Name of the CloudFormation stack to create or update.
#   SOURCE_CIDR             Public CIDR block allowed to send video to the
#                           MediaConnect Router input, for example 203.0.113.0/24.
#   NOTIFICATION_EMAIL      Email address that receives failover notifications.
#   FROZEN_FRAME_THRESHOLD  Optional seconds before a frozen-frame alert fires.
#                           Defaults to the template default of 30 seconds.
#   AWS_PROFILE             Optional named AWS CLI profile. The profile must have
#                           a default Region configured.
#
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "Usage: $0 <ARTIFACTS_BUCKET> <STACK_NAME> <SOURCE_CIDR> <NOTIFICATION_EMAIL> [FROZEN_FRAME_THRESHOLD] [AWS_PROFILE]" >&2
  exit 1
fi

ARTIFACTS_BUCKET="$1"
STACK_NAME="$2"
SOURCE_CIDR="$3"
NOTIFICATION_EMAIL="$4"
THRESHOLD="${5:-}"
AWS_PROFILE_NAME="${6:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/template.yaml"
PACKAGED="${SCRIPT_DIR}/packaged.yaml"

# Use a named profile through the environment so every AWS CLI call inherits it.
if [[ -n "${AWS_PROFILE_NAME}" ]]; then
  export AWS_PROFILE="${AWS_PROFILE_NAME}"
fi

echo "==> Ensuring artifacts bucket '${ARTIFACTS_BUCKET}' exists"
if ! aws s3api head-bucket --bucket "${ARTIFACTS_BUCKET}" 2>/dev/null; then
  echo "    Bucket not found; creating it"
  aws s3 mb "s3://${ARTIFACTS_BUCKET}"
fi

echo "==> Packaging Lambda code and uploading it to S3"
aws cloudformation package \
  --template-file "${TEMPLATE}" \
  --s3-bucket "${ARTIFACTS_BUCKET}" \
  --output-template-file "${PACKAGED}"

echo "==> Deploying stack '${STACK_NAME}'"
PARAM_OVERRIDES=(
  "SourceCidr=${SOURCE_CIDR}"
  "NotificationEmail=${NOTIFICATION_EMAIL}"
)
if [[ -n "${THRESHOLD}" ]]; then
  PARAM_OVERRIDES+=("FrozenFrameThreshold=${THRESHOLD}")
fi
aws cloudformation deploy \
  --template-file "${PACKAGED}" \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "${PARAM_OVERRIDES[@]}"

echo "==> Stack outputs"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query 'Stacks[0].Outputs' \
  --output table

echo "==> Confirm the SNS subscription sent to ${NOTIFICATION_EMAIL}"
