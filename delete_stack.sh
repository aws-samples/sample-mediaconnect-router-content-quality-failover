#!/usr/bin/env bash
#
# Empty the stack-created S3 buckets and delete the stack.
#
# Usage:
#   ./delete_stack.sh <STACK_NAME> [AWS_PROFILE]
#
# The artifacts bucket passed to deploy.sh is not deleted because it might hold
# other deployment artifacts. Remove it manually if it is no longer needed.
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <STACK_NAME> [AWS_PROFILE]" >&2
  exit 1
fi

STACK_NAME="$1"
AWS_PROFILE_NAME="${2:-}"
if [[ -n "${AWS_PROFILE_NAME}" ]]; then
  export AWS_PROFILE="${AWS_PROFILE_NAME}"
fi

empty_bucket() {
  local bucket="$1"
  local payload errors
  echo "    Emptying s3://${bucket}, including all object versions and delete markers"
  while :; do
    payload="$(aws s3api list-object-versions \
      --bucket "${bucket}" \
      --no-paginate \
      --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}' \
      --output json)"
    if echo "${payload}" | grep -Eq '"Objects": (null|\[\])'; then
      break
    fi
    errors="$(aws s3api delete-objects \
      --bucket "${bucket}" \
      --delete "${payload}" \
      --query 'Errors' \
      --output json)"
    if [[ "${errors}" != "null" ]]; then
      echo "    Failed to delete some objects from s3://${bucket}:" >&2
      echo "${errors}" >&2
      return 1
    fi
  done
}

echo "==> Looking up the S3 buckets created by the stack"
for OUTPUT_KEY in EvidenceBucketName AccessLogBucketName; do
  BUCKET="$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${OUTPUT_KEY}'].OutputValue" \
    --output text 2>/dev/null || true)"

  if [[ -n "${BUCKET}" && "${BUCKET}" != "None" ]]; then
    if aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
      empty_bucket "${BUCKET}"
    else
      echo "    s3://${BUCKET} not found or not accessible; skipping"
    fi
  else
    echo "    No ${OUTPUT_KEY} output found; skipping"
  fi
done

echo "==> Deleting stack '${STACK_NAME}'"
aws cloudformation delete-stack --stack-name "${STACK_NAME}"

echo "==> Waiting for stack deletion to complete"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}"
echo "==> Done"
