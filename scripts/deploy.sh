#!/usr/bin/env bash
set -euo pipefail

# Resource values can be set directly by CloudFormation UserData (preferred during initial deploy)
# or fetched from stack Outputs for manual re-deployment.
: "${AWS_REGION:?ERROR: AWS_REGION environment variable is not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="$SCRIPT_DIR/../k8s"

# V1FS Helm release names — must match the names used in helm install
V1FS_RELEASE_NAME="${V1FS_RELEASE_NAME:-my-release}"
REVIEW_V1FS_RELEASE_NAME="${REVIEW_V1FS_RELEASE_NAME:-rv}"

DEPLOY_REVIEW=false
for arg in "$@"; do
  case "$arg" in
    --review) DEPLOY_REVIEW=true ;;
  esac
done

# If any resource env var is missing, fall back to reading stack Outputs
if [ -z "${SQS_QUEUE_URL:-}" ] || [ -z "${S3_INGEST_BUCKET:-}" ] || \
   [ -z "${S3_CLEAN_BUCKET:-}" ] || [ -z "${S3_QUARANTINE_BUCKET:-}" ] || \
   [ -z "${S3_REVIEW_BUCKET:-}" ] || \
   [ -z "${V1FS_API_KEY_SECRET_ARN:-}" ] || [ -z "${ECR_REPO_URL:-}" ] || \
   [ -z "${AUDIT_LOG_GROUP:-}" ]; then

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
  S3_REVIEW_BUCKET="${S3_REVIEW_BUCKET:-$(get_output ReviewBucketName)}"
  V1FS_API_KEY_SECRET_ARN="${V1FS_API_KEY_SECRET_ARN:-$(get_output ApiKeySecretArn)}"
  ECR_REPO_URL="${ECR_REPO_URL:-$(get_output ECRRepoUrl)}"
  AUDIT_LOG_GROUP="${AUDIT_LOG_GROUP:-$(get_output ScanAuditLogGroupName)}"
  if [ "$DEPLOY_REVIEW" = "true" ]; then
    REVIEW_SQS_QUEUE_URL="${REVIEW_SQS_QUEUE_URL:-$(get_output ReviewScanQueueUrl)}"
    REVIEW_AUDIT_LOG_GROUP="${REVIEW_AUDIT_LOG_GROUP:-$(get_output ReviewAuditLogGroupName)}"
  fi
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
echo "Review:    $S3_REVIEW_BUCKET"
echo "ECR:       $ECR_REPO_URL"
echo "Audit log: ${AUDIT_LOG_GROUP:-disabled}"
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
  S3_REVIEW_BUCKET: "$S3_REVIEW_BUCKET"
  V1FS_SERVER_ADDR: "${V1FS_RELEASE_NAME}-visionone-filesecurity-scanner:50051"
  V1FS_API_KEY_SECRET_ARN: "$V1FS_API_KEY_SECRET_ARN"
  AWS_REGION: "$AWS_REGION"
  LOG_LEVEL: "${LOG_LEVEL:-INFO}"
  MAX_CONCURRENT_SCANS: "50"
  MAX_FILE_SIZE_MB: "${MAX_FILE_SIZE_MB:-500}"
  TM_AM_SCAN_TIMEOUT_SECS: "${TM_AM_SCAN_TIMEOUT_SECS:-600}"
  PML_ENABLED: "${PML_ENABLED:-false}"
  AUDIT_LOG_GROUP: "${AUDIT_LOG_GROUP:-}"
EOF

echo "Applying Deployment..."
sed -e "s|<ECR_REPO_URL>|${ECR_REPO_URL}|g" \
    -e "s|<IMAGE_TAG>|${IMAGE_TAG}|g" \
    "$K8S_DIR/deployment.yaml" | kubectl apply -f -

echo "Applying PodDisruptionBudgets..."
kubectl apply -f "$K8S_DIR/pdb.yaml"

echo "Applying KEDA ScaledObject..."
sed -e "s|<SQS_QUEUE_URL>|${SQS_QUEUE_URL}|g" \
    -e "s|<AWS_REGION>|${AWS_REGION}|g" \
    "$K8S_DIR/scaledobject.yaml" | kubectl apply -f -

echo "Waiting for rollout (Karpenter may need to provision a node first)..."
if kubectl rollout status deployment/scanner-app -n visionone-filesecurity --timeout=300s; then
  echo "Deploy complete. Scanner-app is running."
else
  echo "WARNING: Rollout not yet complete after 300s. This is expected on first deploy while Karpenter provisions a node. The pod will start once the node is ready."
fi
kubectl get pods -n visionone-filesecurity -l app=scanner-app

# --- Review Scanner Pipeline (optional) ---
if [ "$DEPLOY_REVIEW" = "true" ]; then
  echo ""
  echo "=== Deploying Review Scanner Pipeline ==="

  kubectl create namespace visionone-review 2>/dev/null || true

  echo "Applying review-scanner ServiceAccount..."
  kubectl apply -f "$K8S_DIR/review-serviceaccount.yaml"

  echo "Applying review-scanner NetworkPolicy..."
  kubectl apply -f "$K8S_DIR/review-networkpolicy.yaml"

  echo "Generating and applying review-scanner ConfigMap..."
  cat <<REOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: review-scanner-app-config
  namespace: visionone-review
data:
  SQS_QUEUE_URL: "$REVIEW_SQS_QUEUE_URL"
  S3_INGEST_BUCKET: "$S3_REVIEW_BUCKET"
  S3_CLEAN_BUCKET: "$S3_CLEAN_BUCKET"
  S3_QUARANTINE_BUCKET: "$S3_QUARANTINE_BUCKET"
  S3_REVIEW_BUCKET: ""
  V1FS_SERVER_ADDR: "${REVIEW_V1FS_RELEASE_NAME}-visionone-filesecurity-scanner:50051"
  V1FS_API_KEY_SECRET_ARN: "$V1FS_API_KEY_SECRET_ARN"
  AWS_REGION: "$AWS_REGION"
  LOG_LEVEL: "${LOG_LEVEL:-INFO}"
  MAX_CONCURRENT_SCANS: "50"
  MAX_FILE_SIZE_MB: "0"
  TM_AM_SCAN_TIMEOUT_SECS: "${TM_AM_SCAN_TIMEOUT_SECS:-600}"
  PML_ENABLED: "${PML_ENABLED:-false}"
  AUDIT_LOG_GROUP: "${REVIEW_AUDIT_LOG_GROUP:-}"
  REVIEW_ROUTING_ENABLED: "false"
REOF

  echo "Applying review-scanner Deployment..."
  sed -e "s|<ECR_REPO_URL>|${ECR_REPO_URL}|g" \
      -e "s|<IMAGE_TAG>|${IMAGE_TAG}|g" \
      "$K8S_DIR/review-deployment.yaml" | kubectl apply -f -

  echo "Applying review-scanner KEDA ScaledObject..."
  sed -e "s|<SQS_QUEUE_URL>|${REVIEW_SQS_QUEUE_URL}|g" \
      -e "s|<AWS_REGION>|${AWS_REGION}|g" \
      "$K8S_DIR/review-scaledobject.yaml" | kubectl apply -f -

  echo "Waiting for review-scanner rollout..."
  if kubectl rollout status deployment/review-scanner-app -n visionone-review --timeout=300s; then
    echo "Review scanner deploy complete."
  else
    echo "WARNING: Review scanner rollout not yet complete after 300s."
  fi
  kubectl get pods -n visionone-review -l app=review-scanner-app
fi
