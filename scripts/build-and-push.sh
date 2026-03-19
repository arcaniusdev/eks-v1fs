#!/usr/bin/env bash
set -euo pipefail

# Expected environment variables (set by CloudFormation UserData via Fn::Sub):
#   CFN_STACK_NAME  — CloudFormation stack name
#   AWS_REGION      — AWS region
: "${CFN_STACK_NAME:?ERROR: CFN_STACK_NAME environment variable is not set}"
: "${AWS_REGION:?ERROR: AWS_REGION environment variable is not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/../app"

echo "Fetching ECR repo URL from stack: $CFN_STACK_NAME"
ECR_REPO_URL=$(aws cloudformation describe-stacks \
  --stack-name "$CFN_STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ECRRepoUrl`].OutputValue' \
  --output text)

if [ -z "$ECR_REPO_URL" ]; then
  echo "ERROR: Could not find ECRRepoUrl output in stack $CFN_STACK_NAME" >&2
  exit 1
fi

echo "ECR repo: $ECR_REPO_URL"

echo "Authenticating to ECR..."
ACCOUNT_ID="$(echo "$ECR_REPO_URL" | cut -d. -f1)"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "Building image..."
docker build -t scanner-app "$APP_DIR"

echo "Tagging and pushing to $ECR_REPO_URL..."
docker tag scanner-app:latest "${ECR_REPO_URL}:latest"
docker push "${ECR_REPO_URL}:latest"

echo "Done. Image pushed to ${ECR_REPO_URL}:latest"
