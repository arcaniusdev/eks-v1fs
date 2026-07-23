#!/usr/bin/env bash
set -euo pipefail

# ECR_REPO_URL can be set directly by CloudFormation UserData (preferred during initial deploy)
# or fetched from stack Outputs for manual re-deployment.
: "${AWS_REGION:?ERROR: AWS_REGION environment variable is not set}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Scanner-app flavor: python (app/) or java (app-java/). Both produce an image
# named scanner-app:latest with an identical Dockerfile contract (non-root,
# read-only-fs compatible, reads the same env vars), so everything downstream
# (tag, push, deploy) is flavor-agnostic. The java build is a multi-stage
# Maven+JRE image; under QEMU cross-build (x86 bastion → ARM node) its Maven
# stage runs emulated, so expect it to take longer than the Python build.
SCANNER_APP_FLAVOR="${SCANNER_APP_FLAVOR:-python}"
case "$SCANNER_APP_FLAVOR" in
  python) APP_DIR="$SCRIPT_DIR/../app" ;;
  java)   APP_DIR="$SCRIPT_DIR/../app-java" ;;
  *) echo "ERROR: unknown SCANNER_APP_FLAVOR=$SCANNER_APP_FLAVOR (want python|java)" >&2; exit 1 ;;
esac
echo "Scanner-app flavor: $SCANNER_APP_FLAVOR (build context: $APP_DIR)"

if [ -z "${ECR_REPO_URL:-}" ]; then
  : "${CFN_STACK_NAME:?ERROR: Either ECR_REPO_URL or CFN_STACK_NAME must be set}"
  echo "Fetching ECR repo URL from stack: $CFN_STACK_NAME"
  ECR_REPO_URL=$(aws cloudformation describe-stacks \
    --stack-name "$CFN_STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`ECRRepoUrl`].OutputValue' \
    --output text)
  if [ -z "$ECR_REPO_URL" ] || [ "$ECR_REPO_URL" = "None" ]; then
    echo "ERROR: Could not find ECRRepoUrl output in stack $CFN_STACK_NAME" >&2
    exit 1
  fi
fi

# Determine image tag: git SHA (immutable, never :latest)
if command -v git >/dev/null 2>&1 && git -C "$SCRIPT_DIR" rev-parse HEAD >/dev/null 2>&1; then
  IMAGE_TAG=$(git -C "$SCRIPT_DIR" rev-parse --short=12 HEAD)
else
  echo "ERROR: git not available or not a git repo — cannot determine image tag" >&2
  echo "Image tags must be immutable git SHAs, not :latest" >&2
  exit 1
fi

echo "ECR repo: $ECR_REPO_URL"
echo "Image tag: $IMAGE_TAG"

echo "Authenticating to ECR..."
ACCOUNT_ID="$(echo "$ECR_REPO_URL" | cut -d. -f1)"
if ! aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" 2>/dev/null; then
  echo "ERROR: ECR authentication failed. Check AWS credentials and region." >&2
  exit 1
fi

# Build for the node group's CPU architecture (TARGET_ARCH set by UserData;
# defaults to amd64 for manual runs). Cross-arch builds need QEMU binfmt.
TARGET_ARCH="${TARGET_ARCH:-amd64}"
NATIVE_ARCH="$(uname -m)"
case "$NATIVE_ARCH" in
  x86_64) NATIVE_ARCH=amd64 ;;
  aarch64|arm64) NATIVE_ARCH=arm64 ;;
esac
if [ "$TARGET_ARCH" != "$NATIVE_ARCH" ]; then
  echo "Cross-building for $TARGET_ARCH on $NATIVE_ARCH — installing QEMU binfmt..."
  docker run --privileged --rm tonistiigi/binfmt --install "$TARGET_ARCH"
fi

echo "Building image (linux/$TARGET_ARCH)..."
docker build --platform "linux/$TARGET_ARCH" -t scanner-app "$APP_DIR"

echo "Tagging and pushing to $ECR_REPO_URL..."
docker tag scanner-app:latest "${ECR_REPO_URL}:${IMAGE_TAG}"
docker push "${ECR_REPO_URL}:${IMAGE_TAG}"

# Export for deploy.sh to consume
export IMAGE_TAG
echo "Done. Image pushed as ${ECR_REPO_URL}:${IMAGE_TAG}"
