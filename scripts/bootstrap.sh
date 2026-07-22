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
#   EXISTING_INGEST_BUCKET=<name>   existing-user-bucket mode ("" = created)
# ----------------------------------------------------------------------
set -e

V1FS_CHART_VERSION="${V1FS_CHART_VERSION:-1.4.10}"

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:$PATH
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve the 'auto' endpoint mode: NLB (L4) for both full-auto and BYO. The
# NLB is VPC-reachable AND doubles as the pod-discovery registry (target-type=ip
# target group) for client-side dispatchers. alb is explicit opt-in.
# Mirrors the ExposeNLB/ExposeALB Conditions in the CloudFormation template.
if [ "${ENDPOINT_MODE:-}" = "auto" ]; then
  ENDPOINT_MODE="nlb"
  echo "Resolved ScannerEndpointMode=auto -> nlb"
fi

# Resolve the scanner autoscaler:
#   hpa  -> the chart's CPU/mem HPA (TrendAI-supported; python-default option)
#   keda -> KEDA scales the scanner on SQS queue depth (python-KEDA/java-KEDA).
# keda needs a queue: full-auto uses the stack's queue (deploy.sh applies the
# ScaledObject); BYO uses ExternalScanQueueArn (applied later in this script).
# keda with no queue available falls back to hpa so the scanner still scales.
SCANNER_KEDA=false
if [ "${SCANNER_SCALING_MODE:-hpa}" = "keda" ]; then
  if [ "${DEPLOY_SCANNER_APP:-}" = "true" ] || [ -n "${EXTERNAL_SCAN_QUEUE_ARN:-}" ]; then
    SCANNER_KEDA=true
    echo "Scanner autoscaler: KEDA on queue depth (chart HPA disabled)"
  else
    echo "WARNING: ScannerScalingMode=keda but no scan queue (BYO without ExternalScanQueueArn) — falling back to chart HPA."
  fi
else
  echo "Scanner autoscaler: chart CPU/mem HPA (TrendAI-supported)"
fi
export SCANNER_KEDA

# Ensure /usr/local/bin is on PATH and KUBECONFIG is set for SSH sessions
echo 'export PATH=/usr/local/bin:$PATH' > /etc/profile.d/local-bin.sh
echo 'export KUBECONFIG=/root/.kube/config' >> /etc/profile.d/local-bin.sh
chmod 644 /etc/profile.d/local-bin.sh

# ---- System packages ----
apt-get update -y
apt-get remove needrestart -y || true
apt-get install -y python3 python3-pip curl gpg apt-transport-https unzip jq

# Python libraries for in-VPC testing from the bastion: boto3 (S3 access — the
# AWS CLI bundles its own Python and does NOT expose boto3 to system python3)
# and the V1FS gRPC SDK, so load/sanity scans against the scanner endpoint run
# without a manual install. Version matches app/requirements.txt.
pip3 install --quiet boto3 "visionone-filesecurity==1.4.1" || true

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

# Obtain the registration token. If one was provided as a stack parameter it
# lives in Secrets Manager; otherwise mint one at deploy time from the Vision
# One API using the ApiKey (POST /beta/fileSecurity/ctr/registration) — this
# always yields a currently-valid token and removes a manual setup step.
# Files (not shell vars) carry the credentials so `bash -x` tracing never
# echoes them into cloud-init logs; set +x guards the sensitive block.
if [ -n "$V1FS_REGISTRATION_SECRET" ]; then
  aws secretsmanager get-secret-value \
    --secret-id "$V1FS_REGISTRATION_SECRET" \
    --query SecretString --output text \
    --region "$AWS_REGION" | tr -d '\n' > /tmp/.reg-token
else
  echo "No registration token provided — minting one via the Vision One API..."
  set +x
  umask 077
  printf 'Authorization: Bearer %s' "$(aws secretsmanager get-secret-value \
    --secret-id "$V1FS_API_KEY_SECRET_ARN" \
    --query SecretString --output text \
    --region "$AWS_REGION")" > /tmp/.authhdr
  curl -fsS -X POST "$V1_API_ENDPOINT/beta/fileSecurity/ctr/registration" \
    -H @/tmp/.authhdr \
    -H "Content-Type: application/json" | jq -r .token | tr -d '\n' > /tmp/.reg-token
  shred -u /tmp/.authhdr
  umask 022
  set -x
  # Fail fast with a clear message if the mint failed (empty/null token)
  if [ ! -s /tmp/.reg-token ] || grep -q '^null$' /tmp/.reg-token; then
    echo "ERROR: registration-token auto-fetch failed — provide RegistrationToken explicitly" >&2
    exit 1
  fi
fi
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

# ---- Install KEDA via Helm ----
# Needed for the scanner-app (full-auto) and/or to scale the V1FS scanner on a
# customer-provided external queue (BYO). Mirrors the KedaNeeded CFN Condition.
if [ "$DEPLOY_SCANNER_APP" = "true" ] || [ -n "${EXTERNAL_SCAN_QUEUE_ARN:-}" ]; then
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

# ---- Self-signed scanner cert for ALB mode (optional) ----
# ALB mode needs an ACM cert for its HTTPS/gRPC listener. When the user does
# not bring one, generate a self-signed cert for the scanner domain, import it
# to ACM for the listener, and store the public cert in a Secret + SSM so gRPC
# clients can trust it (SDK ca_cert / V1FS_CA_CERT). No publicly-signed
# certificate required. The SAN must match the host clients connect to, since
# the V1FS SDK verifies the cert name and has no skip-verify option.
if [ "$ENDPOINT_MODE" = "alb" ] && [ "$SELF_SIGNED_CERT" = "true" ] && [ -z "$ACM_CERT_ARN" ]; then
  echo "Generating self-signed scanner cert for $SCANNER_DOMAIN..."
  CERT_DIR="$(mktemp -d)"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
    -days 825 -subj "/CN=$SCANNER_DOMAIN" \
    -addext "subjectAltName=DNS:$SCANNER_DOMAIN"

  echo "Importing self-signed cert to ACM..."
  ACM_CERT_ARN=$(aws acm import-certificate \
    --certificate "fileb://$CERT_DIR/tls.crt" \
    --private-key "fileb://$CERT_DIR/tls.key" \
    --tags "Key=Name,Value=scanner-selfsigned-$CFN_STACK_NAME" \
    --region "$AWS_REGION" \
    --query CertificateArn --output text)
  echo "Imported ACM cert: $ACM_CERT_ARN"

  # Store ONLY the public cert (self-signed = its own CA) for clients to trust.
  # The private key stays in ACM (the ALB uses it); it is never persisted here.
  kubectl create secret generic scanner-tls-ca \
    --from-file=ca.crt="$CERT_DIR/tls.crt" \
    -n visionone-filesecurity --dry-run=client -o yaml | kubectl apply -f -
  aws ssm put-parameter \
    --name "/$CFN_STACK_NAME/scanner-ca-cert" \
    --value "$(cat "$CERT_DIR/tls.crt")" \
    --type String --overwrite --region "$AWS_REGION"
  echo "Scanner CA cert stored in Secret scanner-tls-ca and SSM /$CFN_STACK_NAME/scanner-ca-cert"

  rm -rf "$CERT_DIR"
fi

# Endpoint-mode helm arguments.
# nlb: chart-native externalService as an internal NLB (values-nlb.yaml)
# alb: chart-native ingress with gRPC backend + TLS (Trend's documented topology)
ENDPOINT_ARGS=""
if [ "$ENDPOINT_MODE" = "nlb" ]; then
  ENDPOINT_ARGS="-f $REPO_DIR/helm/values-nlb.yaml"
elif [ "$ENDPOINT_MODE" = "alb" ]; then
  # ALB idle timeout must exceed the scanner's scan timeout, else the ALB
  # severs a connection during a long file's quiet analysis phase (no bytes
  # flow while the engine crunches) before the scanner's own deadline fires,
  # turning a slow scan into an opaque connection reset that DLQ-loops. Add a
  # 60s buffer so TM_AM_SCAN_TIMEOUT_SECS wins the race. Max ALB value is 4000.
  IDLE_TIMEOUT=$(( ${TM_AM_SCAN_TIMEOUT_SECS:-600} + 60 ))
  [ "$IDLE_TIMEOUT" -gt 4000 ] && IDLE_TIMEOUT=4000
  # hosts[0].host replaces the chart's default hosts[0] object wholesale, which
  # drops its default paths and renders an Ingress with no spec.rules[].http.paths
  # (invalid). Set the path explicitly alongside the host.
  ENDPOINT_ARGS="--set scanner.ingress.enabled=true \
    --set scanner.ingress.className=alb \
    --set scanner.ingress.hosts[0].host=$SCANNER_DOMAIN \
    --set scanner.ingress.hosts[0].paths[0].path=/ \
    --set scanner.ingress.hosts[0].paths[0].pathType=Prefix \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/backend-protocol-version=GRPC \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/target-type=ip \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/scheme=internal \
    --set scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/certificate-arn=$ACM_CERT_ARN \
    --set-string scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/load-balancer-attributes=idle_timeout.timeout_seconds=$IDLE_TIMEOUT \
    --set-string scanner.ingress.annotations.alb\\.ingress\\.kubernetes\\.io/listen-ports=[{\"HTTPS\":443}]"
fi

# Chart 1.4.10 workaround: the management-service init job's K8s->DB data
# migration requires the ontap-agent-config ConfigMap and crash-loops on a
# fresh install without it (we don't use ONTAP agents). Pre-create it empty.
kubectl create configmap ontap-agent-config -n visionone-filesecurity 2>/dev/null || true

# Chart HPA args follow the scanner autoscaler resolved at the top:
#   keda -> disable the chart HPA (KEDA owns scanner scaling; the ScaledObject
#           is applied by deploy.sh in full-auto or below for BYO+external).
#   hpa  -> enable the chart HPA with the CFN replica bounds.
if [ "$SCANNER_KEDA" = "true" ]; then
  SCANNER_AUTOSCALE_ARGS="--set scanner.autoscaling.enabled=false"
else
  SCANNER_AUTOSCALE_ARGS="--set scanner.autoscaling.enabled=true \
    --set scanner.autoscaling.minReplicas=$SCANNER_MIN_REPLICAS \
    --set scanner.autoscaling.maxReplicas=$SCANNER_MAX_REPLICAS"
fi

# Install the chart (no --wait; pods need time to register with Vision One
# cloud on first startup). values-base is the single source of truth.
# shellcheck disable=SC2086
helm install my-release visionone-filesecurity/visionone-filesecurity \
  -n visionone-filesecurity \
  --version "$V1FS_CHART_VERSION" \
  -f "$REPO_DIR/helm/values-base.yaml" \
  $SCANNER_AUTOSCALE_ARGS \
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
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  systemctl enable docker
  systemctl start docker

  # Existing-user-bucket mode: never delete the user's objects —
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

# ---- BYO external-queue: KEDA scales the scanner on the customer's queue ----
# In full-auto, deploy.sh applies this ScaledObject against our queue. In BYO
# (deploy.sh is not run) with an external queue, apply it here against that
# queue so the scanner fleet still tracks the backlog. The chart HPA is off.
if [ "$SCANNER_KEDA" = "true" ] && [ "$DEPLOY_SCANNER_APP" != "true" ]; then
  echo "Applying KEDA scanner ScaledObject for external queue..."
  # arn:aws:sqs:REGION:ACCOUNT:NAME -> URL. Region taken from the ARN so a
  # cross-region queue resolves correctly.
  EXT_REGION=$(echo "$EXTERNAL_SCAN_QUEUE_ARN" | cut -d: -f4)
  EXT_ACCOUNT=$(echo "$EXTERNAL_SCAN_QUEUE_ARN" | cut -d: -f5)
  EXT_NAME=$(echo "$EXTERNAL_SCAN_QUEUE_ARN" | cut -d: -f6)
  EXT_QUEUE_URL="https://sqs.${EXT_REGION}.amazonaws.com/${EXT_ACCOUNT}/${EXT_NAME}"
  sed -e "s|<SQS_QUEUE_URL>|${EXT_QUEUE_URL}|g" \
      -e "s|<AWS_REGION>|${EXT_REGION}|g" \
      -e "s|<SCANNER_MIN_REPLICAS>|${SCANNER_MIN_REPLICAS:-1}|g" \
      -e "s|<SCANNER_MAX_REPLICAS>|${SCANNER_MAX_REPLICAS:-10}|g" \
      -e "s|<SCANNER_QUEUE_LENGTH>|${SCANNER_QUEUE_LENGTH:-50}|g" \
      "$REPO_DIR/k8s/scanner-scaledobject.yaml" | kubectl apply -f -
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
      if [ "$SELF_SIGNED_CERT" = "true" ]; then
        echo "NOTE: self-signed cert in Secret scanner-tls-ca and SSM /$CFN_STACK_NAME/scanner-ca-cert."
        echo "      gRPC clients must trust it (SDK ca_cert / V1FS_CA_CERT)."
      fi
    fi
  else
    echo "WARNING: scanner endpoint hostname not available after 10 minutes."
    echo "Check: kubectl get svc,ingress -n visionone-filesecurity"
  fi
fi

echo "Bootstrap complete."
