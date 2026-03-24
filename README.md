# EKS Vision One File Security Scanner

High performance malware scanning pipeline on AWS. Files uploaded to an S3 bucket are automatically scanned using [TrendAI Vision One File Security](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-intro-origin) and routed to a clean bucket, review bucket, or quarantine bucket based on the scan result.

Everything deploys from a single CloudFormation template — the EKS cluster, networking, storage, queues, IAM, and the scanner application itself.

## What is Vision One File Security?

Vision One File Security is TrendAI's malware scanning service for files. It uses multiple detection engines — pattern matching, heuristics, and predictive machine learning (PML) — to identify threats in files of any type.

In this project, the scanner runs **inside the Kubernetes cluster** as a set of pods deployed via Helm. The scanner app communicates with these pods over gRPC to scan files, and the scanner pods phone home to the Vision One cloud for threat intelligence updates and to report results.

This means files are scanned locally within your VPC — they are never uploaded to an external service.

For more information, see the [Vision One File Security Helm chart repository](https://trendmicro.github.io/visionone-file-security-helm/).

## How It Works

The pipeline has two stages: a **main pipeline** that scans every file with decompression limits enforced, and a **review pipeline** that re-scans files whose archives exceeded those limits with no restrictions.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ MAIN PIPELINE (decompression limits enforced)                          │
│                                                                        │
│  S3 Ingest ──▶ SQS Queue ──▶ Scanner App ──▶ V1FS Scanner (gRPC)      │
│                    │                               │                   │
│                    ▼                         ┌─────┼──────┐            │
│              DLQ (3 fails)                CLEAN  REVIEW  MALICIOUS     │
│                    │                        │      │       │           │
│             Lambda auto-retry               ▼      │       ▼          │
│            (backoff → discard)        Clean Bucket  │  Quarantine      │
│                                                    ▼                   │
├─────────────────────────────────────── Review Bucket ──────────────────┤
│                                            │                           │
│ REVIEW PIPELINE (no decompression limits)  │                           │
│                                            ▼                           │
│                               Review SQS Queue                        │
│                                    │                                   │
│                                    ▼                                   │
│                            Review Scanner ──▶ V1FS rv (no limits)      │
│                                                    │                   │
│                              Review DLQ      ┌─────┴─────┐            │
│                                │           CLEAN      MALICIOUS        │
│                         Lambda auto-retry    │           │             │
│                                              ▼           ▼             │
│                                        Clean Bucket  Quarantine        │
└─────────────────────────────────────────────────────────────────────────┘
```

| Step | What happens |
|---|---|
| **Ingest** | A file lands in the S3 Ingest Bucket (uploaded by a user, application, or pipeline). S3 sends an `ObjectCreated` event to the SQS queue. |
| **Scan** | A scanner-app pod long-polls the queue, downloads the file into memory, and scans it via the V1FS Python SDK over gRPC to the in-cluster scanner pods. |
| **Route** | The file is copied to one of three destinations based on the scan result, then deleted from the Ingest Bucket. The SQS message is removed and the result is written to the CloudWatch audit trail. |
| **Review** | Files routed to the Review Bucket are archives the main scanner could not fully analyze — the file size, nesting depth, file count, compression ratio, or decompressed size exceeded the configured limits. The review pipeline re-scans these with a second V1FS scanner release (`rv`) that has no decompression limits, then routes to Clean or Quarantine. The review pipeline keeps one pod always warm for immediate processing. |

**Routing rules:**

| Verdict | Condition | Destination |
|---|---|---|
| **Clean** | `scanResult == 0`, no decompression errors | Clean Bucket |
| **Review** | `scanResult == 0`, decompression limit exceeded | Review Bucket → re-scanned by review pipeline |
| **Review (oversize)** | File exceeds `MAX_FILE_SIZE_MB` (default 500) | Review Bucket → server-side copy (no download), re-scanned by review pipeline |
| **Malicious** | `scanResult > 0` | Quarantine Bucket |

### Failure Handling

If a scan fails (gRPC error, download failure, or any transient exception), the scanner immediately shortens the SQS message visibility timeout to 30 seconds, making it available for another pod to pick up almost immediately. Without this, failed messages would stay invisible for the full visibility timeout (600 seconds) before being retried — a 10-minute delay for what might be a momentary network blip. The fast retry ensures transient failures recover in seconds, not minutes.

After 3 consecutive failures, the message moves to a Dead Letter Queue. A Lambda function automatically re-queues DLQ messages with exponential backoff (60s → 300s → 900s). After 3 DLQ retries (9 total scan attempts), the message is logged as a permanent failure and discarded. Both pipelines have independent DLQs and remediation Lambdas.

```
Scan fails → visibility shortened to 30s → fast retry by another pod
  ↓ (3 failures)
DLQ → Lambda re-queues with backoff (60s → 300s → 900s)
  ↓ (3 DLQ retries = 9 total attempts)
Permanent failure logged and discarded
```

### Orphaned File Reconciliation

The review scanner includes a reconciliation loop that monitors the Ingest Bucket for orphaned files — objects that were uploaded but never processed due to transient failures such as scanner pod restarts, or SQS message expiration. Every 5 minutes, the review scanner lists the Ingest Bucket and sends a synthetic SQS message for any file older than 30 minutes, re-entering it into the main scan pipeline. This ensures no file is silently dropped, even if every retry mechanism in the normal flow has been exhausted. Both the interval and age threshold are configurable via `RECONCILIATION_INTERVAL` and `RECONCILIATION_AGE_THRESHOLD` environment variables.

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
| **S3 Buckets** | Ingest (with event notifications), Clean, Review, Quarantine — all have `DeletionPolicy: Retain` to preserve files when the stack is deleted |
| **SQS Queues** | Main queue (600s visibility timeout, 20s long polling) + Dead Letter Queue (120s visibility timeout); Review SQS queue + Review DLQ for the review pipeline |
| **DLQ Remediation Lambda** | Re-queues failed messages with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard |
| **Review DLQ Remediation Lambda** | Same retry logic as the main DLQ Lambda, handles review pipeline failures independently |
| **CloudWatch Alarms** | DLQ messages (any > 0), queue age (> 20 min for 5 consecutive minutes), and review DLQ messages (any > 0) via SNS topic |
| **Scan Audit Log** | CloudWatch log group with structured JSON per scan (file, verdict, malware names, SHA256, duration), 30-day retention |
| **Review Audit Log** | Separate CloudWatch log group (`review-audit-${StackName}`) for review pipeline scan results, 30-day retention |
| **CloudWatch Dashboard** | 29-widget dashboard with queue health, scan throughput/latency, malware detection stats, DLQ remediation, pod distribution, recent scan results, and review pipeline metrics |
| **IAM Roles** | Least-privilege roles for nodes, bastion, scanner app, KEDA operator, Karpenter, and DLQ remediation |
| **Pod Identity** | Binds IAM roles to Kubernetes service accounts — no access keys needed |
| **Secrets Manager** | Stores the V1FS registration token and API key |
| **Metrics Server** | Provides CPU/memory metrics for cluster monitoring |
| **KEDA** | Scales both scanner-app and V1FS scanner pods based on SQS queue depth; also scales review pipeline pods |
| **Karpenter NodePool** | Provisions xlarge scanner nodes (r7i/r7a/r6i) directly via EC2 Fleet API; consolidates underutilized nodes automatically |
| **V1FS Review Release** | Second Helm release (`rv`) with no CLISH scan policy — unlimited decompression for deep analysis of review bucket files |
| **EFS Filesystem** | Encrypted shared storage (ReadWriteMany) for V1FS scanner ephemeral volume across multiple pods |
| **Pre-delete Cleanup Lambda** | Runs automatically during stack deletion — terminates Karpenter EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes before CloudFormation deletes the roles and cluster |
| **Bastion Host** | Provisions the cluster, installs Helm charts, builds and deploys the scanner app |

### Scanner Application

A Python asyncio application built for speed. Scan requests use **gRPC** — a binary protocol that is dramatically faster than traditional REST/RPC, with lower latency, smaller payloads, and native streaming support. Files are scanned entirely in memory via `scan_buffer()`, eliminating disk I/O from the critical path. The result is scan latency measured in milliseconds, not seconds.

- **gRPC-native scanning** — binary protocol with persistent HTTP/2 connections to in-cluster scanner pods, avoiding the overhead of REST serialization and per-request TCP handshakes
- **Fully async pipeline** — `aiobotocore` for S3/SQS operations and `amaas.grpc.aio` for scan requests, all running concurrently on a single event loop with zero thread-blocking
- **In-memory scanning** — files are downloaded as byte buffers and passed directly to the scanner over gRPC, never written to disk
- **50 concurrent scans per pod** — each pod maintains 50 in-flight scan requests simultaneously (configurable via `MAX_CONCURRENT_SCANS`), fully saturating the scanner backend
- **Visibility heartbeat** — automatically extends SQS message visibility during long-running scans to prevent duplicate processing. On failure, immediately shortens visibility to 30 seconds for fast retry by another pod
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

**Queue-driven pod scaling (KEDA)** — KEDA watches the SQS queue every 5 seconds and scales scanner-app pods proportionally to the backlog. When thousands of files land in the ingest bucket, the resulting SQS messages trigger rapid scale-out — 1 pod for every 5 queued messages, up to 150 pods. Each pod immediately begins pulling messages and scanning files at 50 concurrent scans. When the queue drains, KEDA scales back down after a 300-second cooldown, keeping pods alive through tail processing before releasing capacity.

- Polls SQS queue depth every 5 seconds for fast reaction to bursts
- Includes in-flight messages (being scanned but not yet deleted) in scaling decisions
- Range: 1 to 150 pods (always at least 1 pod running, ready for immediate processing)

**Queue-driven scanner scaling (KEDA)** — The V1FS scanner pods also scale based on SQS queue depth, ensuring scan backend capacity grows proportionally to demand. KEDA adds 1 scanner pod per 50 queued messages, up to 150 pods. This replaced the original CPU-based HPA which was too conservative and only scaled to 4 pods under heavy load.

- Scales based on SQS queue depth (50 messages per scanner pod)
- Range: 1 to 150 scanner pods
- Each scanner pod requests 800m CPU and 2Gi memory

**Review pipeline scaling (KEDA)** — The review pipeline uses the same KEDA SQS-driven pattern but with minimal scaling. Both review-scanner-app and review-v1fs-scanner scale from 1 to 5 pods based on review SQS queue depth (threshold 50), with a 300-second cooldown. One pod of each is always warm to avoid cold-start gRPC failures when files arrive for deep analysis.

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
| Review scanner-app pods (KEDA) | 1 | 5 | Always-warm for low-latency gRPC |
| Review V1FS scanner pods (KEDA) | 1 | 5 | Unlimited decompression, always-warm |
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

The scanner app pods get AWS permissions automatically through EKS Pod Identity. No access keys are configured anywhere.

1. CloudFormation creates IAM roles with permissions scoped to specific resources — `ScannerAppRole` for the main scanner (ingest/clean/review/quarantine buckets, main SQS queue) and `ReviewScannerAppRole` for the review scanner (review/clean/quarantine buckets, review SQS queue — deliberately no write access to the review bucket)
2. Pod Identity Associations bind each role to its respective Kubernetes service account (`scanner-app` in `visionone-filesecurity`, `review-scanner-app` in `visionone-review`)
3. The Pod Identity Agent (a DaemonSet on each node) intercepts credential requests from pods and injects temporary credentials
4. Each scanner app retrieves the V1FS API key from Secrets Manager at startup using these credentials

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
| **MaxFileSizeMB** | `500` | Maximum file size in MB the main scanner will download and scan (1–2048). Files exceeding this limit are routed to the review bucket via server-side S3 copy (no download into pod memory), where the review scanner re-scans them with no size limit |

#### Scan Policy Parameters

These parameters configure the V1FS scanner's decompression behavior, controlling how the scanner handles compressed archives (ZIP, RAR, nested archives, etc.). Without explicit limits, the scanner defaults to unlimited — these parameters provide protection against archive-based attacks while allowing legitimate files through.

| Parameter | Default | Range | Description |
|---|---|---|---|
| **MaxDecompressionLayer** | `10` | 1–20 | Maximum archive nesting depth. Controls how many levels of nested archives the scanner will unpack (e.g., a zip inside a zip inside a zip). Higher values catch deeply nested malware but increase scan time and memory usage. A value of 10 handles virtually all legitimate archives while blocking excessive nesting used in evasion techniques |
| **MaxDecompressionFileCount** | `1000` | 0+ (0 = unlimited) | Maximum number of files the scanner will extract from a single archive before stopping. Protects against archive bombs that contain millions of tiny files designed to exhaust memory or stall scanning. 1000 is sufficient for most legitimate archives including large software packages |
| **MaxDecompressionRatio** | `150` | 100–2147483647 | Maximum allowed compression ratio (decompressed size ÷ compressed size). A classic zip bomb is a small file that decompresses to an enormous payload — the scanner skips entries exceeding this ratio. At the default of 150, a 1 MB compressed file is allowed to decompress to at most 150 MB |
| **MaxDecompressionSize** | `512` | 0–2048 MB (0 = unlimited) | Maximum total decompressed size in MB the scanner will process from a single archive. Caps the total memory and disk consumed when unpacking large archives. 512 MB provides ample headroom for legitimate large archives while preventing resource exhaustion |

These settings are applied automatically during stack creation via the V1FS management service CLI (CLISH). To view or modify settings on a running cluster:

```bash
# View current scan policy
kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- clish scanner scan-policy show

# Modify settings (changes take effect immediately, no pod restart required)
kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- clish scanner scan-policy modify \
  --max-decompression-layer=10 \
  --max-decompression-file-count=1000 \
  --max-decompression-ratio=150 \
  --max-decompression-size=512
```

You don't need to clone the repo to deploy. The bastion host UserData automatically:

1. Installs kubectl, Helm, eksctl, Docker, and the AWS CLI
2. Configures kubeconfig and creates the `visionone-filesecurity` namespace
3. Installs Karpenter and KEDA via Helm
4. Deploys the Vision One File Security scanner pods via Helm (GPG-verified)
5. Configures the scan policy decompression limits via CLISH
6. Installs second V1FS scanner release (`rv`) with unlimited decompression (no CLISH scan policy)
7. Clones this repo, builds the scanner app Docker image, pushes it to ECR
8. Deploys the scanner app and review scanner app (`deploy.sh --review`) to the cluster

If you want to further develop the application using [Claude Code](https://claude.ai/claude-code), clone the repo — it includes `CLAUDE.md` and supporting files in the `docs/` directory that provide Claude with comprehensive project context, architectural constraints, and operational guardrails.

Stack creation takes approximately 20-30 minutes. Monitor progress:

```bash
aws cloudformation describe-stacks --stack-name my-scanner --query 'Stacks[0].StackStatus'
```

### Stack Outputs

After creation, the stack exports key resource identifiers:

| Output | Description |
|---|---|
| `DashboardUrl` | CloudWatch dashboard URL — real-time pipeline monitoring |
| `IngestBucketName` | S3 bucket for uploading files to scan |
| `CleanBucketName` | S3 bucket for files that passed scanning |
| `QuarantineBucketName` | S3 bucket for malicious files |
| `ReviewBucketName` | S3 bucket for files requiring manual review (decompression limits exceeded) |
| `FileScanQueueUrl` | SQS queue URL for S3 file events |
| `FileScanDLQUrl` | SQS dead letter queue URL |
| `AlarmSNSTopicArn` | SNS topic for scan alarms (subscribe for notifications) |
| `ScanAuditLogGroupName` | CloudWatch log group for scan audit trail |
| `ReviewScanQueueUrl` | SQS queue URL for review pipeline file events |
| `ReviewScanDLQUrl` | SQS dead letter queue URL for review pipeline |
| `ReviewAuditLogGroupName` | CloudWatch log group for review scan audit trail |
| `ECRRepoUrl` | ECR repository for the scanner app image |
| `ClusterName` | EKS cluster name |
| `BastionPublicIP` | Bastion host IP (connect via SSM, not SSH) |

Retrieve any output:

```bash
aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardUrl`].OutputValue' --output text
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
  review-serviceaccount.yaml  Review scanner ServiceAccount (Pod Identity, no annotations)
  review-deployment.yaml      Review scanner deployment (same image, reconciliation enabled)
  review-networkpolicy.yaml   Egress restricted to DNS, rv V1FS scanner, AWS HTTPS
  review-scaledobject.yaml    KEDA ScaledObjects for review pipeline (min 1, max 5)
scripts/
  build-and-push.sh        Build Docker image and push to ECR (tagged with git SHA)
  deploy.sh                Template ConfigMap from stack outputs and apply k8s manifests
  upgrade.py               Safely upgrade both V1FS Helm releases with custom values
```

## Redeploying the Scanner App

To update the custom scanner-app code (Python application in `app/`), connect to the bastion via Session Manager and run:

```bash
cd /opt/eks-v1fs && git pull
export CFN_STACK_NAME=my-scanner
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
/opt/eks-v1fs/scripts/deploy.sh --review
```

This rebuilds the Docker image, pushes it to ECR, and re-applies the k8s manifests for both the main and review scanner apps. Both use the same Docker image — only the environment variables differ. This does **not** update the V1FS scanner — see [Updating the V1FS Scanner](#updating-the-v1fs-scanner) for that.

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

### Upgrade procedure

Connect to the bastion host via AWS Systems Manager Session Manager and run the upgrade script:

```bash
python3 /opt/eks-v1fs/scripts/upgrade.py
```

The script handles everything automatically:

1. Sets up the environment (KUBECONFIG, Helm repository)
2. Captures the current CLISH scan policy before upgrading
3. Updates the Helm repository and shows available versions
4. Upgrades both Helm releases (`my-release` and `rv`) with all required custom `--set` values
5. Re-applies the captured CLISH scan policy to `my-release` (not `rv` — it runs with unlimited decompression)
6. Verifies no HPA was re-created (would conflict with KEDA)
7. Verifies KEDA ScaledObjects are active and scanner pods are running
8. Runs a sanity scan (1 clean file + 1 EICAR test file) to confirm scanning works

The script checks whether a newer version is available and exits early if already up to date. To force a specific version (e.g., rollback or skip a release), use `--version X.Y.Z`. Other options: `--dry-run` to preview without executing, `--skip-sanity` to skip the test scan.

**Do not run `helm upgrade` manually** — a plain `helm upgrade` without all `--set` values reverts to chart defaults, re-enabling HPA (conflicts with KEDA) and resetting resources. The script ensures all custom values are specified. Do not use `--reuse-values` — if the new chart version renames or adds values, it can cause silent misconfiguration.

These resources are **not affected** by `helm upgrade` and do not need re-applying: KEDA ScaledObjects, PodDisruptionBudgets, Pod Identity associations, scanner-app deployment, and CLISH scan policy settings.

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
- Review scanner IAM role has no write access to the review bucket, preventing routing loops back to the review pipeline
- Credentials are stored in Secrets Manager, never in plaintext
- The V1FS Helm chart is GPG-verified before installation

## Cleanup

```bash
aws cloudformation delete-stack --stack-name my-scanner
```

A pre-delete cleanup Lambda runs automatically during stack deletion — it terminates Karpenter-managed EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes before CloudFormation removes the roles and cluster. No manual cleanup is needed for these resources. Review pipeline SQS queues, the review DLQ remediation Lambda, and the review audit log group are also deleted with the stack.

Note: All S3 buckets (ingest, clean, review, quarantine) have `DeletionPolicy: Retain` and are **not deleted** with the stack. This prevents accidental data loss — scanned files, review items, and quarantined malware are preserved for forensic investigation or audit. The ECR repository and its images **are** deleted with the stack. To clean up retained buckets after stack deletion:

```bash
# List retained buckets (names are in the stack outputs, saved before deletion)
aws s3 rb s3://<ingest-bucket> --force
aws s3 rb s3://<clean-bucket> --force
aws s3 rb s3://<review-bucket> --force
aws s3 rb s3://<quarantine-bucket> --force
```
