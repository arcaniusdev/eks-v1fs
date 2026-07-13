#!/bin/bash -x
# ----------------------------------------------------------------------
# Bastion provisioning — invoked by CloudFormation UserData after the
# repo is cloned. UserData exports all CFN-derived environment variables
# (see the Bastion resource in eks-v1fs.yaml), then runs this script.
# Runs under `set -e` inherited expectations: any failure propagates to
# UserData, which signals FAILURE to CloudFormation.
#
# Deployment modes (all driven by environment variables):
#   DEPLOY_SCANNER_APP=true|false   scanning application module
#   DEPLOY_REVIEW=true|false        review pipeline (requires scanner app)
#   ENDPOINT_MODE=none|nlb|alb      external gRPC endpoint exposure
#   EXISTING_INGEST_BUCKET=<name>   existing-customer-bucket mode ("" = created)
# ----------------------------------------------------------------------
set -e

V1FS_CHART_VERSION="${V1FS_CHART_VERSION:-1.4.10}"

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$PATH
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Ensure /usr/local/bin is on PATH and KUBECONFIG is set for SSH sessions
echo 'export PATH=/usr/local/bin:$PATH' > /etc/profile.d/local-bin.sh
echo 'export KUBECONFIG=/root/.kube/config' >> /etc/profile.d/local-bin.sh
chmod 644 /etc/profile.d/local-bin.sh

# ---- System packages ----
apt-get update -y
apt-get remove needrestart -y || true
apt-get install -y python3 python3-pip curl gpg apt-transport-https unzip jq

# ---- AWS CLI v2 ----
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install --update
rm -rf /tmp/aws /tmp/awscliv2.zip
aws --version

# ---- Helm ----
curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey | gpg --dearmor | tee /usr/share/keyrings/helm.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" | tee /etc/apt/sources.list.d/helm-stable-debian.list
apt-get update -y
apt-get install -y helm
helm version

# ---- eksctl ----
ARCH=amd64
PLATFORM=$(uname -s)_$ARCH
curl -sLO "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_$PLATFORM.tar.gz"
tar -xzf "eksctl_$PLATFORM.tar.gz" -C /tmp && rm "eksctl_$PLATFORM.tar.gz"
mv /tmp/eksctl /usr/local/bin/eksctl
eksctl version

# ---- kubectl ----
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
rm -f kubectl
kubectl version --client

# ---- Configure kubeconfig ----
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"
export KUBECONFIG=/root/.kube/config

# ---- Create V1FS namespace and secrets ----
kubectl create namespace visionone-filesecurity

# Retrieve registration token from Secrets Manager into a temp file.
# Using --from-file avoids shell interpolation issues with special
# characters in the token (e.g. $, backticks, !, \).
aws secretsmanager get-secret-value \
  --secret-id "$V1FS_REGISTRATION_SECRET" \
  --query SecretString --output text \
  --region "$AWS_REGION" | tr -d '\n' > /tmp/.reg-token
kubectl create secret generic token-secret \
  --from-file=registration-token=/tmp/.reg-token \
  -n visionone-filesecurity
shred -u /tmp/.reg-token

kubectl create secret generic device-token-secret \
  -n visionone-filesecurity

# ---- Install AWS Load Balancer Controller via Helm ----
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName="$CLUSTER_NAME" \
  --set vpcId="$VPC_ID" \
  --set region="$AWS_REGION" \
  --wait --timeout 5m

# ---- Install Metrics Server (required by the chart HPA) ----
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl rollout status deployment/metrics-server -n kube-system --timeout=120s

# ---- Install Cluster Autoscaler via Helm ----
# The standard Kubernetes node autoscaler. Discovers the managed node
# group's ASG via the k8s.io/cluster-autoscaler tags EKS applies
# automatically. IAM via Pod Identity (ClusterAutoscalerRole).
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm repo update autoscaler
helm install cluster-autoscaler autoscaler/cluster-autoscaler \
  -n kube-system \
  --set autoDiscovery.clusterName="$CLUSTER_NAME" \
  --set awsRegion="$AWS_REGION" \
  --set rbac.serviceAccount.name=cluster-autoscaler \
  --set extraArgs.balance-similar-node-groups=true \
  --set extraArgs.expander=least-waste \
  --set extraArgs.scale-down-unneeded-time=2m \
  --wait --timeout 5m

# ---- Install KEDA via Helm (scales only our scanner-app) ----
if [ "$DEPLOY_SCANNER_APP" = "true" ]; then
  helm repo add kedacore https://kedacore.github.io/charts
  helm repo update kedacore
  helm install keda kedacore/keda \
    -n keda --create-namespace \
    --wait --timeout 5m
fi

# ---- Create gp3 StorageClass for V1FS database ----
kubectl wait --for=condition=ready pod -l app=ebs-csi-controller -n kube-system --timeout=300s
cat <<EOFGP3 | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOFGP3

# ---- Create EFS StorageClass for V1FS scanner ephemeral volume ----
kubectl wait --for=condition=ready pod -l app=efs-csi-controller -n kube-system --timeout=300s
cat <<EOFSC | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: $EFS_ID
  directoryPerms: "700"
  uid: "0"
  gid: "0"
  basePath: "/scanner"
mountOptions:
  - tls
EOFSC

# ---- Install Vision One File Security via Helm (GPG-verified) ----
helm repo add visionone-filesecurity https://trendmicro.github.io/visionone-file-security-helm/
helm repo update
curl -o /tmp/public-key.asc https://trendmicro.github.io/visionone-file-security-helm/public-key.asc
gpg --import /tmp/public-key.asc
gpg --export > /root/.gnupg/pubring.gpg

# Verify chart signature (validation only - not used for install)
helm pull --verify visionone-filesecurity/visionone-filesecurity --version "$V1FS_CHART_VERSION"

# Endpoint-mode helm arguments.
# nlb: chart-native externalService as an internal NLB (values-nlb.yaml)
# alb: chart-native ingress with gRPC backend + TLS (Trend's documented topology)
ENDPOINT_ARGS=""
if [ "$ENDPOINT_MODE" = "nlb" ]; then
  ENDPOINT_ARGS="-f $REPO_DIR/helm/values-nlb.yaml"
elif [ "$ENDPOINT_MODE" = "alb" ]; then
  ENDPOINT_ARGS="--set scanner.ingress.enabled=true \
    --set scanner.ingress.className=alb \
    --set scanner.ingress.hosts[0].host=$SCANNER_DOMAIN \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/backend-protocol-version=GRPC \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/target-type=ip \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/scheme=internal \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/certificate-arn=$ACM_CERT_ARN \
    --set-string scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/listen-ports=[{\"HTTPS\":443}]"
fi

# Chart 1.4.10 workaround: the management-service init job's K8s->DB data
# migration requires the ontap-agent-config ConfigMap and crash-loops on a
# fresh install without it (we don't use ONTAP agents). Pre-create it empty.
kubectl create configmap ontap-agent-config -n visionone-filesecurity 2>/dev/null || true

# Install with the chart's own HPA (Trend-supported autoscaling) and the
# repo values file as the single source of truth (no --wait; pods need
# time to register with Vision One cloud on first startup)
# shellcheck disable=SC2086
helm install my-release visionone-filesecurity/visionone-filesecurity \
  -n visionone-filesecurity \
  --version "$V1FS_CHART_VERSION" \
  -f "$REPO_DIR/helm/values-base.yaml" \
  --set scanner.autoscaling.minReplicas="$SCANNER_MIN_REPLICAS" \
  --set scanner.autoscaling.maxReplicas="$SCANNER_MAX_REPLICAS" \
  $ENDPOINT_ARGS

# ---- Configure V1FS scan policy via CLISH ----
# The management service needs to be ready before we can run CLISH.
echo "Waiting for V1FS management service to be ready..."
kubectl rollout status deployment/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity --timeout=180s

echo "Applying scan policy settings via CLISH..."
kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- \
  clish scanner scan-policy modify \
    --max-decompression-layer="$MAX_DECOMPRESSION_LAYER" \
    --max-decompression-file-count="$MAX_DECOMPRESSION_FILE_COUNT" \
    --max-decompression-ratio="$MAX_DECOMPRESSION_RATIO" \
    --max-decompression-size="$MAX_DECOMPRESSION_SIZE"

echo "Verifying scan policy..."
kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- \
  clish scanner scan-policy show

# ---- Review pipeline: second V1FS release (no decompression limits) ----
# Separate namespace because the Helm chart creates shared ServiceAccounts
# that conflict when two releases share a namespace.
if [ "$DEPLOY_REVIEW" = "true" ]; then
  kubectl create namespace visionone-review 2>/dev/null || true
  kubectl create secret generic token-secret \
    --from-literal=registration-token="$(kubectl get secret token-secret -n visionone-filesecurity -o jsonpath='{.data.registration-token}' | base64 -d)" \
    -n visionone-review 2>/dev/null || true
  kubectl create secret generic device-token-secret \
    -n visionone-review 2>/dev/null || true
  # Same chart 1.4.10 workaround as the main release (see above)
  kubectl create configmap ontap-agent-config -n visionone-review 2>/dev/null || true

  echo "Installing rv V1FS scanner (unlimited decompression)..."
  helm install rv visionone-filesecurity/visionone-filesecurity \
    -n visionone-review \
    --version "$V1FS_CHART_VERSION" \
    -f "$REPO_DIR/helm/values-base.yaml" \
    --set scanner.autoscaling.minReplicas=1 \
    --set scanner.autoscaling.maxReplicas=3

  # No CLISH scan-policy for rv — defaults are unlimited.
  # The review pipeline re-scans files that exceeded limits in the main pipeline.
fi

# ---- Scanner application module ----
if [ "$DEPLOY_SCANNER_APP" = "true" ]; then
  # ---- Install Docker (needed to build scanner app image) ----
  apt-get install -y ca-certificates gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io
  systemctl enable docker
  systemctl start docker

  # Existing-customer-bucket mode: never delete the customer's objects —
  # tag them with the verdict instead. Also disables reconciliation
  # (objects legitimately remain in the bucket).
  if [ -n "$EXISTING_INGEST_BUCKET" ]; then
    export DELETE_SOURCE_ENABLED="false"
  else
    export DELETE_SOURCE_ENABLED="true"
  fi
  if [ "$DEPLOY_REVIEW" = "true" ]; then
    export REVIEW_ROUTING_ENABLED="true"
  else
    export REVIEW_ROUTING_ENABLED="false"
  fi

  "$REPO_DIR/scripts/build-and-push.sh"
  "$REPO_DIR/scripts/deploy.sh"

  if [ "$DEPLOY_REVIEW" = "true" ]; then
    "$REPO_DIR/scripts/deploy.sh" --review
  fi
fi

# ---- Publish the scanner endpoint address to SSM ----
if [ "$ENDPOINT_MODE" != "none" ]; then
  echo "Waiting for scanner endpoint hostname..."
  ENDPOINT=""
  for _ in $(seq 1 60); do
    if [ "$ENDPOINT_MODE" = "nlb" ]; then
      HOSTNAME=$(kubectl get svc my-release-visionone-filesecurity-scanner-lb \
        -n visionone-filesecurity \
        -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
      [ -n "$HOSTNAME" ] && ENDPOINT="$HOSTNAME:50051"
    else
      HOSTNAME=$(kubectl get ingress -n visionone-filesecurity \
        -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
      [ -n "$HOSTNAME" ] && ENDPOINT="$SCANNER_DOMAIN:443"
    fi
    [ -n "$ENDPOINT" ] && break
    sleep 10
  done
  if [ -n "$ENDPOINT" ]; then
    aws ssm put-parameter \
      --name "/$CFN_STACK_NAME/scanner-endpoint" \
      --value "$ENDPOINT" \
      --type String --overwrite \
      --region "$AWS_REGION"
    echo "Scanner endpoint published: $ENDPOINT"
    if [ "$ENDPOINT_MODE" = "alb" ] && [ -n "$HOSTNAME" ]; then
      echo "NOTE: create a DNS CNAME: $SCANNER_DOMAIN -> $HOSTNAME"
    fi
  else
    echo "WARNING: scanner endpoint hostname not available after 10 minutes."
    echo "Check: kubectl get svc,ingress -n visionone-filesecurity"
  fi
fi

echo "Bootstrap complete."
