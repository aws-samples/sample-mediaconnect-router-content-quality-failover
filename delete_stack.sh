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

# Empty a bucket completely, including all object versions and delete markers.
# Both stack-created buckets have versioning enabled, so 'aws s3 rm --recursive'
# alone leaves noncurrent versions and delete markers that block deletion.
empty_bucket() {
  local bucket="$1"
  echo "    Emptying s3://${bucket} (buckets must be empty before deletion)"
  aws s3 rm "s3://${bucket}" --recursive >/dev/null 2>&1 || true
  while :; do
    local payload
    payload="$(aws s3api list-object-versions \
      --bucket "${bucket}" \
      --max-items 1000 \
      --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}}' \
      --output json 2>/dev/null || echo '{"Objects": null}')"
    if echo "${payload}" | grep -Eq '"Objects": (null|\[\])'; then
      break
    fi
    aws s3api delete-objects --bucket "${bucket}" --delete "${payload}" >/dev/null 2>&1 || break
  done
}

echo "==> Looking up the S3 buckets created by the stack"
for OUTPUT_KEY in EvidenceBucketName AccessLogBucketName; do
  BUCKET="$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${OUTPUT_KEY}'].OutputValue" \
    --output text 2>/dev/null || true)"

  if [[ -n "${BUCKET}" && "${BUCKET}" != "None" ]]; then
    empty_bucket "${BUCKET}"
  else
    echo "    No ${OUTPUT_KEY} output found; skipping"
  fi
done

echo "==> Deleting stack '${STACK_NAME}'"
aws cloudformation delete-stack --stack-name "${STACK_NAME}"

echo "==> Waiting for stack deletion to complete"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}"
echo "==> Done"
