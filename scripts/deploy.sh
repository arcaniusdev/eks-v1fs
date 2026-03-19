#!/usr/bin/env bash
set -euo pipefail

# Resource values can be set directly by CloudFormation UserData (preferred during initial deploy)
# or fetched from stack Outputs for manual re-deployment.
: "${AWS_REGION:?ERROR: AWS_REGION environment variable is not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="$SCRIPT_DIR/../k8s"

# If any resource env var is missing, fall back to reading stack Outputs
if [ -z "${SQS_QUEUE_URL:-}" ] || [ -z "${S3_INGEST_BUCKET:-}" ] || \
   [ -z "${S3_CLEAN_BUCKET:-}" ] || [ -z "${S3_QUARANTINE_BUCKET:-}" ] || \
   [ -z "${V1FS_API_KEY_SECRET_ARN:-}" ] || [ -z "${ECR_REPO_URL:-}" ]; then

  : "${CFN_STACK_NAME:?ERROR: Either set all resource env vars or set CFN_STACK_NAME}"
  echo "Fetching CloudFormation outputs for stack: $CFN_STACK_NAME"
  OUTPUTS=$(aws cloudformation describe-stacks \
    --stack-name "$CFN_STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].Outputs' \
    --output json)

  get_output() {
    echo "$OUTPUTS" | python3 -c "
import json, sys
outputs = json.load(sys.stdin)
for o in outputs:
    if o['OutputKey'] == sys.argv[1]:
        print(o['OutputValue'])
        break
" "$1"
  }

  SQS_QUEUE_URL="${SQS_QUEUE_URL:-$(get_output FileScanQueueUrl)}"
  S3_INGEST_BUCKET="${S3_INGEST_BUCKET:-$(get_output IngestBucketName)}"
  S3_CLEAN_BUCKET="${S3_CLEAN_BUCKET:-$(get_output CleanBucketName)}"
  S3_QUARANTINE_BUCKET="${S3_QUARANTINE_BUCKET:-$(get_output QuarantineBucketName)}"
  V1FS_API_KEY_SECRET_ARN="${V1FS_API_KEY_SECRET_ARN:-$(get_output ApiKeySecretArn)}"
  ECR_REPO_URL="${ECR_REPO_URL:-$(get_output ECRRepoUrl)}"
fi

# Determine image tag: use IMAGE_TAG env var, git SHA, or "latest"
if [ -n "${IMAGE_TAG:-}" ]; then
  : # already set
elif command -v git >/dev/null 2>&1 && git -C "$SCRIPT_DIR" rev-parse HEAD >/dev/null 2>&1; then
  IMAGE_TAG=$(git -C "$SCRIPT_DIR" rev-parse --short=12 HEAD)
else
  IMAGE_TAG="latest"
fi

echo "SQS Queue: $SQS_QUEUE_URL"
echo "Ingest:    $S3_INGEST_BUCKET"
echo "Clean:     $S3_CLEAN_BUCKET"
echo "Quarantine:$S3_QUARANTINE_BUCKET"
echo "ECR:       $ECR_REPO_URL"
echo "Image tag: $IMAGE_TAG"

echo "Applying ServiceAccount..."
kubectl apply -f "$K8S_DIR/serviceaccount.yaml"

echo "Applying NetworkPolicy..."
kubectl apply -f "$K8S_DIR/networkpolicy.yaml"

echo "Generating and applying ConfigMap..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: scanner-app-config
  namespace: visionone-filesecurity
data:
  SQS_QUEUE_URL: "$SQS_QUEUE_URL"
  S3_INGEST_BUCKET: "$S3_INGEST_BUCKET"
  S3_QUARANTINE_BUCKET: "$S3_QUARANTINE_BUCKET"
  S3_CLEAN_BUCKET: "$S3_CLEAN_BUCKET"
  V1FS_SERVER_ADDR: "my-release-visionone-filesecurity-scanner:50051"
  V1FS_API_KEY_SECRET_ARN: "$V1FS_API_KEY_SECRET_ARN"
  AWS_REGION: "$AWS_REGION"
  LOG_LEVEL: "INFO"
  MAX_CONCURRENT_SCANS: "20"
  PML_ENABLED: "${PML_ENABLED:-false}"
EOF

echo "Applying Deployment..."
sed -e "s|<ECR_REPO_URL>|${ECR_REPO_URL}|g" \
    -e "s|<IMAGE_TAG>|${IMAGE_TAG}|g" \
    "$K8S_DIR/deployment.yaml" | kubectl apply -f -

echo "Applying KEDA ScaledObject..."
sed -e "s|<SQS_QUEUE_URL>|${SQS_QUEUE_URL}|g" \
    -e "s|<AWS_REGION>|${AWS_REGION}|g" \
    "$K8S_DIR/scaledobject.yaml" | kubectl apply -f -

echo "Waiting for rollout..."
kubectl rollout status deployment/scanner-app -n visionone-filesecurity --timeout=120s

echo "Deploy complete."
kubectl get pods -n visionone-filesecurity -l app=scanner-app
