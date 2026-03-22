# CLAUDE.md — EKS File Security Scanner Workflow

## AWS Session Credentials

Claude Code requires active AWS session credentials to use the AWS CLI. Before starting any AWS operations, ensure credentials are configured in the terminal:

```bash
# Recommended: IAM Identity Center (temporary credentials, no access keys)
aws sso login --profile <profile-name>

# Alternative: AWS CLI built-in login flow
aws login
```

Both methods obtain temporary session credentials — no long-lived access keys required. If AWS CLI commands fail with `ExpiredToken` or `InvalidClientTokenId`, prompt the user to re-authenticate by running `aws login` or `aws sso login` again. Do not attempt AWS operations without valid credentials.

## Branding

Trend Micro has rebranded to **TrendAI**. Always use "TrendAI" in user-facing text (README, docs, comments). Internal references in code, SDK package names, Helm chart URLs, and config values still use the old naming (e.g., `visionone-filesecurity`, `trendmicro.github.io`) — do not change those.

## Project Overview

Containerized Python application on EKS that polls an SQS queue for S3 object-creation events, scans each file for malware using the Vision One File Security SDK (gRPC to in-cluster scanner pods), and routes files to either a quarantine bucket (malicious) or clean bucket (clean). All infrastructure is provisioned by a single CloudFormation template (`eks-v1fs.yaml`).

```
S3 (Ingest) → SQS Queue → EKS Pod (scan via gRPC) → Clean or Quarantine Bucket
                  └→ DLQ (after 3 failures)
```

## Detailed Documentation

Topic-specific docs are in the `docs/` directory:

| File | Contents |
|---|---|
| [docs/infrastructure.md](docs/infrastructure.md) | VPC, EKS, S3, SQS, EFS, EBS, ECR, IAM roles, bastion access |
| [docs/scanner-app.md](docs/scanner-app.md) | Service account, V1FS SDK usage, app logic, container image, config, deployment |
| [docs/security.md](docs/security.md) | Container hardening, network security, data protection, secrets management |
| [docs/guardrails.md](docs/guardrails.md) | Do NOT do list, workflow rules, lessons learned, bastion environment notes |
| [docs/performance.md](docs/performance.md) | KEDA scaling config and performance characteristics |

## Slash Commands

| Command | Purpose |
|---|---|
| `/regenerate` | Full teardown + redeploy a fresh stack |
| `/teardown` | Delete all billable resources in the AWS account |
| `/upgrade-v1fs` | Safely upgrade V1FS scanner Helm chart with all custom values |

## File Structure

```
project/
├── CLAUDE.md                     # This file — Claude Code project guidance
├── docs/                         # Detailed docs (local only)
├── eks-v1fs.yaml                 # CloudFormation template (all infrastructure)
├── app/
│   ├── Dockerfile                # python:3.11-slim, non-root UID 999
│   ├── requirements.txt          # Pinned: visionone-filesecurity, aiobotocore, boto3
│   ├── scanner.py                # Main async polling + scan loop, health server, audit trail
│   └── config.py                 # Environment variable loading + validation
├── k8s/
│   ├── serviceaccount.yaml       # ServiceAccount: scanner-app (Pod Identity, NO annotations)
│   ├── deployment.yaml           # Hardened deployment (non-root, read-only fs, drop caps, health probes)
│   ├── configmap.yaml            # SQS URL, S3 bucket names, scanner endpoint, API key ARN, audit log group
│   ├── networkpolicy.yaml        # Egress restricted to DNS, V1FS scanner, AWS HTTPS
│   ├── pdb.yaml                  # PodDisruptionBudgets for scanner-app and V1FS scanner (Karpenter consolidation protection)
│   └── scaledobject.yaml         # KEDA ScaledObjects + TriggerAuthentication for SQS-driven autoscaling (both scanner-app and V1FS scanner)
└── scripts/
    ├── build-and-push.sh         # Build Docker image and push to ECR (tagged with git SHA)
    └── deploy.sh                 # Apply k8s manifests to the cluster
```

## Current Scaling Limits

| Component | Max | Config Location |
|---|---|---|
| Scanner-app pods (KEDA) | 150 | `k8s/scaledobject.yaml` |
| V1FS scanner pods (KEDA) | 150 | `k8s/scaledobject.yaml` |
| Karpenter CPU limit | 300 | `eks-v1fs.yaml` NodePool CRD in bastion UserData |
| Managed node group (system only) | 6 | `eks-v1fs.yaml` MaxSize |
| MAX_CONCURRENT_SCANS | 50 | `k8s/configmap.yaml` |
| Full-scale concurrent scans | 7,500 | 150 pods × 50 concurrent |
| vCPU required at full scale | ~200 | Account quota increased to 300 |

## Quick Reference — Critical Rules

### Identity & Credentials
- **Pod Identity, not IRSA** — no `eks.amazonaws.com/role-arn` annotations anywhere
- **KEDA auth**: `provider: aws`, `identityOwner: keda` (not `aws-eks`, not `operator`)
- **V1FS SDK**: `init()` is sync (don't await), `scan_buffer()` and `quit()` are async
- **PML**: disabled (`pml=False`) unless account supports it
- **Never use SSH to connect to the bastion** — always use AWS Systems Manager Session Manager (`aws ssm start-session`). SSH keys are stored in SSM Parameter Store for emergency use only

### Deployment & Stack Management
- **Image tags**: immutable git SHA, never `:latest`
- **S3 bucket names**: auto-generated by CloudFormation, never hardcoded
- **ECR repo name**: auto-generated by CloudFormation (no `RepositoryName` property) to avoid uppercase stack name conflicts
- **ECR cleanup is automatic** — `EmptyOnDelete: true` on the ECR repo, so CloudFormation empties and deletes it during teardown
- **Stack deployment**: `--disable-rollback`, unique incrementing stack names, `--template-url` with S3-hosted copy (template exceeds 51KB inline limit)
- **Test before commit**: validate changes in a live stack before pushing to git. Exception: files the bastion clones from git (k8s manifests, app code) must be pushed before they can be tested
- **Always run `build-and-push.sh` before `deploy.sh` on live clusters** — the deploy script uses the current git SHA as the image tag. If you push code changes and run only `deploy.sh`, it will try to pull an image tag that doesn't exist in ECR, causing `ImagePullBackOff`
- **Live cluster patching**: the deploy script substitutes `<SQS_QUEUE_URL>` and `<AWS_REGION>` placeholders with real values. Applying the template file directly with `kubectl apply` will break KEDA with "invalid input region" errors
- **NodeInstanceType parameter is for the managed node group only** — it controls system nodes (r7i.large default), NOT Karpenter scanner nodes. Karpenter's instance types are defined in the NodePool CRD in bastion UserData

### Karpenter & Scaling
- **Karpenter replaces Cluster Autoscaler** — provisions nodes directly via EC2 Fleet API (30-60s vs 1-2min). NodePool and EC2NodeClass CRDs are applied in bastion UserData. Managed node group (max 6) is for system components only; scanner workloads run on Karpenter nodes via nodeAffinity
- **Karpenter instance flexibility** — NodePool allows r7i.xlarge, r7a.xlarge, r6i.xlarge only. On-demand only (no spot). CPU limit of 300 matches vCPU quota
- **Karpenter uses a CloudFormation-managed instance profile** — the EC2NodeClass specifies `instanceProfile` (not `role`) referencing `KarpenterNodeInstanceProfile`. Do NOT switch to `role:` — that causes Karpenter to create dynamic instance profiles that orphan on stack deletion
- **Both scanner-app AND V1FS scanner scale via KEDA on SQS depth** — no CPU-based HPA. The Helm chart's `scanner.autoscaling.enabled` must be `false` to prevent HPA conflicts. Tuned values: scanner-app threshold=5, V1FS threshold=50, polling interval=10s, cooldown=90s
- **PodDisruptionBudgets are required** — `k8s/pdb.yaml` protects scanner-app (maxUnavailable 25%) and V1FS scanner (minAvailable 1) from Karpenter consolidation during active scanning
- **AWS on-demand vCPU quota** — 300. Karpenter NodePool `limits.cpu` is set to 300 to match
- **Default VPC must have subnets** — Karpenter v1.3.0 does a dry-run `RunInstances` to validate IAM permissions, which defaults to the default VPC. If the default VPC has no subnets, validation fails with "MissingInput". Do not delete default VPC subnets during account cleanup
- **V1FS scan cache affects benchmark results** — the scanner caches results by file hash. Running the same files on the same stack produces artificially fast results (~28ms vs ~4.3s real). Clear the cache without redeploying: `kubectl rollout restart deployment/my-release-visionone-filesecurity-scan-cache -n visionone-filesecurity`
- **V1FS scanner pods do not expose Prometheus metrics** — no `/metrics` HTTP endpoint. Custom I/O-based HPA metrics are not possible without a sidecar proxy

### Observability
- **Health probes on port 8080** — scanner-app serves `/healthz` (liveness) and `/readyz` (readiness) via a lightweight async TCP server. Readiness returns 503 until the gRPC scan handle is initialized and during shutdown. The network policy only restricts Egress, so kubelet probe ingress is unrestricted
- **Scan audit trail** — each scan result is written to CloudWatch Logs (`scan-audit-${StackName}`) as structured JSON. The `ScannerAppPolicy` includes `logs:CreateLogStream` and `logs:PutLogEvents` permissions. Audit entries are batched (up to 25 per write) and flushed on shutdown. If the log group doesn't exist, audit logging degrades gracefully
- **DLQ remediation Lambda** — triggered by SQS event source mapping on the DLQ. Re-queues messages with exponential backoff (60s → 300s → 900s) using a `DLQRetryCount` message attribute. After 3 DLQ retries (9 total scan attempts), logs `PERMANENT_FAILURE` and discards. Do NOT manually process the DLQ — the Lambda handles it automatically
- **DLQ visibility timeout must be >= Lambda timeout** — the DLQ has `VisibilityTimeout: 120` (seconds) to satisfy the SQS event source mapping requirement (Lambda timeout is 60s). Without this, CloudFormation fails to create the `DLQEventSourceMapping`
- **SNS alarm topic requires subscription** — the `AlarmSNSTopic` is created but has no subscribers by default. Subscribe with: `aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint you@example.com`
- **CloudWatch Dashboard** — `scanner-${StackName}`, 26 widgets. CFN-managed, created/deleted with the stack. Dashboard URL is in stack outputs

### Operational Gotchas
- **Bastion has S3 ingest write permission** — the bastion role includes `s3:PutObject` and `s3:ListBucket` on the ingest bucket. Use `aws s3 sync` from bastion for fastest file delivery
- **aws s3 sync with `--quiet` silently swallows errors** — always test S3 access separately before relying on sync output. A "fast" sync that completes in seconds for thousands of files likely means it failed silently
- **SSM command output truncation** — long SSM outputs get truncated, causing subsequent commands in the same invocation to silently not execute. Use `--quiet` for s3 operations when they're not the last command, or split into separate SSM invocations
- **SSM TimeoutSeconds minimum is 30** — values below 30 cause parameter validation errors

### Cleanup & Lifecycle
- **Pre-delete cleanup Lambda** — `CleanupLambda` runs automatically during stack deletion. It terminates Karpenter EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes BEFORE CloudFormation deletes the roles and cluster
- **Orphaned EBS volumes after stack deletion** — V1FS PVCs (100 GB gp3 each) persist after stack deletion. Always check: `aws ec2 describe-volumes --filters Name=status,Values=available`
- **Orphaned EC2 instances after stack deletion** — Karpenter-managed nodes can survive stack deletion. Check: `aws ec2 describe-instances --filters Name=instance-state-name,Values=running`

## Node Scaling Architecture (Karpenter)

Karpenter replaces the Kubernetes Cluster Autoscaler for node provisioning. This is a deliberate architectural choice — do not revert to Cluster Autoscaler.

### Why Karpenter

The production workload is sustained, latency-sensitive file scanning with unpredictable spikes. Cluster Autoscaler was too slow (1-2 min node launch via ASG) and too blunt (single instance type, timer-based scale-down). Karpenter solves both:

- **Faster provisioning**: 30-60 seconds via direct EC2 Fleet API calls, bypassing the ASG entirely
- **Smart consolidation**: Replaces timer-based scale-down with intelligent bin-packing. Karpenter simulates whether pods can be rescheduled to fewer nodes, then consolidates. This is superior for sustained load with natural variation
- **Instance flexibility**: Selects from multiple instance types (r7i, r7a, r6i (xlarge only)) based on availability and fit, eliminating single-type capacity failures
- **Cost alignment**: Consolidation + right-sizing means the cluster closely tracks actual demand

### Two-tier node architecture

| Tier | Managed by | Purpose | Max nodes |
|---|---|---|---|
| System nodes | EKS managed node group | CoreDNS, KEDA, EBS/EFS CSI, Karpenter, Pod Identity agent | 6 |
| Scanner nodes | Karpenter NodePool `scanner-pool` | scanner-app pods, V1FS scanner pods | ~75 xlarge nodes (300 vCPU limit) |

Scanner workloads are directed to Karpenter nodes via `nodeAffinity` on `karpenter.sh/nodepool`. System components stay on the managed node group naturally (no taint needed — Karpenter nodes have the `scanner-pool` label and system pods don't request it).

### Key configuration locations

- **NodePool + EC2NodeClass CRDs**: Applied inline in bastion UserData (`eks-v1fs.yaml`), not as separate k8s manifest files
- **Karpenter IAM**: `KarpenterControllerRole` in `eks-v1fs.yaml` — PolicyDocument uses `Fn::Sub` with JSON string format because CloudFormation YAML does not support `!Sub` as map keys (required for IAM condition keys containing the cluster name). Instance profile management permissions are minimal (`iam:GetInstanceProfile` only) because the instance profile is CloudFormation-managed, not Karpenter-managed
- **Node access**: `KarpenterNodeAccessEntry` (`EC2_LINUX` type) — required for Karpenter-launched nodes to join the cluster
- **Discovery tags**: `karpenter.sh/discovery` on private subnets and node security group
- **PDBs**: `k8s/pdb.yaml` — scanner-app (maxUnavailable 25%), V1FS scanner (minAvailable 1)
- **Consolidation**: `WhenEmptyOrUnderutilized` after 2 minutes, with 10% disruption budget

### System node group sizing

The managed node group (r7i.large, 2 vCPU each) needs a minimum of **3 nodes** (not 2). With 2 nodes (4 vCPU total), system components (CoreDNS, KEDA, EBS/EFS CSI, LB controller, metrics server, Karpenter) exhaust available CPU. Karpenter requests 500m CPU per replica × 2 replicas = 1 vCPU. The third node provides headroom. The managed node group stays on r7i.large — system pods don't need the 32 GiB of xlarge instances.

### Deploy script rollout timeout

The deploy script (`scripts/deploy.sh`) waits up to 300s for the scanner-app rollout, but treats timeout as a **warning, not a failure**. On first deployment, Karpenter must provision a node before the scanner-app pod can schedule — this can take 60-90s. The bastion signals SUCCESS to CloudFormation regardless, and the pod starts once the node is ready.

### In-place V1FS upgrades with Karpenter

Karpenter does not interfere with Helm chart upgrades. Rolling updates are handled by the Kubernetes Deployment controller at the pod level. Karpenter only manages nodes. PDBs prevent Karpenter from consolidating nodes during an upgrade.

**V1FS Helm upgrades require re-specifying all custom `--set` values.** A plain `helm upgrade` without them reverts to chart defaults, re-enabling HPA (conflicts with KEDA) and resetting resources. See the "Updating the V1FS Scanner" section in README.md or use the `/upgrade-v1fs` slash command.

## Performance Characteristics

- **V1FS scanner is I/O and memory bound, not CPU bound** — CPU stays below 70% even under heavy load. The scanning engine loads signature databases into memory and spends most time on network I/O (gRPC) and disk operations. This is why CPU-based HPA didn't work and we switched to KEDA SQS-driven scaling
- **r7i/r7a/r6i xlarge (memory-optimized) instances only** — 32 GiB per node provides headroom for signature databases. Non-burstable CPU gives consistent performance. Karpenter NodePool only allows xlarge (4 vCPU) — large (2 vCPU) was removed because at scale, fewer larger nodes provision faster. General-purpose (m7i) was removed because Karpenter chose it over memory-optimized to save cost, but the scanner needs the extra memory
- **Rewriting scanner-app in Go would not improve throughput** — the bottleneck is the V1FS scanner backend and network round-trips, not the Python runtime. The app spends nearly all time waiting on I/O
- **At theoretical max scale (150+150 pods), vCPU totals ~200** — 150 × 500m + 150 × 800m = 195 vCPU for pods, plus node overhead. The 300 vCPU quota provides ample headroom
- **gRPC scan timeout is configurable** — the V1FS SDK reads `TM_AM_SCAN_TIMEOUT_SECS` from environment (default 300s). Set to 600s in the configmap to prevent "Deadline Exceeded" on complex files. Files exceeding the timeout go to DLQ after 3 retries
- **Cleanup Lambda for graceful stack deletion** — `CleanupLambda` in `eks-v1fs.yaml` automatically terminates Karpenter EC2 instances, cleans up orphaned instance profiles, and deletes orphaned EBS volumes during stack deletion. Users can simply run `aws cloudformation delete-stack` without manual cleanup
- **DLQ remediation Lambda** — `DLQRemediationLambda` in `eks-v1fs.yaml` auto-re-queues failed messages with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard. Scan failures that are transient (network blips, scanner restarts) recover automatically
- **CloudWatch Alarms** — DLQ alarm (any messages > 0) and Queue Age alarm (oldest message > 20 min for 5 consecutive minutes) alert via SNS topic. Subscribe to the topic to receive notifications
- **CloudWatch Dashboard** — `scanner-${StackName}`, 26 widgets covering queue health, scan throughput/latency (Logs Insights), malware detection stats, DLQ remediation, pod distribution, and recent scan results. CFN-managed, created/deleted with the stack. Dashboard URL is in stack outputs

