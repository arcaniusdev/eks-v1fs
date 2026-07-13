# EKS Vision One File Security Scanner

Malware scanning on AWS, deployed the way TrendAI supports it. A single CloudFormation template provisions an EKS cluster running [TrendAI Vision One File Security](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-intro-origin) — and, optionally, a complete S3 scanning pipeline where files uploaded to an S3 bucket are automatically scanned and routed to a clean bucket or quarantine bucket based on the verdict.

Everything deploys from one template — the EKS cluster, networking, storage, the scanner itself, and (if enabled) the queues, buckets, IAM, and scanning application.

## What is Vision One File Security?

Vision One File Security is TrendAI's malware scanning service for files. It uses multiple detection engines — pattern matching, heuristics, and predictive machine learning (PML) — to identify threats in files of any type.

In this project, the scanner runs **inside the Kubernetes cluster** as a set of pods deployed via the official Helm chart. Scanning applications communicate with these pods over gRPC to scan files, and the scanner pods phone home to the Vision One cloud for threat intelligence updates and to report results.

This means files are scanned locally within your VPC — they are never uploaded to an external service.

For more information, see the [Vision One File Security Helm chart repository](https://trendmicro.github.io/visionone-file-security-helm/).

## Architecture Realignment (July 2026)

This deployment was realigned with **TrendAI's supported deployment methodology** so that evaluations deploy a configuration TrendAI can support, rather than a custom high-performance variant. If you are evaluating Vision One File Security, what you deploy here is what TrendAI documents and supports.

What changed:

- **The V1FS scanner scales with the Helm chart's own HPA** (`scanner.autoscaling.enabled=true`, CPU 80% / memory 80% targets) — the Trend-supported autoscaling mechanism. KEDA no longer touches the chart-owned scanner; it scales only our optional scanner-app.
- **Karpenter was removed.** The cluster uses one managed node group with the standard Kubernetes Cluster Autoscaler — the conventional, supported node-scaling path.
- **The S3/SQS scanning application is now an optional module** (`DeployScannerApp`, default on). Turn it off and the stack is a clean TrendAI V1FS deployment plus a scanner endpoint for your own application.
- **The review pipeline is optional and off by default** (`DeployReviewPipeline`). Without it, files that exceed decompression limits are quarantined with explanatory tags rather than passed as clean.
- **The Helm chart is pinned to version 1.4.10**, with all custom values consolidated in `helm/values-base.yaml` — the single source of truth for install and upgrades.
- **New deployment options**: scan an existing S3 bucket you already own (`ExistingIngestBucket`), and expose the scanner endpoint via an internal NLB (default) or a TLS ALB (`ScannerEndpointMode`).

**Scaling expectations changed with it.** The chart HPA plus Cluster Autoscaler reacts in roughly 1–3 minutes (HPA metrics window + node provisioning), which is normal, expected evaluation behavior. The previous architecture's high-scale benchmarks (150-pod fleets, thousands of concurrent scans) no longer apply — this deployment favors supportability over peak throughput.

**There is no in-place migration** from pre-realignment (Karpenter-era) stacks. Delete the old stack and deploy a new one.

## Deployment Modes

Four parameter combinations cover the common cases:

| Mode | DeployScannerApp | DeployReviewPipeline | ExistingIngestBucket | ScannerEndpointMode | What you get |
|---|---|---|---|---|---|
| **Default (full auto-scan)** | `true` | `false` | *(empty)* | `nlb` | Complete pipeline: new ingest bucket → SQS → scanner-app → clean/quarantine. Decompression-limit files are quarantined with tags. Scanner endpoint also published for ad-hoc use. |
| **Endpoint-only (bring your own app)** | `false` | `false` | — | `nlb` or `alb` | Just the TrendAI V1FS scanner on EKS plus a gRPC endpoint. No buckets, queues, ECR, scanner-app IAM, DLQ Lambdas, dashboard, or KEDA. Connect your own application via the SDK. |
| **Existing bucket** | `true` | `false` | *your bucket name* | any | Scans a bucket you already own via S3 → EventBridge → SQS. Source objects are **tagged** with the verdict, never deleted. Verdict copies still land in clean/quarantine. |
| **Full + review pipeline** | `true` | `true` | *(empty or set)* | any | Adds a second V1FS release with unlimited decompression that re-scans archives exceeding the main scanner's limits before a final clean/quarantine verdict. |

CloudFormation Rules enforce the valid combinations: `DeployReviewPipeline=true` requires `DeployScannerApp=true`, and `ScannerEndpointMode=alb` requires both `ACMCertificateArn` and `ScannerDomain`.

## How It Works

The core of every deployment is the TrendAI V1FS scanner, installed via the official Helm chart and scaled by the chart's own HPA. The scanning application and review pipeline are optional modules layered on top:

```
                        ┌────────────────────────────────────────────────────┐
                        │ EKS CLUSTER                                        │
                        │                                                    │
  Your own app ─────────┼──▶ NLB / ALB ──▶ TrendAI V1FS Scanner              │
  (SDK over gRPC)       │   (optional        Helm chart 1.4.10               │
                        │    endpoint)       chart HPA: 1–10 pods            │
                        │                    (CPU 80% / memory 80%)          │
                        │                        ▲ gRPC :50051               │
                        │                        │                           │
                        │  ┌─────────────────────┴──────────────────────┐    │
                        │  │ OPTIONAL: scanner-app module               │    │
  S3 Ingest ──▶ SQS ────┼──┼─▶ scanner-app pods (KEDA, 1–20)            │    │
  (created or           │  │   route to Clean / Quarantine buckets      │    │
   existing bucket)     │  └────────────────────────────────────────────┘    │
                        │                                                    │
                        │  ┌────────────────────────────────────────────┐    │
                        │  │ OPTIONAL: review pipeline                  │    │
  Review bucket ─▶ SQS ─┼──┼─▶ review-scanner-app ─▶ V1FS "rv" release  │    │
                        │  │   (no decompression limits)                │    │
                        │  └────────────────────────────────────────────┘    │
                        └────────────────────────────────────────────────────┘
```

With the scanner-app module enabled (the default), the full pipeline looks like this:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ MAIN PIPELINE (decompression limits enforced)                            │
│                                                                          │
│  S3 Ingest ──▶ SQS Queue ──▶ Scanner App ──▶ V1FS Scanner (gRPC)         │
│                    │                               │                     │
│                    ▼                     ┌─────────┼──────────┐          │
│              DLQ (3 fails)             CLEAN   DECOMP-LIMIT  MALICIOUS   │
│                    │                     │         │            │        │
│             Lambda auto-retry            ▼         │            ▼        │
│            (backoff → discard)     Clean Bucket    │      Quarantine     │
│                                                    ▼                     │
│              review OFF (default): Quarantine + explanatory tags        │
│              review ON:            Review Bucket ──▶ review pipeline    │
├──────────────────────────────────────────────────────────────────────────┤
│ REVIEW PIPELINE (optional, no decompression limits)                      │
│                                                                          │
│  Review Bucket ──▶ Review SQS ──▶ Review Scanner ──▶ V1FS rv (no limits) │
│                        │                                  │              │
│                  Review DLQ                        ┌──────┴──────┐       │
│               Lambda auto-retry                  CLEAN       MALICIOUS   │
│                                                    │            │        │
│                                                    ▼            ▼        │
│                                              Clean Bucket  Quarantine    │
└──────────────────────────────────────────────────────────────────────────┘
```

| Step | What happens |
|---|---|
| **Ingest** | A file lands in the ingest bucket (stack-created, or your existing bucket). S3 sends an `ObjectCreated` event to the SQS queue — directly for a stack-created bucket, or via EventBridge for an existing bucket. |
| **Scan** | A scanner-app pod long-polls the queue, downloads the file into memory, and scans it via the V1FS Python SDK over gRPC to the in-cluster scanner pods. |
| **Route** | The file is copied to the destination bucket with a `ScanResult` tag. For a stack-created ingest bucket, the source object is then deleted; for an existing bucket, the source object is **tagged with the verdict and left in place**. The SQS message is removed and the result is written to the CloudWatch audit trail. |
| **Review** *(optional)* | If the review pipeline is deployed, archives the main scanner could not fully analyze are re-scanned by a second V1FS release (`rv`) with no decompression limits, then routed to Clean or Quarantine. |

**Routing rules:**

| Verdict | Condition | Destination |
|---|---|---|
| **Clean** | `scanResult == 0`, no decompression errors | Clean Bucket (tag `ScanResult=S3-Clean`) |
| **Malicious** | `scanResult > 0` | Quarantine Bucket (tag `ScanResult=S3-Malware`) |
| **Decompression limit — review OFF (default)** | `scanResult == 0`, decompression limit exceeded | Quarantine Bucket with tags `ScanResult=S3-DecompressionLimit` and `ScanErrors=<error names>` |
| **Decompression limit — review ON** | `scanResult == 0`, decompression limit exceeded | Review Bucket → re-scanned by review pipeline |
| **Oversize — review OFF (default)** | File exceeds `MAX_FILE_SIZE_MB` (default 500) | Quarantine Bucket via server-side copy (tag `ScanResult=S3-Oversize`) |
| **Oversize — review ON** | File exceeds `MAX_FILE_SIZE_MB` | Review Bucket via server-side copy, re-scanned with no size limit |

A file that hit a decompression limit was **not fully inspected** — treating it as clean would be unsafe. Quarantining it with explanatory tags (rather than passing it through) also fixes a bug in the previous architecture where such files could land in the clean bucket when the review pipeline was unavailable. The `ScanErrors` tag records exactly which limit was hit (`ATSE_ZIP_RATIO_ERR`, `ATSE_MAXDECOM_ERR`, `ATSE_ZIP_FILE_COUNT_ERR`, or `ATSE_EXTRACT_TOO_BIG_ERR`) so an operator can decide whether to raise the limits or deploy the review pipeline.

### Scanner Application (optional module)

A Python asyncio application built for efficiency. Scan requests use **gRPC** — a binary protocol with lower latency, smaller payloads, and native streaming compared to REST. Files are scanned entirely in memory via `scan_buffer()`, eliminating disk I/O from the critical path.

- **gRPC-native scanning** — binary protocol with persistent HTTP/2 connections to in-cluster scanner pods, avoiding the overhead of REST serialization and per-request TCP handshakes
- **Fully async pipeline** — `aiobotocore` for S3/SQS operations and `amaas.grpc.aio` for scan requests, all running concurrently on a single event loop with zero thread-blocking
- **In-memory scanning** — files are downloaded as byte buffers and passed directly to the scanner over gRPC, never written to disk
- **50 concurrent scans per pod** — each pod maintains 50 in-flight scan requests simultaneously (configurable via `MAX_CONCURRENT_SCANS`)
- **Visibility heartbeat** — automatically extends SQS message visibility during long-running scans to prevent duplicate processing. On failure, immediately shortens visibility to 30 seconds for fast retry by another pod
- **Health probes** — liveness (`/healthz`) and readiness (`/readyz`) endpoints on port 8080. Liveness catches deadlocked event loops (pod is restarted); readiness gates traffic until the gRPC scan handle is initialized
- **Scan audit trail** — every scan result is written to CloudWatch Logs as structured JSON (file key, size, verdict, malware names, SHA256, scan duration, pod name), batched for efficiency
- **Pull-based load distribution** — no load balancer needed. All scanner-app pods long-poll the same SQS queue, and SQS delivers each message to exactly one consumer. Pods that are free pull more messages; pods that are busy naturally slow their polling via backpressure (semaphore + 2x in-flight cap). gRPC connections to V1FS scanner pods are distributed by the Kubernetes Service at connection setup time
- **Graceful shutdown** — handles SIGTERM to drain in-flight scans and flush audit entries before exiting (5-minute grace period)
- **Predictive Machine Learning** — PML can be enabled for advanced threat detection (requires account-level PML support)

Set `DeployScannerApp=false` if you already have a scanning application — the stack then deploys only the TrendAI V1FS scanner and publishes its endpoint. See [Connect Your Own Scanning Application](#connect-your-own-scanning-application).

### Database & Storage

The Vision One File Security management service uses a **PostgreSQL database** deployed as a Kubernetes StatefulSet within the cluster. The database stores scan metadata, configuration, and operational state.

- **PostgreSQL StatefulSet** — deployed by the V1FS Helm chart with `dbEnabled: true`
- **EBS gp3 storage** — 100Gi encrypted persistent volume for database data, provisioned by the EBS CSI Driver
- **EFS shared storage** — 100Gi ReadWriteMany volume for scanner ephemeral files, provisioned by the EFS CSI Driver. Multiple scanner pods across different nodes share this storage simultaneously, eliminating the single-node bottleneck of block storage
- **StorageClasses** — `gp3` (EBS, block storage, ReadWriteOnce) for the database and `efs-sc` (EFS, network filesystem, ReadWriteMany) for scanner ephemeral volumes

The database configuration is immutable after initial deployment — changing storage class or size requires deleting and recreating the StatefulSet.

### Compute

The cluster uses a **single managed node group** for everything — system components and scanner workloads. The default instance type is `r7i.xlarge` (4 vCPU, 32 GiB); memory-optimized instances give the V1FS scanner headroom for signature databases and in-memory file analysis, and each xlarge node fits four scanner pods (800m CPU / 2Gi memory each — the chart default) alongside scanner-app and system pods.

Node group sizing (all configurable via parameters):

| Setting | Default | Notes |
|---|---|---|
| `NodeGroupMinSize` | 2 | Minimum 2 for CoreDNS/AZ redundancy |
| `NodeGroupDesiredSize` | 2 | Starting point — Cluster Autoscaler adjusts from here |
| `NodeGroupMaxSize` | 8 | Fits full-mode peak load (see math below) |

**Why max 8 nodes:** an `r7i.xlarge` has 4 vCPU, of which roughly **3.6 vCPU is usable** after kubelet/system reservations and per-node DaemonSets. Full-mode peak pod requests total approximately **26.4 vCPU** — 10 V1FS scanner pods × 800m, 20 scanner-app pods × 500m, the review pipeline, and system components (CoreDNS, KEDA, CSI drivers, Cluster Autoscaler, LB controller, metrics server). 26.4 ÷ 3.6 ≈ 7.4, so 8 nodes covers peak with a margin. If you raise the replica maximums, raise `NodeGroupMaxSize` to match.

### Autoscaling

Scaling is deliberately conventional — each component scales by the mechanism its vendor supports:

**V1FS scanner pods — chart-native HPA.** The Helm chart's built-in HorizontalPodAutoscaler (`scanner.autoscaling.enabled=true`) scales scanner pods on **CPU 80% / memory 80%** utilization targets. This is the autoscaling mechanism TrendAI documents and supports. Bounds are set by the `ScannerMinReplicas` / `ScannerMaxReplicas` CloudFormation parameters (default 1–10). The Metrics Server (installed by the bootstrap) provides the utilization metrics. **KEDA never touches the chart-owned scanner.**

**scanner-app pods — KEDA on SQS depth** *(only when `DeployScannerApp=true`)*. Our scanning application is queue-driven, so it scales on queue depth: KEDA polls the SQS queue every 5 seconds and adds 1 pod per 5 queued messages (in-flight messages included), from 1 up to `ScannerAppMaxReplicas` (default 20), with a 300-second cooldown. KEDA gets its SQS read permissions via Pod Identity — no access keys.

**Review pipeline** *(only when `DeployReviewPipeline=true`)*. The review-scanner-app scales via KEDA on the review queue (1–5 pods); the `rv` V1FS release scales via its own chart HPA (1–3 pods). One pod of each stays warm to avoid cold-start gRPC failures.

**Nodes — Cluster Autoscaler.** The standard Kubernetes Cluster Autoscaler (installed via Helm by the bootstrap, IAM via Pod Identity) discovers the managed node group's Auto Scaling Group through the `k8s.io/cluster-autoscaler` tags that EKS applies automatically. When pods can't be scheduled, it grows the ASG (up to `NodeGroupMaxSize`); it scales down nodes unneeded for 2 minutes, preferring the least-waste expander. PodDisruptionBudgets (`k8s/pdb.yaml`) protect active scan workloads during scale-down.

**Expected scale-up latency: 1–3 minutes.** The HPA reacts to its metrics window, and if a new node is needed, the Cluster Autoscaler provisions it through the ASG. This is normal behavior for a supported evaluation configuration. If a burst of files arrives, the SQS queue simply holds the backlog — nothing is lost — and drains as capacity comes online. (The previous architecture's headline numbers — 150-pod fleets, 7,500 concurrent scans, sub-minute node provisioning — belonged to the custom Karpenter/KEDA design and no longer apply.)

```
Files arrive in S3 → SQS queue depth rises
         |
         v
KEDA scales scanner-app (1 → up to 20 pods)
         |
         v
Scan load raises V1FS scanner CPU/memory
         |
         v
Chart HPA scales V1FS scanner (1 → up to 10 pods)
         |
         v
Pending pods trigger Cluster Autoscaler → ASG adds nodes (up to 8)
         |
         v
Queue drains → KEDA and HPA scale down → Cluster Autoscaler removes idle nodes
```

### Failure Handling

If a scan fails (gRPC error, download failure, or any transient exception), the scanner immediately shortens the SQS message visibility timeout to 30 seconds, making it available for another pod to pick up almost immediately. Without this, failed messages would stay invisible for the full visibility timeout (600 seconds) before being retried — a 10-minute delay for what might be a momentary network blip. The fast retry ensures transient failures recover in seconds, not minutes.

After 3 consecutive failures, the message moves to a Dead Letter Queue. A Lambda function automatically re-queues DLQ messages with exponential backoff (60s → 300s → 900s). After 3 DLQ retries (9 total scan attempts), the message is logged as a permanent failure and discarded. If the review pipeline is deployed, it has its own independent DLQ and remediation Lambda.

```
Scan fails → visibility shortened to 30s → fast retry by another pod
  ↓ (3 failures)
DLQ → Lambda re-queues with backoff (60s → 300s → 900s)
  ↓ (3 DLQ retries = 9 total attempts)
Permanent failure logged and discarded
```

### Orphaned File Reconciliation

A reconciliation loop monitors the ingest bucket for orphaned files — objects that were uploaded but never processed due to transient failures such as scanner pod restarts or SQS message expiration. Every 5 minutes it lists the ingest bucket and sends a synthetic SQS message for any file older than 30 minutes, re-entering it into the main scan pipeline. This ensures no file is silently dropped, even if every retry mechanism in the normal flow has been exhausted. The interval and age threshold are configurable via `RECONCILIATION_INTERVAL` and `RECONCILIATION_AGE_THRESHOLD`.

Where it runs depends on the deployment mode:

- **Review pipeline deployed** — the review scanner-app runs the loop (it is always warm).
- **Review pipeline off (default)** — the main scanner-app runs the loop.
- **Existing-bucket mode** — reconciliation is **disabled**: scanned objects legitimately remain in your bucket (they are tagged, not deleted), so "old object still present" is not a failure signal.

## Scanner Endpoint Exposure

The `ScannerEndpointMode` parameter controls how the V1FS scanner's gRPC endpoint is exposed to scanning applications outside the cluster:

| Mode | What it creates | Connection |
|---|---|---|
| `nlb` *(default)* | An **internal Network Load Balancer** via the chart's `externalService` (`helm/values-nlb.yaml`), exposing gRPC on `:50051` and ICAP on `:1344`. VPC-internal only — reachable from your VPC or peered networks, never from the internet. | Plaintext gRPC in-VPC |
| `alb` | An **ALB Ingress** using TrendAI's documented topology — the chart's scanner ingress with `backend-protocol-version: GRPC`, internal scheme, and TLS terminated with your ACM certificate. Requires `ACMCertificateArn` and `ScannerDomain`; after deployment, **you create a DNS CNAME** from `ScannerDomain` to the ALB hostname (the bastion log prints the exact record). | TLS gRPC on `:443` |
| `none` | Nothing — the scanner is reachable only inside the cluster via its ClusterIP service. | In-cluster only |

Both load balancers are created by the AWS Load Balancer Controller (installed by the bootstrap) and cleaned up automatically on stack deletion.

The bastion publishes the resolved endpoint address to SSM Parameter Store at **`/<stack-name>/scanner-endpoint`** — e.g. `internal-xyz.elb.us-east-1.amazonaws.com:50051` (NLB) or `scanner.example.com:443` (ALB).

## Connect Your Own Scanning Application

With `DeployScannerApp=false` (or alongside the built-in pipeline), connect any application that uses the [V1FS Python SDK](https://pypi.org/project/visionone-filesecurity/) to the published endpoint.

Look up the endpoint:

```bash
aws ssm get-parameter --name "/<stack-name>/scanner-endpoint" \
  --query Parameter.Value --output text
```

Initialize the SDK against it:

```python
import amaas.grpc

# NLB mode (default) — plaintext gRPC inside the VPC
handle = amaas.grpc.init("<nlb-hostname>:50051", api_key, False)

# ALB mode — TLS via your ACM certificate and domain
handle = amaas.grpc.init("<scanner-domain>:443", api_key, True)

result = amaas.grpc.scan_file(handle, "/path/to/file")
amaas.grpc.quit(handle)
```

The `api_key` is your Vision One API key with "Run file scan via SDK" permission (the same key the stack stores in Secrets Manager). The async variant (`amaas.grpc.aio`) takes the same arguments. Your application must run in (or have network reachability to) the VPC for NLB mode; ALB mode additionally requires the DNS CNAME described above.

## Scanning an Existing S3 Bucket

Set `ExistingIngestBucket` to the name of a bucket you already own and the stack scans it instead of creating a new ingest bucket.

**How it's wired:** S3 → **EventBridge** → SQS. An EventBridge rule filtered on your bucket forwards `Object Created` events to the scan queue. A custom-resource Lambda **merges** `EventBridgeConfiguration` into your bucket's existing notification configuration — your current event notifications (Lambda triggers, SNS topics, other queues) are preserved, never clobbered. Stack deletion never touches the bucket: no objects, tags, or notification configuration are removed.

**Prerequisites:**

- The bucket must exist in the same region and account as the stack.
- Any existing notification configuration is fine — EventBridge delivery is additive.

**Semantics (tag, don't delete):**

- Source objects are **never deleted**. After scanning, the object in your bucket is tagged with the verdict (`ScanResult=S3-Clean`, `S3-Malware`, `S3-DecompressionLimit` + `ScanErrors=...`, or `S3-Oversize`), so results are visible in place.
- Verdict **copies** still land in the stack's clean/quarantine buckets (and review bucket, if deployed), preserving the routed-output workflow.
- **Reconciliation is disabled** — objects legitimately remain in the bucket, so age-based re-queueing would loop.
- Defense in depth: the scanner-app IAM role has **no `s3:DeleteObject`** on your bucket — the pipeline cannot delete your data even if misconfigured.

## Architecture

### Infrastructure (CloudFormation)

The `eks-v1fs.yaml` template creates everything. Resources marked *(scanner-app)* or *(review)* exist only when the corresponding module is enabled.

| Resource | Purpose |
|---|---|
| **VPC** | `10.2.0.0/16` with public and private subnets across 2 AZs |
| **NAT Gateways** | One per AZ — pods in private subnets reach the internet for threat intelligence updates |
| **EKS Cluster** | Private API endpoint, full audit logging, managed addons (vpc-cni, CoreDNS, kube-proxy, Pod Identity Agent, EBS CSI Driver, EFS CSI Driver) |
| **Managed Node Group** | Single node group of `r7i.xlarge` (default) instances in private subnets, min 2 / max 8 — hosts system components and scanner workloads, scaled by the Cluster Autoscaler |
| **AWS Load Balancer Controller** | Creates the internal NLB or ALB for the scanner endpoint (Helm, Pod Identity) |
| **Cluster Autoscaler** | Standard Kubernetes node autoscaler (Helm, Pod Identity, ASG auto-discovery via EKS-applied tags) |
| **EFS Filesystem** | Encrypted shared storage (ReadWriteMany) for the V1FS scanner ephemeral volume across multiple pods |
| **Secrets Manager** | Stores the V1FS registration token and API key |
| **Metrics Server** | Provides the CPU/memory metrics the chart HPA scales on |
| **SSM Parameter** | `/<stack-name>/scanner-endpoint` — the published scanner endpoint address |
| **ECR Repository** *(scanner-app)* | Hosts the scanner app container image, scan-on-push enabled |
| **S3 Buckets** *(scanner-app)* | Ingest (created unless `ExistingIngestBucket` is set), Clean, Quarantine, and Review *(review only)* — all stack-created buckets have `DeletionPolicy: Retain` |
| **EventBridge Wiring** *(existing-bucket mode)* | Events rule filtered on your bucket + a custom-resource Lambda that merges `EventBridgeConfiguration` into the bucket's notification config |
| **SQS Queues** *(scanner-app)* | Main queue (600s visibility timeout, 20s long polling) + Dead Letter Queue; review queue + review DLQ *(review only)* |
| **DLQ Remediation Lambda(s)** *(scanner-app)* | Re-queues failed messages with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard |
| **CloudWatch Alarms** *(scanner-app)* | DLQ messages (any > 0), queue age (> 20 min for 5 consecutive minutes) via SNS topic |
| **Scan Audit Log** *(scanner-app)* | CloudWatch log group with structured JSON per scan, 30-day retention; separate `review-audit-${StackName}` group *(review only)* |
| **CloudWatch Dashboard** *(scanner-app)* | Queue health, scan throughput/latency, malware detection stats, DLQ remediation, recent scan results |
| **KEDA** *(scanner-app)* | Scales the scanner-app deployment on SQS queue depth — never the chart-owned V1FS scanner |
| **V1FS Review Release** *(review)* | Second Helm release (`rv`) in `visionone-review` with no CLISH scan policy — unlimited decompression for deep analysis |
| **IAM Roles** | Least-privilege roles for nodes, bastion, Cluster Autoscaler, LB controller, and (when enabled) scanner app, KEDA operator, and DLQ remediation |
| **Pod Identity** | Binds IAM roles to Kubernetes service accounts — no access keys needed |
| **Pre-delete Cleanup Lambda** | Runs during stack deletion — removes LB-controller-created load balancers, target groups, and security groups, and deletes orphaned EBS volumes |
| **Bastion Host** | Exports environment, clones this repo, and runs `scripts/bootstrap.sh` to provision the cluster |

### How Credentials Work

Pods get AWS permissions automatically through EKS Pod Identity. No access keys are configured anywhere.

1. CloudFormation creates IAM roles with permissions scoped to specific resources — `ScannerAppRole` for the main scanner-app (ingest/clean/quarantine buckets, main SQS queue; no `s3:DeleteObject` on an existing user bucket) and, if the review pipeline is deployed, `ReviewScannerAppRole` (review/clean/quarantine buckets, review SQS queue — deliberately no write access to the review bucket)
2. Pod Identity Associations bind each role to its Kubernetes service account (`scanner-app` in `visionone-filesecurity`, `review-scanner-app` in `visionone-review`, plus the Cluster Autoscaler, LB controller, and KEDA service accounts in their namespaces)
3. The Pod Identity Agent (a DaemonSet on each node) intercepts credential requests from pods and injects temporary credentials
4. Each scanner app retrieves the V1FS API key from Secrets Manager at startup using these credentials

## Prerequisites

You need one credential from the TrendAI Vision One console:

1. **API Key** — used by the scanning application to authenticate scan requests, and to auto-fetch the scanner registration token at deploy time. Generate this under **Administration > API Keys > Add API Key** with the **"Run file scan via SDK"** permission.

The **registration token** (used by scanner pods to register with Vision One) is fetched automatically during deployment via the Vision One API (`POST /beta/fileSecurity/ctr/registration`) using your API key — no manual step needed. If your API key cannot mint registration tokens, generate one manually under **Cloud Security > File Security > Containerized Scanner > Get ready to deploy containerized scanner > Get registration token** and pass it as the `RegistrationToken` parameter. Tenants outside the US region should set `VisionOneApiEndpoint` to their regional API host.

If you choose `ScannerEndpointMode=alb`, you also need an **ACM certificate** covering your chosen scanner domain, and the ability to create a DNS CNAME record for it.

## Deployment

### Launch the stack

Download the `eks-v1fs.yaml` CloudFormation template and deploy it in AWS CloudFormation. The template requires one parameter:

- **ApiKey** — your Vision One API key with "Run file scan via SDK" permission

(**RegistrationToken** is optional — leave it empty and the deployment mints one automatically from the Vision One API using your ApiKey. **VisionOneApiEndpoint** defaults to the US-region host `https://api.xdr.trendmicro.com`.)

Optional parameters:

| Parameter | Default | Description |
|---|---|---|
| **PrimaryAZ** | `us-east-1a` | Availability Zone 1 |
| **SecondaryAZ** | `us-east-1b` | Availability Zone 2 |
| **NodeInstanceType** | `r7i.xlarge` | EC2 instance type for the managed node group (`r7i.xlarge`, `r7a.xlarge`, `r6i.xlarge`, or `r7i.2xlarge`). One node group hosts both system components and scanner workloads |
| **NodeGroupMinSize** | `2` | Minimum nodes in the managed node group (min 2 for CoreDNS/AZ redundancy) |
| **NodeGroupDesiredSize** | `2` | Initial desired nodes — the Cluster Autoscaler adjusts from here |
| **NodeGroupMaxSize** | `8` | Maximum nodes the Cluster Autoscaler may scale to. The default fits full-mode peak load on r7i.xlarge |
| **ScannerMinReplicas** | `1` | Minimum V1FS scanner pods (chart HPA `minReplicas`) |
| **ScannerMaxReplicas** | `10` | Maximum V1FS scanner pods (chart HPA `maxReplicas`, CPU/memory 80% targets) |
| **DeployScannerApp** | `true` | Deploy the S3/SQS scanning application module (buckets, queues, ECR, scanner-app pods). Set to `false` to deploy only the V1FS scanner and its endpoint |
| **ScannerAppMaxReplicas** | `20` | Maximum scanner-app pods (KEDA SQS-driven scaling) |
| **DeployReviewPipeline** | `false` | Deploy the review pipeline (second V1FS release with unlimited decompression). Requires `DeployScannerApp=true`. When `false`, files exceeding decompression limits are quarantined with explanatory tags |
| **ExistingIngestBucket** | *(empty)* | Name of an existing S3 bucket to scan. Leave empty to create a new ingest bucket. See [Scanning an Existing S3 Bucket](#scanning-an-existing-s3-bucket) |
| **ScannerEndpointMode** | `nlb` | Scanner endpoint exposure: `nlb` (internal NLB), `alb` (TLS ALB Ingress), or `none` |
| **ACMCertificateArn** | *(empty)* | ACM certificate ARN for the scanner ALB (required when `ScannerEndpointMode=alb`) |
| **ScannerDomain** | *(empty)* | DNS name for the scanner ALB, e.g. `scanner.example.com` (required when `ScannerEndpointMode=alb`) |
| **ExistingVpcId** | *(empty)* | Deploy into an existing VPC instead of creating one. When set, `ExistingVpcCidr` and all three subnet parameters are required. See [Deploying into an Existing VPC](#deploying-into-an-existing-vpc) |
| **ExistingVpcCidr** | *(empty)* | CIDR block of the existing VPC (used for scanner endpoint security group rules) |
| **ExistingPrivateSubnet1Id** / **ExistingPrivateSubnet2Id** | *(empty)* | Two private subnets in different AZs (EKS nodes, EFS mount targets) |
| **ExistingBastionSubnetId** | *(empty)* | Subnet for the bastion host — private is fine (access is SSM-only; it needs only outbound internet) |
| **PMLEnabled** | `false` | Enable Predictive Machine Learning scanning (requires account support) |
| **MaxFileSizeMB** | `500` | Maximum file size in MB the main scanner will download and scan (1–2048). Larger files are routed via server-side S3 copy — to the review bucket if the review pipeline is deployed, otherwise to quarantine with `ScanResult=S3-Oversize` |
| **ScanTimeoutSeconds** | `600` | gRPC scan timeout; also sets the SQS visibility timeout to match |

CloudFormation Rules validate the combinations at stack creation: `DeployReviewPipeline=true` asserts `DeployScannerApp=true`, `ScannerEndpointMode=alb` asserts both `ACMCertificateArn` and `ScannerDomain` are set, and `ExistingVpcId` asserts the CIDR and all three subnet parameters are set.

#### Deploying into an Existing VPC

By default the stack creates its own network (VPC `10.2.0.0/16`, two public + two private subnets, an internet gateway, and two NAT gateways). Set `ExistingVpcId` (plus `ExistingVpcCidr`, `ExistingPrivateSubnet1Id`, `ExistingPrivateSubnet2Id`, and `ExistingBastionSubnetId`) to deploy into a network you already manage — none of those resources are created, and the AZ parameters are ignored.

Your VPC must provide what the created network normally would:

- **DNS support and DNS hostnames enabled** on the VPC
- **Two private subnets in different AZs** with **outbound internet access** (NAT gateway or proxy) — the scanner must reach Vision One for registration, threat updates, and telemetry
- The `kubernetes.io/role/internal-elb=1` tag on both private subnets, so the AWS Load Balancer Controller can place the internal scanner NLB/ALB
- **A bastion subnet with outbound internet** — private is fine; the bastion has no inbound rules and is reached exclusively through SSM Session Manager (in existing-VPC mode it gets no public IP)

The stack still creates its own security groups inside your VPC, and teardown removes only what the stack created — your VPC and subnets are never modified.

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

### What the bastion does

You don't need to clone the repo to deploy. The bastion host's UserData is now minimal — it **exports the CloudFormation-derived environment variables, clones this repo, and runs `scripts/bootstrap.sh`**, which:

1. Installs kubectl, Helm, eksctl, and the AWS CLI, and configures kubeconfig
2. Creates the `visionone-filesecurity` namespace and registration-token secrets
3. Installs the AWS Load Balancer Controller, Metrics Server, and Cluster Autoscaler via Helm
4. Installs KEDA via Helm *(only when the scanner-app module is enabled)*
5. Creates the `gp3` (EBS) and `efs-sc` (EFS) StorageClasses
6. Installs the TrendAI Vision One File Security Helm chart — **pinned to version 1.4.10, GPG-verified**, using `helm/values-base.yaml` plus the endpoint overlay (`helm/values-nlb.yaml` or the ALB ingress values), with the chart's own HPA enabled
7. Configures the scan policy decompression limits via CLISH
8. Installs the second V1FS release (`rv`) with unlimited decompression *(only when the review pipeline is enabled)*
9. Builds the scanner-app Docker image, pushes it to ECR, and deploys the scanner-app (and review pipeline, if enabled) *(only when the scanner-app module is enabled)*
10. Waits for the load balancer hostname and publishes the scanner endpoint to SSM Parameter Store (`/<stack-name>/scanner-endpoint`)

If you want to further develop the application using [Claude Code](https://claude.ai/claude-code), clone the repo — it includes `CLAUDE.md` and supporting files in the `docs/` directory that provide Claude with comprehensive project context, architectural constraints, and operational guardrails.

Stack creation takes approximately 20-30 minutes. Monitor progress:

```bash
aws cloudformation describe-stacks --stack-name my-scanner --query 'Stacks[0].StackStatus'
```

### Stack Outputs

After creation, the stack exports key resource identifiers. Outputs for the scanner-app module and review pipeline exist only when those modules are enabled.

| Output | Description |
|---|---|
| `ClusterName` | EKS cluster name |
| `BastionPublicIP` | Bastion host IP (connect via SSM, not SSH) |
| `DashboardUrl` | CloudWatch dashboard URL — real-time pipeline monitoring *(scanner-app)* |
| `IngestBucketName` | S3 bucket for uploading files to scan *(scanner-app; your own bucket in existing-bucket mode)* |
| `CleanBucketName` | S3 bucket for files that passed scanning *(scanner-app)* |
| `QuarantineBucketName` | S3 bucket for malicious, decompression-limit, and oversize files *(scanner-app)* |
| `ReviewBucketName` | S3 bucket for files awaiting deep analysis *(review)* |
| `FileScanQueueUrl` | SQS queue URL for S3 file events *(scanner-app)* |
| `FileScanDLQUrl` | SQS dead letter queue URL *(scanner-app)* |
| `AlarmSNSTopicArn` | SNS topic for scan alarms — subscribe for notifications *(scanner-app)* |
| `ScanAuditLogGroupName` | CloudWatch log group for the scan audit trail *(scanner-app)* |
| `ReviewScanQueueUrl` / `ReviewScanDLQUrl` / `ReviewAuditLogGroupName` | Review pipeline queue, DLQ, and audit log group *(review)* |
| `ECRRepoUrl` | ECR repository for the scanner app image *(scanner-app)* |

The scanner endpoint address is published separately to SSM Parameter Store at `/<stack-name>/scanner-endpoint` (see [Connect Your Own Scanning Application](#connect-your-own-scanning-application)).

Retrieve any output:

```bash
aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`ClusterName`].OutputValue' --output text
```

### Verify

Connect to the bastion via Session Manager:

```bash
aws ssm start-session --target <instance-id>
```

Check that everything is running:

```bash
kubectl get pods -n visionone-filesecurity
kubectl get hpa -n visionone-filesecurity     # the chart's scanner HPA
```

You should see the V1FS scanner pods (and, if enabled, the `scanner-app` pod) in `Running` state, and one HorizontalPodAutoscaler targeting the chart scanner.

### Test

With the scanner-app module enabled, upload a file to the ingest bucket:

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

You should see the file scanned and routed to the clean bucket (in existing-bucket mode, the source object is also tagged with `ScanResult=S3-Clean`).

To test malware detection, upload the [EICAR test file](https://www.eicar.org/download-anti-malware-testfile/):

```bash
echo 'X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' \
  > /tmp/eicar.txt
aws s3 cp /tmp/eicar.txt s3://$INGEST_BUCKET/
```

This should be flagged as malicious and routed to the quarantine bucket.

In endpoint-only mode (`DeployScannerApp=false`), test with the SDK against the published endpoint instead — see [Connect Your Own Scanning Application](#connect-your-own-scanning-application).

## Project Structure

```
eks-v1fs.yaml              CloudFormation template (all infrastructure)
helm/
  values-base.yaml         V1FS Helm values — single source of truth for install and upgrades
  values-nlb.yaml          Overlay: internal NLB endpoint via the chart's externalService
app/
  Dockerfile               python:3.11-slim, non-root UID 999
  requirements.txt         Pinned dependencies (visionone-filesecurity, aiobotocore, boto3)
  scanner.py               Async SQS polling, S3 download, V1FS scan, routing/tagging, health server, audit trail
  config.py                Environment variable loading and validation
k8s/
  serviceaccount.yaml      Kubernetes ServiceAccount (Pod Identity, no annotations)
  configmap.yaml           Environment config template (populated by deploy script)
  deployment.yaml          Hardened pod spec (non-root, read-only fs, drop all capabilities)
  networkpolicy.yaml       Egress restricted to DNS, V1FS scanner, and AWS HTTPS
  pdb.yaml                 PodDisruptionBudgets (protect active scans during node scale-down)
  scaledobject.yaml        KEDA ScaledObject + TriggerAuthentication — scales ONLY scanner-app
  review-serviceaccount.yaml  Review scanner ServiceAccount (Pod Identity, no annotations)
  review-deployment.yaml      Review scanner deployment (same image, reconciliation enabled)
  review-networkpolicy.yaml   Egress restricted to DNS, rv V1FS scanner, AWS HTTPS
  review-scaledobject.yaml    KEDA ScaledObject for the review scanner-app (min 1, max 5)
scripts/
  bootstrap.sh             Bastion provisioning — all cluster setup, driven by deployment-mode env vars
  build-and-push.sh        Build Docker image and push to ECR (tagged with git SHA)
  deploy.sh                Template ConfigMap from stack outputs and apply k8s manifests
  upgrade.py               Safely upgrade the V1FS Helm release(s), preserving values and HPA bounds
```

## Redeploying the Scanner App

To update the custom scanner-app code (Python application in `app/`), connect to the bastion via Session Manager and run:

```bash
cd /opt/eks-v1fs && git pull
export CFN_STACK_NAME=my-scanner
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
/opt/eks-v1fs/scripts/deploy.sh --review   # only if the review pipeline is deployed
```

This rebuilds the Docker image, pushes it to ECR, and re-applies the k8s manifests. The main and review scanner apps use the same Docker image — only the environment variables differ. This does **not** update the V1FS scanner — see [Updating the V1FS Scanner](#updating-the-v1fs-scanner) for that.

## Updating the V1FS Scanner

The TrendAI Vision One File Security scanner is installed via its official Helm chart, pinned to a known-good version (currently **1.4.10**). All custom values live in **`helm/values-base.yaml`** — the single source of truth used by both the initial install (`scripts/bootstrap.sh`) and upgrades (`scripts/upgrade.py`). The deviations from chart defaults are deliberately minimal and all documented chart options, keeping the deployment within TrendAI's supported methodology:

| Chart default | Our value (`values-base.yaml`) | Why |
|---|---|---|
| No ephemeral volume config | EFS-backed shared ephemeral volume (`efs-sc`, ReadWriteMany, 100Gi) | Scanner pods on multiple nodes share scratch space |
| Chart-created storage class | `gp3` StorageClass (CloudFormation-created) for the database PVC | Encrypted EBS gp3 |
| Scanner ingress enabled (inert) | Explicitly disabled — ALB mode re-enables it with its own settings | Endpoint exposure is controlled by `ScannerEndpointMode` |
| Management-service ingress enabled | Explicitly disabled | Never expose the management service |

The chart's own HPA (`scanner.autoscaling.enabled=true`) and scanner resources (800m CPU / 2Gi memory) are now **chart defaults** — no longer overridden. HPA min/max replicas come from the CloudFormation parameters at install time and are read from the live cluster at upgrade time.

### Upgrade procedure

Connect to the bastion host via AWS Systems Manager Session Manager and run the upgrade script:

```bash
python3 /opt/eks-v1fs/scripts/upgrade.py
```

The script handles everything automatically:

1. Sets up the environment (KUBECONFIG, Helm repository) and **discovers the installed releases** — `my-release` always; `rv` only if the review pipeline is installed
2. Captures the current CLISH scan policy before upgrading
3. Updates the Helm repository, shows available versions, and exits early if already up to date
4. For each release, **captures its install-time values (`helm get values`) and the live HPA min/max replicas**, then upgrades with `helm/values-base.yaml` + the captured values + the live HPA bounds — so operator tuning (e.g. a raised `maxReplicas`) survives the upgrade
5. Re-applies the captured CLISH scan policy to `my-release` only (not `rv` — it runs with unlimited decompression)
6. Verifies the **chart's HPA exists** for each release, and that no KEDA ScaledObject targets the chart-owned scanner (KEDA must scale only our scanner-app)
7. Verifies scanner pods are running
8. Runs a sanity scan — via the ingest bucket if the scanner-app module is deployed, otherwise it prints the published SSM endpoint and an SDK one-liner for a manual check

To pin a specific version (e.g., rollback or skip a release), use `--version X.Y.Z`. Other options: `--dry-run` to preview without executing, `--skip-sanity` to skip the test scan.

**Do not run `helm upgrade` manually without `-f helm/values-base.yaml`** — a plain `helm upgrade` reverts to chart defaults, losing the EFS ephemeral volume, the database storage class, and the ingress settings. Do not use `--reuse-values` — if the new chart version renames or adds values, it can cause silent misconfiguration.

These resources are **not affected** by `helm upgrade` and do not need re-applying: the KEDA ScaledObject (scanner-app), PodDisruptionBudgets, Pod Identity associations, the scanner-app deployment, and CLISH scan policy settings (the script re-applies the policy anyway as a safeguard).

## Security

- All S3 buckets use AES256 encryption with public access fully blocked
- SQS queues use server-side encryption
- All stack-created S3 buckets have `DeletionPolicy: Retain` — they survive stack deletion to preserve files for forensic investigation
- In existing-bucket mode, the stack never deletes objects from your bucket, and the scanner-app IAM role has no `s3:DeleteObject` permission on it
- ECR repository has scan-on-push enabled for container vulnerability scanning
- IMDSv2 is enforced on all nodes
- EBS volumes are encrypted
- EFS filesystem is encrypted at rest and in transit (TLS mount option)
- The scanner endpoint is never internet-facing — the NLB and ALB are both internal (VPC-only), and ALB mode adds TLS via your ACM certificate
- VPC Flow Logs capture all network traffic
- EKS audit logging is enabled for all control plane components
- IAM policies use least-privilege, resource-scoped permissions
- The review scanner IAM role (when deployed) has no write access to the review bucket, preventing routing loops back to the review pipeline
- Credentials are stored in Secrets Manager, never in plaintext
- The V1FS Helm chart is GPG-verified before installation

## Cleanup

```bash
aws cloudformation delete-stack --stack-name my-scanner
```

A pre-delete cleanup Lambda runs automatically during stack deletion — it removes the load balancers created by the AWS Load Balancer Controller (the scanner NLB or ALB), their target groups, and their security groups (identified by the `elbv2.k8s.aws/cluster=<cluster-name>` tag), and deletes orphaned EBS volumes before CloudFormation removes the roles and cluster. No manual cleanup is needed for these resources. Scanner-app and review pipeline resources (queues, Lambdas, log groups, the SSM endpoint parameter) are CloudFormation-managed and deleted with the stack.

Notes:

- All stack-created S3 buckets (ingest, clean, quarantine, review) have `DeletionPolicy: Retain` and are **not deleted** with the stack. This prevents accidental data loss — scanned files and quarantined malware are preserved for forensic investigation or audit.
- In existing-bucket mode, **your bucket is never touched** — objects, tags, and notification configuration are all left exactly as they are.
- The ECR repository and its images **are** deleted with the stack.

To clean up retained buckets after stack deletion:

```bash
# List retained buckets (names are in the stack outputs, saved before deletion)
aws s3 rb s3://<ingest-bucket> --force
aws s3 rb s3://<clean-bucket> --force
aws s3 rb s3://<quarantine-bucket> --force
aws s3 rb s3://<review-bucket> --force   # if the review pipeline was deployed
```
