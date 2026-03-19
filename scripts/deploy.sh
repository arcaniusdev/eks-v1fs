#!/usr/bin/env bash
set -euo pipefail

# Expected environment variables (set by CloudFormation UserData via Fn::Sub):
#   CFN_STACK_NAME  — CloudFormation stack name
#   AWS_REGION      — AWS region
: "${CFN_STACK_NAME:?ERROR: CFN_STACK_NAME environment variable is not set}"
: "${AWS_REGION:?ERROR: AWS_REGION environment variable is not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
K8S_DIR="$SCRIPT_DIR/../k8s"

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
    if o['OutputKey'] == '$1':
        print(o['OutputValue'])
        break
"
}

SQS_QUEUE_URL="$(get_output FileScanQueueUrl)"
S3_INGEST_BUCKET="$(get_output IngestBucketName)"
S3_CLEAN_BUCKET="$(get_output CleanBucketName)"
S3_QUARANTINE_BUCKET="$(get_output QuarantineBucketName)"
API_KEY_SECRET_ARN="$(get_output ApiKeySecretArn)"
ECR_REPO_URL="$(get_output ECRRepoUrl)"

echo "ECR repo: $ECR_REPO_URL"

echo "Applying ServiceAccount..."
kubectl apply -f "$K8S_DIR/serviceaccount.yaml"

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
  V1FS_API_KEY_SECRET_ARN: "$API_KEY_SECRET_ARN"
  AWS_REGION: "$AWS_REGION"
  LOG_LEVEL: "INFO"
  MAX_CONCURRENT_SCANS: "20"
EOF

echo "Applying Deployment..."
sed "s|<ECR_REPO_URL>|${ECR_REPO_URL}|g" "$K8S_DIR/deployment.yaml" | kubectl apply -f -

echo "Applying KEDA ScaledObject..."
sed -e "s|<SQS_QUEUE_URL>|${SQS_QUEUE_URL}|g" \
    -e "s|<AWS_REGION>|${AWS_REGION}|g" \
    "$K8S_DIR/scaledobject.yaml" | kubectl apply -f -

echo "Waiting for rollout..."
kubectl rollout status deployment/scanner-app -n visionone-filesecurity --timeout=120s

echo "Deploy complete."
kubectl get pods -n visionone-filesecurity -l app=scanner-app
