# EKS Vision One File Security Scanner

Automated malware scanning pipeline on AWS. Files uploaded to an S3 bucket are automatically scanned using [TrendAI Vision One File Security](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-intro-origin) and routed to a clean bucket or quarantine bucket based on the scan result.

Everything deploys from a single CloudFormation template — the EKS cluster, networking, storage, queues, IAM, and the scanner application itself.

## What is Vision One File Security?

Vision One File Security is TrendAI's malware scanning service. It uses multiple detection engines — pattern matching, heuristics, and predictive machine learning (PML) — to identify threats in files of any type.

In this project, the scanner runs **inside the Kubernetes cluster** as a set of pods deployed via Helm. The scanner app communicates with these pods over gRPC to scan files, and the scanner pods phone home to the Vision One cloud for threat intelligence updates and to report results.

This means files are scanned locally within your VPC — they are never uploaded to an external service.

For more information, see the [Vision One File Security Helm chart repository](https://trendmicro.github.io/visionone-file-security-helm/).

## How It Works

```
                         S3 Ingest Bucket
                               |
                    s3:ObjectCreated event
                               |
                               v
                          SQS Queue ---------> Dead Letter Queue
                               |                (after 3 failures)
                               v                       |
                     Scanner App Pod (EKS)      DLQ Remediation Lambda
                        |            |          (re-queue with backoff,
                   Download file   Scan via      max 3 DLQ retries)
                   from S3         gRPC
                        |        (in-cluster
                  +-----+------+  V1FS pods)
                  |            |
              CLEAN        MALICIOUS
                  |            |
                  v            v
           Clean Bucket   Quarantine Bucket
                  |            |
              Delete from Ingest Bucket
                  |            |
         Delete SQS message + write audit log
```

1. A file lands in the **Ingest Bucket** (uploaded by a user, application, or pipeline)
2. S3 sends an event notification to an **SQS queue**
3. The **scanner app pod** long-polls the queue, picks up the message, and downloads the file into memory
4. The file is scanned using the **Vision One File Security Python SDK** over gRPC to the in-cluster scanner pods
5. Based on the result:
   - **Clean** (`scanResult == 0`) — file is copied to the Clean Bucket
   - **Malicious** (`scanResult > 0`) — file is copied to the Quarantine Bucket
6. The original file is deleted from the Ingest Bucket and the SQS message is removed

If scanning fails, the message stays in the queue and is retried. After 3 failures it moves to a Dead Letter Queue, where a Lambda function automatically re-queues it with exponential backoff (60s, 300s, 900s). After 3 DLQ retries (9 total scan attempts), the message is logged as a permanent failure and discarded. Each scan result is written to a CloudWatch Logs audit trail for compliance and troubleshooting.

## Architecture

### Infrastructure (CloudFormation)

The `eks-v1fs.yaml` template creates everything:

| Resource | Purpose |
|---|---|
| **VPC** | `10.2.0.0/16` with public and private subnets across 2 AZs |
| **NAT Gateways** | One per AZ — pods in private subnets reach the internet for threat intelligence updates |
| **EKS Cluster** | Private API endpoint, full audit logging, managed addons (vpc-cni, CoreDNS, kube-proxy, Pod Identity Agent, EBS CSI Driver, EFS CSI Driver) |
| **Node Group** | `r7i.large` system nodes (2 vCPU, 16 GiB) in private subnets, min 3 / max 6 — hosts system components only |
| **ECR Repository** | Hosts the scanner app container image, scan-on-push enabled |
| **S3 Buckets** | Ingest (with event notifications), Clean, Quarantine — all have `DeletionPolicy: Retain` to preserve files when the stack is deleted |
| **SQS Queues** | Main queue (600s visibility timeout, 20s long polling) + Dead Letter Queue (120s visibility timeout) |
| **DLQ Remediation Lambda** | Re-queues failed messages with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard |
| **CloudWatch Alarms** | DLQ messages (any > 0) and queue age (> 20 min for 5 consecutive minutes) via SNS topic |
| **Scan Audit Log** | CloudWatch log group with structured JSON per scan (file, verdict, malware names, SHA256, duration), 30-day retention |
| **CloudWatch Dashboard** | 26-widget dashboard with queue health, scan throughput/latency, malware detection stats, DLQ remediation, pod distribution, and recent scan results |
| **IAM Roles** | Least-privilege roles for nodes, bastion, scanner app, KEDA operator, Karpenter, and DLQ remediation |
| **Pod Identity** | Binds IAM roles to Kubernetes service accounts — no access keys needed |
| **Secrets Manager** | Stores the V1FS registration token and API key |
| **Metrics Server** | Provides CPU/memory metrics for cluster monitoring |
| **KEDA** | Scales both scanner-app and V1FS scanner pods based on SQS queue depth |
| **Karpenter NodePool** | Provisions xlarge scanner nodes (r7i/r7a/r6i) directly via EC2 Fleet API; consolidates underutilized nodes automatically |
| **EFS Filesystem** | Encrypted shared storage (ReadWriteMany) for V1FS scanner ephemeral volume across multiple pods |
| **Pre-delete Cleanup Lambda** | Runs automatically during stack deletion — terminates Karpenter EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes before CloudFormation deletes the roles and cluster |
| **Bastion Host** | Provisions the cluster, installs Helm charts, builds and deploys the scanner app |

### Scanner Application

A Python asyncio application built for speed. Scan requests use **gRPC** — a binary protocol that is dramatically faster than traditional REST/RPC, with lower latency, smaller payloads, and native streaming support. Files are scanned entirely in memory via `scan_buffer()`, eliminating disk I/O from the critical path. The result is scan latency measured in milliseconds, not seconds.

- **gRPC-native scanning** — binary protocol with persistent HTTP/2 connections to in-cluster scanner pods, avoiding the overhead of REST serialization and per-request TCP handshakes
- **Fully async pipeline** — `aiobotocore` for S3/SQS operations and `amaas.grpc.aio` for scan requests, all running concurrently on a single event loop with zero thread-blocking
- **In-memory scanning** — files are downloaded as byte buffers and passed directly to the scanner over gRPC, never written to disk
- **50 concurrent scans per pod** — each pod maintains 50 in-flight scan requests simultaneously (configurable via `MAX_CONCURRENT_SCANS`), fully saturating the scanner backend
- **Visibility heartbeat** — automatically extends SQS message visibility during long-running scans to prevent duplicate processing
- **Health probes** — liveness (`/healthz`) and readiness (`/readyz`) endpoints on port 8080. Liveness catches deadlocked event loops (pod is restarted); readiness gates traffic until the gRPC scan handle is initialized
- **Scan audit trail** — every scan result is written to CloudWatch Logs as structured JSON (file key, size, verdict, malware names, SHA256, scan duration, pod name), batched for efficiency
- **Graceful shutdown** — handles SIGTERM to drain in-flight scans and flush audit entries before exiting (5-minute grace period)
- **Predictive Machine Learning** — PML can be enabled for advanced threat detection (requires account-level PML support)

### Database & Storage

The Vision One File Security management service uses a **PostgreSQL database** deployed as a Kubernetes StatefulSet within the cluster. The database stores scan metadata, configuration, and operational state.

- **PostgreSQL StatefulSet** — deployed by the V1FS Helm chart with `dbEnabled: true`
- **EBS gp3 storage** — 100Gi encrypted persistent volume for database data, provisioned by the EBS CSI Driver
- **EFS shared storage** — 100Gi ReadWriteMany volume for scanner ephemeral files, provisioned by the EFS CSI Driver. Multiple scanner pods across different nodes share this storage simultaneously, eliminating the single-node bottleneck of block storage
- **StorageClasses** — `gp3` (EBS, block storage, ReadWriteOnce) for the database and `efs-sc` (EFS, network filesystem, ReadWriteMany) for scanner ephemeral volumes

The database configuration is immutable after initial deployment — changing storage class or size requires deleting and recreating the StatefulSet.

### Performance-Optimized Compute

The cluster uses two tiers of compute. A small managed node group of `r7i.large` instances (2 vCPU, 16 GiB) hosts system components (CoreDNS, KEDA, Karpenter, CSI drivers). Scanner workloads run on Karpenter-provisioned xlarge instances — `r7i.xlarge`, `r7a.xlarge`, or `r6i.xlarge` (4 vCPU, 32 GiB each). The R7i/R7a/R6i families provide consistent, non-burstable CPU performance with high memory-to-vCPU ratios, ensuring scanner pods have ample headroom for signature databases and in-memory file analysis without triggering OOM kills.

Each xlarge node fits 4 V1FS scanner pods (800m CPU / 2Gi memory each) alongside scanner-app pods, maximizing pod density while requiring fewer nodes to provision at scale.

### Autoscaling

Three independent scaling systems work in concert, each reacting to different signals but coordinating automatically through Kubernetes to deliver elastic throughput from a single idle pod to **7,500 concurrent scan slots** in under a minute.

**Queue-driven pod scaling (KEDA)** — KEDA watches the SQS queue every 10 seconds and scales scanner-app pods proportionally to the backlog. When thousands of files land in the ingest bucket, the resulting SQS messages trigger rapid scale-out — 1 pod for every 5 queued messages, up to 150 pods. Each pod immediately begins pulling messages and scanning files at 50 concurrent scans. When the queue drains, KEDA scales back down after a 90-second cooldown, keeping costs aligned with demand.

- Polls SQS queue depth every 10 seconds for fast reaction to bursts
- Includes in-flight messages (being scanned but not yet deleted) in scaling decisions
- Range: 1 to 150 pods (always at least 1 pod running, ready for immediate processing)

**Queue-driven scanner scaling (KEDA)** — The V1FS scanner pods also scale based on SQS queue depth, ensuring scan backend capacity grows proportionally to demand. KEDA adds 1 scanner pod per 50 queued messages, up to 150 pods. This replaced the original CPU-based HPA which was too conservative and only scaled to 4 pods under heavy load.

- Scales based on SQS queue depth (50 messages per scanner pod)
- Range: 1 to 150 scanner pods
- Each scanner pod requests 800m CPU and 2Gi memory

**Infrastructure scaling (Karpenter)** — When KEDA creates pods that can't be scheduled due to insufficient cluster capacity, Karpenter provisions new nodes directly via the EC2 Fleet API in 30-60 seconds — roughly 2x faster than the traditional Cluster Autoscaler/ASG approach. Karpenter selects the optimal instance type from a flexible set (r7i.xlarge, r7a.xlarge, r6i.xlarge) based on pending pod requirements and availability, eliminating capacity failures from single-instance-type dependency. When load subsides, Karpenter consolidates underutilized nodes after 2 minutes, intelligently bin-packing remaining pods onto fewer nodes before removing excess capacity. Pod Disruption Budgets protect active scan workloads from premature eviction during consolidation.

A small managed node group (3-6 nodes) hosts system components (CoreDNS, KEDA, EBS/EFS CSI drivers, LB controller, metrics server, Karpenter itself). Scanner workloads are directed to Karpenter-provisioned nodes via nodeAffinity, keeping the system plane isolated from workload scaling turbulence.

**Why Karpenter over Cluster Autoscaler:** This system is designed for sustained, latency-sensitive production traffic with unpredictable spikes. Karpenter's direct EC2 provisioning, intelligent consolidation, and multi-instance-type flexibility deliver faster scale-out, lower cost during low-demand periods, and better resilience compared to the ASG-based Cluster Autoscaler.

**Karpenter NodePool configuration (`scanner-pool`):**

| Setting | Value | Rationale |
|---|---|---|
| Instance types | r7i.xlarge, r7a.xlarge, r6i.xlarge | Memory-optimized (32 GiB), xlarge only for fewer nodes at scale |
| Capacity type | On-demand only | No spot — scan visibility timeouts make interruptions expensive |
| CPU limit | 300 vCPU | Matches AWS account quota, prevents over-provisioning |
| Memory limit | 2,400 GiB | Proportional to CPU limit |
| Consolidation policy | WhenEmptyOrUnderutilized | Bin-packs pods onto fewer nodes when load drops |
| Consolidation delay | 2 minutes | Balances cost savings against scaling churn |
| Disruption budget | 10% of nodes | Limits how many nodes can be drained simultaneously |
| Node expiry | 30 days | Automatic node rotation for security patching |
| AMI | Amazon Linux 2023 (EKS-optimized) | Auto-selected via `al2023@latest` |
| Block storage | 20 GiB gp3, encrypted | Matches managed node group configuration |
| IMDSv2 | Required (hop limit 2) | Security hardening |

**At full scale:**

| Layer | Min | Max | Multiplier |
|---|---|---|---|
| Scanner-app pods (KEDA) | 1 | 150 | 50 concurrent scans each |
| V1FS scanner pods (KEDA) | 1 | 150 | gRPC scan workers |
| Nodes (Karpenter) | 3 | ~75 | 4 vCPU each (xlarge only) |
| **Total concurrent scans** | **50** | **7,500** | **150x scale-out** |

**Important:** At full scale (150+150 pods), the cluster requires approximately 195 on-demand vCPUs for pod requests alone (150 × 500m scanner-app + 150 × 800m V1FS scanner), plus 6 vCPU for the 3 system nodes. The default AWS account limit is 64 vCPUs — request an increase to at least 300 via the AWS Service Quotas console for "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances." The Karpenter NodePool enforces a CPU limit of 300 to prevent exceeding the account quota, providing headroom above the ~200 vCPU pod request total.

**How they work together:**

```
Thousands of files arrive in S3
         |
         v
SQS queue depth spikes
         |
         v
KEDA detects backlog, scales scanner-app from 1 to 150 pods
         |
         v
Scanner-app pods open concurrent gRPC streams to V1FS scanner
         |
         v
KEDA scales V1FS scanner pods from 1 to 150 based on queue depth
         |
         v
Karpenter provisions nodes via EC2 Fleet API in 30-60 seconds
         |
         v
7,500 concurrent scans running — queue drains rapidly
         |
         v
Queue empty → KEDA scales pods back down → Karpenter consolidates idle nodes
```

KEDA and Karpenter get their AWS permissions through Pod Identity — KEDA reads SQS queue metrics, Karpenter manages EC2 instances directly. No access keys are involved.

### How Credentials Work

The scanner app pod gets AWS permissions automatically through EKS Pod Identity. No access keys are configured anywhere.

1. CloudFormation creates an IAM role (`ScannerAppRole`) with permissions scoped to the specific S3 buckets, SQS queue, and Secrets Manager secret
2. A Pod Identity Association binds this role to the `scanner-app` Kubernetes service account
3. The Pod Identity Agent (a DaemonSet on each node) intercepts credential requests from the pod and injects temporary credentials
4. The app retrieves the V1FS API key from Secrets Manager at startup using these credentials

## Prerequisites

You need two credentials from the TrendAI Vision One console:

1. **Registration Token** — used by the scanner pods to register with Vision One. Generate this under **Cloud Security > File Security > Containerized Scanner > Get ready to deploy containerized scanner > Get registration token**.
2. **API Key** — used by the scanner application to authenticate scan requests. Generate this under **Administration > API Keys > Add API Key** with the **"Run file scan via SDK"** permission.

## Deployment

### Launch the stack

Download the `eks-v1fs.yaml` CloudFormation template and deploy it in AWS CloudFormation. The template requires two parameters:

- **RegistrationToken** — your Vision One File Security registration token
- **ApiKey** — your Vision One API key with "Run file scan via SDK" permission

Optional parameters:

| Parameter | Default | Description |
|---|---|---|
| **PrimaryAZ** | `us-east-1a` | Availability Zone 1 |
| **SecondaryAZ** | `us-east-1b` | Availability Zone 2 |
| **NodeInstanceType** | `r7i.large` | EC2 instance type for EKS worker nodes |
| **DesiredCapacity** | `3` | Number of system nodes in managed node group (3–6) |
| **PMLEnabled** | `false` | Enable Predictive Machine Learning scanning (requires account support) |

You don't need to clone the repo. The bastion host UserData automatically:

1. Installs kubectl, Helm, eksctl, Docker, and the AWS CLI
2. Configures kubeconfig and creates the `visionone-filesecurity` namespace
3. Installs Karpenter and KEDA via Helm
4. Deploys the Vision One File Security scanner pods via Helm (GPG-verified)
5. Clones this repo, builds the scanner app Docker image, pushes it to ECR
6. Deploys the scanner app (ServiceAccount, ConfigMap, Deployment, KEDA ScaledObject) to the cluster

Stack creation takes approximately 20-30 minutes. Monitor progress:

```bash
aws cloudformation describe-stacks --stack-name my-scanner --query 'Stacks[0].StackStatus'
```

### Verify

Connect to the bastion via Session Manager:

```bash
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`BastionPublicIP`].OutputValue' --output text)

# Or use Session Manager (no SSH key needed):
aws ssm start-session --target <instance-id>
```

Check that everything is running:

```bash
kubectl get pods -n visionone-filesecurity
```

You should see the V1FS scanner pods and the `scanner-app` pod all in `Running` state.

### Test

Upload a file to the ingest bucket:

```bash
INGEST_BUCKET=$(aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`IngestBucketName`].OutputValue' --output text)

echo "Hello, this is a clean test file" > /tmp/testfile.txt
aws s3 cp /tmp/testfile.txt s3://$INGEST_BUCKET/
```

Watch the scanner app logs:

```bash
kubectl logs -f deployment/scanner-app -n visionone-filesecurity
```

You should see the file scanned and routed to the clean bucket.

To test malware detection, upload the [EICAR test file](https://www.eicar.org/download-anti-malware-testfile/):

```bash
echo 'X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' \
  > /tmp/eicar.txt
aws s3 cp /tmp/eicar.txt s3://$INGEST_BUCKET/
```

This should be flagged as malicious and routed to the quarantine bucket.

## Project Structure

```
eks-v1fs.yaml              CloudFormation template (all infrastructure)
app/
  Dockerfile               python:3.11-slim, non-root UID 999
  requirements.txt         Pinned dependencies (visionone-filesecurity, aiobotocore, boto3)
  scanner.py               Async SQS polling, S3 download, V1FS scan, file routing, health server, audit trail
  config.py                Environment variable loading and validation
k8s/
  serviceaccount.yaml      Kubernetes ServiceAccount (Pod Identity, no annotations)
  configmap.yaml           Environment config template (populated by deploy script)
  deployment.yaml          Hardened pod spec (non-root, read-only fs, drop all capabilities)
  networkpolicy.yaml       Egress restricted to DNS, V1FS scanner, and AWS HTTPS
  pdb.yaml                 PodDisruptionBudgets (Karpenter consolidation protection)
  scaledobject.yaml        KEDA ScaledObject + TriggerAuthentication for SQS-driven autoscaling
scripts/
  build-and-push.sh        Build Docker image and push to ECR (tagged with git SHA)
  deploy.sh                Template ConfigMap from stack outputs and apply k8s manifests
```

## Redeploying the Scanner App

To update the custom scanner-app code (Python application in `app/`), connect to the bastion via Session Manager and run:

```bash
cd /opt/eks-v1fs && git pull
export CFN_STACK_NAME=my-scanner
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
```

This rebuilds the Docker image, pushes it to ECR, and re-applies the k8s manifests. It does **not** update the V1FS scanner — see [Updating the V1FS Scanner](#updating-the-v1fs-scanner) for that.

## Updating the V1FS Scanner

The TrendAI Vision One File Security scanner is installed via its official Helm chart, but this deployment customizes several Helm values to integrate with our KEDA-based autoscaling and EKS infrastructure. A standard `helm upgrade` without re-specifying these values will silently revert them to chart defaults, breaking the deployment.

### Why this matters

The Helm chart's default behavior conflicts with our architecture in several ways:

| Default behavior | Our customization | What breaks if reverted |
|---|---|---|
| HPA enabled (`scanner.autoscaling.enabled=true`) | HPA disabled, KEDA scales instead | Two controllers fight over replica count, causing erratic scaling |
| Default CPU/memory requests | 800m CPU / 2Gi memory | Scanner pods may be under-resourced or improperly bin-packed |
| No EFS ephemeral volume | EFS-backed shared ephemeral storage | Scanner pods lose shared scratch space |
| Default storage class | gp3 StorageClass for database PVC | Database PVC may fail to provision |

### Safe upgrade procedure

When TrendAI releases a new scanner image, upgrade via the bastion host using Session Manager:

```bash
aws ssm start-session --target <bastion-instance-id>
```

Then run the upgrade with **all custom values explicitly specified**:

```bash
helm repo update visionone-filesecurity

helm upgrade my-release visionone-filesecurity/visionone-filesecurity \
  -n visionone-filesecurity \
  --set scanner.autoscaling.enabled=false \
  --set scanner.resources.requests.cpu=800m \
  --set scanner.resources.requests.memory=2Gi \
  --set visiononeFilesecurity.management.dbEnabled=true \
  --set databaseContainer.storageClass.create=false \
  --set databaseContainer.persistence.storageClassName=gp3 \
  --set databaseContainer.persistence.size=100Gi \
  --set scanner.ephemeralVolume.enabled=true \
  --set scanner.ephemeralVolume.storageClass=efs-sc \
  --set scanner.ephemeralVolume.accessMode=ReadWriteMany \
  --set scanner.ephemeralVolume.size=100Gi
```

**Do not use `--reuse-values`** — if the new chart version renames or adds values, `--reuse-values` can cause silent misconfiguration.

### What does not need re-applying after upgrade

These resources are managed separately from the Helm chart and are not affected by `helm upgrade`:

- **KEDA ScaledObjects** — applied via `kubectl`, not Helm
- **PodDisruptionBudgets** — applied via `kubectl`
- **Pod Identity associations** — managed by CloudFormation
- **Scanner-app deployment** — separate deployment, not part of the V1FS chart

### Verify after upgrade

```bash
# Confirm no HPA was created (should return empty)
kubectl get hpa -n visionone-filesecurity

# Confirm KEDA ScaledObjects are still active
kubectl get scaledobject -n visionone-filesecurity

# Confirm scanner pods are running with correct resources
kubectl describe pod -n visionone-filesecurity -l app.kubernetes.io/component=scanner | grep -A 2 "Requests:"

# Run a sanity scan (1 clean + 1 EICAR) to verify scanning works
```

## Security

- All S3 buckets use AES256 encryption with public access fully blocked
- SQS queues use server-side encryption
- All S3 buckets have `DeletionPolicy: Retain` — they survive stack deletion to preserve files for forensic investigation
- ECR repository has scan-on-push enabled for container vulnerability scanning
- IMDSv2 is enforced on all nodes
- EBS volumes are encrypted
- EFS filesystem is encrypted at rest and in transit (TLS mount option)
- VPC Flow Logs capture all network traffic
- EKS audit logging is enabled for all control plane components
- IAM policies use least-privilege, resource-scoped permissions
- Credentials are stored in Secrets Manager, never in plaintext
- The V1FS Helm chart is GPG-verified before installation

## Cleanup

```bash
aws cloudformation delete-stack --stack-name my-scanner
```

A pre-delete cleanup Lambda runs automatically during stack deletion — it terminates Karpenter-managed EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes before CloudFormation removes the roles and cluster. No manual cleanup is needed for these resources.

Note: All S3 buckets (ingest, clean, quarantine) have `DeletionPolicy: Retain` and are **not deleted** with the stack. This prevents accidental data loss — scanned files and quarantined malware are preserved for forensic investigation or audit. The ECR repository and its images **are** deleted with the stack. To clean up retained buckets after stack deletion:

```bash
# List retained buckets (names are in the stack outputs, saved before deletion)
aws s3 rb s3://<ingest-bucket> --force
aws s3 rb s3://<clean-bucket> --force
aws s3 rb s3://<quarantine-bucket> --force
```
