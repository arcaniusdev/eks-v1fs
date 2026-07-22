# CLAUDE.md — EKS File Security Scanner Workflow

> **THREE DEPLOYMENT OPTIONS (one template, `ScannerScalingMode` param).** `hpa` (default) = the chart's CPU/mem HPA (TrendAI-supported). `keda` = KEDA scales the V1FS scanner on SQS **queue depth** (`k8s/scanner-scaledobject.yaml`, `v1fs-scanner-sqs-scaler`); chart HPA disabled; a customer variant, NOT Trend-supported. `bootstrap.sh` resolves the mode into `SCANNER_KEDA` (keda needs a queue — full-auto's, or `ExternalScanQueueArn` in BYO; else falls back to hpa) and sets `scanner.autoscaling.enabled` accordingly; `deploy.sh` applies the scanner ScaledObject only when `SCANNER_KEDA=true`. **Upgrade discipline for keda:** always `scripts/upgrade.py` (never bare `helm upgrade`) — it re-asserts `autoscaling.enabled=false` and FAILS if a chart HPA reappears alongside KEDA (two autoscalers thrash). Client references live in `reference/{python-default,python-KEDA,java-KEDA}/`, each with its own POC-GUIDE.md; the KEDA ones include the pull/semaphore dispatcher (NLB-target-group discovery + per-pod pool). `ScannerEndpointMode=auto` → **NLB** (ALB opt-in); the NLB doubles as the pod-discovery registry. All options ARM-capable.

## AWS Session Credentials

Claude Code requires active AWS session credentials to use the AWS CLI. Before starting any AWS operations, ensure credentials are configured in the terminal:

```bash
# Recommended: IAM Identity Center (temporary credentials, no access keys)
aws sso login --profile <profile-name>

# Alternative: AWS CLI built-in login flow
aws login
```

Both methods obtain temporary session credentials — no long-lived access keys required. If AWS CLI commands fail with `ExpiredToken` or `InvalidClientTokenId`, prompt the user to re-authenticate by running `aws login` or `aws sso login` again. Do not attempt AWS operations without valid credentials.

## Public Repository Hygiene

This repository is PUBLIC. Never commit: AWS account IDs, credentials/tokens/keys of any kind, internal or identifying information (names, emails, internal URLs/hostnames), or references to specific organizations, engagements, or deployments. Use placeholders like `<ACCOUNT_ID>` in examples. Generic terms only ("user", "evaluation") — review every diff for these before committing.

## Branding

Trend Micro has rebranded to **TrendAI**. Always use "TrendAI" in user-facing text (README, docs, comments). Internal references in code, SDK package names, Helm chart URLs, and config values still use the old naming (e.g., `visionone-filesecurity`, `trendmicro.github.io`) — do not change those.

## Project Overview

evaluation-friendly EKS deployment of the TrendAI V1FS containerized scanner, aligned with TrendAI's supported deployment methodology (chart-native HPA, standard Cluster Autoscaler, pinned chart 1.4.10). A single CloudFormation template (`eks-v1fs.yaml`) provisions everything; optional modules are toggled by parameters:

| Mode | Parameters | What you get |
|---|---|---|
| Default full-auto | (defaults) | V1FS scanner + our scanner-app: drop files in the ingest bucket → clean files tagged in place, malicious moved to quarantine |
| BYO scanning app | `DeployScannerApp=false` | V1FS scanner only + gRPC endpoint (internal NLB by default) published to SSM `/<stack>/scanner-endpoint` |
| Existing bucket | `ExistingIngestBucket=<name>` | Scans a user-owned bucket via S3→EventBridge→SQS; objects are tagged with verdicts, never deleted |
| External queue drain | `ExternalScanQueueArn=<arn>` + `ExternalScanSourceBucketArns=<arns>` | scanner-app drains YOUR SQS queue (S3-event-shaped msgs) INSTEAD of a stack-built queue; no ingest bucket/queue/DLQ/dashboard built. Clean tagged in place, malicious copied to quarantine (your objects never deleted). Full-auto only; mutually exclusive with ExistingIngestBucket |
| Full + review | `DeployReviewPipeline=true` | Adds the second `rv` release (unlimited decompression) for deep archive analysis |

```
S3 (Ingest, created or existing) → SQS Queue → scanner-app Pod (gRPC) → clean: tag in place / malicious: move to Quarantine Bucket
                  └→ DLQ (after 3 failures)                (decompression-limit → review bucket if review pipeline enabled)
[optional] S3 (Review) → Review SQS → Review Scanner Pod (no limits) → clean: tag in place / malicious: move to Quarantine
                  └→ Review DLQ (after 3 failures)
[optional] External scanning app → internal NLB (gRPC :50051) or ALB Ingress (TLS :443) → V1FS scanner
```

## Detailed Documentation

Topic-specific docs are in the `docs/` directory:

| File | Contents |
|---|---|
| [docs/infrastructure.md](docs/infrastructure.md) | VPC, EKS, S3, SQS, EFS, EBS, ECR, IAM roles, bastion access |
| [docs/scanner-app.md](docs/scanner-app.md) | Service account, V1FS SDK usage, app logic, container image, config, deployment |
| [docs/security.md](docs/security.md) | Container hardening, network security, data protection, secrets management |
| [docs/guardrails.md](docs/guardrails.md) | Do NOT do list, workflow rules, lessons learned, bastion environment notes |
| [docs/performance.md](docs/performance.md) | HPA/KEDA/Cluster Autoscaler scaling config and performance characteristics |

## File Structure

```
project/
├── CLAUDE.md                     # This file — Claude Code project guidance
├── docs/                         # Detailed docs (local only)
├── eks-v1fs.yaml                 # CloudFormation template (all infrastructure; Rules + Conditions for modes)
├── helm/
│   ├── values-base.yaml          # V1FS chart values — single source of truth (install + upgrades)
│   └── values-nlb.yaml           # Overlay: internal NLB endpoint via chart externalService
├── app/
│   ├── Dockerfile                # python:3.11-slim, non-root UID 999
│   ├── requirements.txt          # Pinned: visionone-filesecurity, aiobotocore, boto3
│   ├── scanner.py                # Async polling + scan loop, dual event parsing, health server, audit trail
│   └── config.py                 # Environment variable loading + validation
├── k8s/
│   ├── serviceaccount.yaml       # ServiceAccount: scanner-app (Pod Identity, NO annotations)
│   ├── deployment.yaml           # Hardened deployment (non-root, read-only fs, drop caps, health probes)
│   ├── configmap.yaml            # SQS URL, S3 bucket names, scanner endpoint, API key ARN, audit log group
│   ├── networkpolicy.yaml        # Egress restricted to DNS, V1FS scanner, AWS HTTPS
│   ├── pdb.yaml                  # PodDisruptionBudgets (Cluster Autoscaler drain protection)
│   ├── scaledobject.yaml         # KEDA ScaledObject for scanner-app ONLY (V1FS scanner uses chart HPA)
│   ├── review-serviceaccount.yaml  # ServiceAccount: review-scanner-app (Pod Identity, NO annotations)
│   ├── review-deployment.yaml      # Review scanner deployment (same image, different config)
│   ├── review-networkpolicy.yaml   # Egress restricted to DNS, rv V1FS scanner, AWS HTTPS
│   └── review-scaledobject.yaml    # KEDA ScaledObject for review-scanner-app ONLY (min 1, max 5)
└── scripts/
    ├── bootstrap.sh              # All bastion provisioning logic (invoked by UserData after git clone)
    ├── build-and-push.sh         # Build Docker image and push to ECR (tagged with git SHA)
    ├── deploy.sh                 # Apply k8s manifests to the cluster
    └── upgrade.py                # Safely upgrade V1FS Helm release(s), preserving installed values + HPA bounds
```

## Current Scaling Limits

| Component | Max | Config Location |
|---|---|---|
| V1FS scanner pods (chart HPA, CPU/mem 80%) | 10 (default) | `ScannerMaxReplicas` CFN parameter |
| Scanner-app pods (KEDA, SQS-driven) | 20 (default) | `ScannerAppMaxReplicas` CFN parameter |
| Managed node group (single tier, r8g.xlarge default) | 8 (default) | `NodeGroupMaxSize` CFN parameter |
| MAX_CONCURRENT_SCANS | 50 | `k8s/configmap.yaml` |
| Review scanner-app pods (KEDA) | 5 | `k8s/review-scaledobject.yaml` |
| Review V1FS scanner pods (chart HPA) | 3 | `scripts/bootstrap.sh` rv install |

Full-mode peak ≈ 26 vCPU → 8 × xlarge nodes (r8g.xlarge default, 4 vCPU each); fits within the default 64 on-demand vCPU quota. No quota increase needed at evaluation scale.
Review pipeline keeps 1 pod warm at all times (min replicas = 1) to avoid cold-start gRPC failures.
Expect 1–3 minutes for HPA + Cluster Autoscaler scale-up under load — normal for the supported configuration. This is an evaluation-sized deployment, not a burst-throughput one.

## Quick Reference — Critical Rules

### Identity & Credentials
- **Never store credentials in files** — if the user provides API keys, tokens, passwords, or other credentials, advise that storing them in plaintext files is a security risk and offer to store them in AWS Secrets Manager instead. Retrieve credentials from Secrets Manager at runtime rather than embedding them in CLAUDE.md, memory files, scripts, or configuration
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
- **Live cluster patching**: the deploy script substitutes `<SQS_QUEUE_URL>`, `<AWS_REGION>`, and `<MAX_REPLICAS>` placeholders with real values. Applying the template file directly with `kubectl apply` will break KEDA with "invalid input region" errors
- **NodeInstanceType controls the single managed node group** — one node group (default r8g.xlarge, Graviton ARM) hosts system components AND scanner workloads. xlarge memory-optimized classes only (fits four 800m/2Gi scanner pods per node). Default is Graviton ARM (r8g.xlarge; r7g.xlarge also ARM); x86 classes (r7i/r7a/r6i xlarge, r7i.2xlarge) optional. The `NodeArchMap` mapping selects the AMI (ARM64 vs x86_64) and exports `TARGET_ARCH` so the bastion builds the scanner-app image for the node arch (QEMU binfmt cross-build when bastion arch ≠ node arch — the default t3.medium bastion is x86, so it QEMU-cross-builds for ARM nodes). All V1FS chart images and supporting components are multi-arch
- **Deployment-mode parameters**: `DeployScannerApp` (default true), `DeployReviewPipeline` (default false, requires scanner app — CFN Rule enforced), `ExistingIngestBucket` (empty = create), `ScannerEndpointMode` (auto/none/nlb/alb, **default auto → internal NLB** for both full-auto and BYO on this branch; the NLB doubles as the pod-discovery registry for client-side dispatchers. `alb` is explicit opt-in and requires `ScannerDomain` (defaulted) + a cert — `SelfSignedScannerCert` defaults true — Rule enforced), `ExistingVpcId` (empty = create network; when set requires `ExistingVpcCidr` + 2 private subnets + bastion subnet — Rule enforced. User VPC needs DNS enabled, NAT egress, `kubernetes.io/role/internal-elb=1` on private subnets; bastion gets no public IP and the EKSCluster waits on NAT routes via a conditional `network-ready` tag reference since DependsOn can't be conditional)
- **No in-place migration across major architecture changes** — delete and redeploy rather than stack-updating an older deployment

### V1FS Scan Policy (CLISH)
- **Scan policy is configured via CLISH after Helm install** — the V1FS management service exposes a CLI (`clish`) for runtime scanner configuration. Four decompression settings are available, all controlled via CloudFormation parameters and applied automatically during bastion provisioning
- **Settings are applied post-install, not via Helm values** — scan policy is a runtime ConfigMap managed by the management service, not a Helm chart value. The bastion UserData waits for the management service rollout, then runs `clish scanner scan-policy modify` with the CloudFormation parameter values
- **Defaults are unset (unlimited) in the scanner** — without explicit configuration, the scanner has no decompression limits. Our CloudFormation template provides sensible defaults to protect against archive-based attacks

| Parameter | Default | Range | Purpose |
|---|---|---|---|
| `MaxDecompressionLayer` | 10 | 1-20 | Max archive nesting depth (zip in zip). Protects against deeply nested malware |
| `MaxDecompressionFileCount` | 1000 | 0+ (0=unlimited) | Max files extracted from one archive. Protects against file-count bombs |
| `MaxDecompressionRatio` | 150 | 100-2147483647 | Max compression ratio. A 1 MB file decompressing to >150 MB is flagged as a zip bomb |
| `MaxDecompressionSize` | 512 | 0-2048 MB (0=unlimited) | Max total decompressed size per archive. Caps memory/disk usage |

- **To view current settings on a live cluster**: `kubectl exec deploy/my-release-visionone-filesecurity-management-service -n visionone-filesecurity -- clish scanner scan-policy show`
- **To modify settings on a live cluster**: `kubectl exec deploy/my-release-visionone-filesecurity-management-service -n visionone-filesecurity -- clish scanner scan-policy modify --max-decompression-layer=<N> ...`
- **Changes take effect immediately** — no pod restart required. The scanner detects ConfigMap updates and reloads
- **CLISH also has agent management commands** (`clish agent`) for ONTAP storage agent integration — not relevant to our SDK-based scanning architecture
- **Decompression limit violations route to review or quarantine** — when the main V1FS scanner returns `scanResult=0` (clean) but includes `foundErrors` entries indicating decompression limits were exceeded, scanner-app routes the file to the review bucket (review pipeline enabled) where the `rv` release re-scans it with no limits, OR to quarantine with tags `ScanResult=S3-DecompressionLimit` and `ScanErrors=<names>` (review disabled — the default). Incompletely-scanned files are NEVER marked clean
- **V1FS SDK `foundErrors` names**: `ATSE_ZIP_RATIO_ERR` (compression ratio exceeded), `ATSE_MAXDECOM_ERR` (nesting depth exceeded), `ATSE_ZIP_FILE_COUNT_ERR` (file count exceeded), `ATSE_EXTRACT_TOO_BIG_ERR` (decompressed size exceeded). These are returned in `result.foundErrors[].name` of the SDK response
- **MAX_FILE_SIZE_MB is configurable** — files exceeding this limit (default 500 MB) are routed via server-side S3 copy (no download into pod memory) to the review bucket (review enabled) or quarantine with `ScanResult=S3-Oversize` (review disabled). The review scanner has no file size limit (`MAX_FILE_SIZE_MB=0`) and scans them normally

### Review Pipeline (Deep Analysis — OPTIONAL, default OFF)
- **Enabled via `DeployReviewPipeline=true`** (requires `DeployScannerApp=true`, enforced by a CFN Rule). When disabled, no rv release, review bucket/queue/DLQ/Lambda/log group are created, and decompression-limit files quarantine with explanatory tags
- **Second Helm release `rv` in `visionone-review` namespace** — installed with no CLISH scan policy applied (unlimited decompression). This allows the review scanner to fully analyze archives that exceeded the main scanner's decompression limits. A separate namespace is required because each V1FS Helm release creates a ServiceAccount named `visionone-filesecurity` — installing both releases in the same namespace causes a ServiceAccount conflict
- **Review scanner reads from the review bucket** — clean files are tagged in place, malicious files are moved to quarantine; it NEVER routes back to review. `REVIEW_ROUTING_ENABLED=false` in the review scanner ConfigMap prevents the application from attempting review routing, and the `ReviewScannerAppRole` IAM policy has no write permission to the review bucket as a defense-in-depth control
- **Always-warm review pipeline** — min 1, max 5 pods, cooldown 300s. Keeps one review scanner-app and one V1FS scanner pod running at all times to avoid cold-start gRPC connection failures when files arrive
- **Shares the same `token-secret`** — no second V1FS registration token is needed. Both Helm releases use the same token
- **Separate audit log group** — `review-audit-${StackName}` for review scan results, independent from the main `scan-audit-${StackName}`
- **Separate DLQ with remediation Lambda** — the review pipeline has its own SQS DLQ and Lambda for retry/discard handling
- **Deploy with `deploy.sh --review`** — the deploy script applies review-specific k8s manifests (review-serviceaccount, review-deployment, review-networkpolicy, review-scaledobject)
- **Same Docker image as the main scanner** — behavior is controlled entirely by environment variables (different SQS queue, different scanner endpoint, `REVIEW_ROUTING_ENABLED=false`)
- **Orphaned file reconciliation** — a background loop lists the ingest bucket every `RECONCILIATION_INTERVAL` seconds (default 300) and sends synthetic SQS messages to the main scan queue for any objects older than `RECONCILIATION_AGE_THRESHOLD` seconds (default 1800). This catches files that were uploaded but never processed due to transient failures. It runs on the REVIEW scanner when the review pipeline is enabled, on the MAIN scanner-app when review is disabled (deploy.sh adds the reconciliation block automatically), and is FORCED OFF in existing-bucket mode (objects legitimately persist there). `ScannerAppRole` has `sqs:SendMessage` on the main queue for this purpose

### Autoscaling (Trend-Supported Configuration)
- **V1FS scanner autoscaling is `ScannerScalingMode`-driven** — `hpa` (default): chart CPU/mem HPA (`scanner.autoscaling.enabled=true` from `values-base.yaml` + min/max from `ScannerMinReplicas`/`ScannerMaxReplicas`), TrendAI-supported. `keda`: KEDA on queue depth (`k8s/scanner-scaledobject.yaml`, `v1fs-scanner-sqs-scaler`, threshold `SCANNER_QUEUE_LENGTH`), chart HPA disabled — a customer variant, NOT Trend-supported. `bootstrap.sh` computes `SCANNER_KEDA` (keda + a queue → true; keda-without-queue falls back to hpa) and sets `autoscaling.enabled` accordingly; `deploy.sh` applies the ScaledObject only when `SCANNER_KEDA=true`. **keda upgrade safety**: `upgrade.py` re-asserts `--set scanner.autoscaling.enabled=false` and its Step-6 guard **fails the upgrade** if a chart HPA reappears alongside the ScaledObject. NEVER bare `helm upgrade` in keda mode.
- **KEDA scales our scanner-app AND the V1FS scanner** (both on SQS depth: scanner-app threshold 5 msgs/pod, scanner threshold `SCANNER_QUEUE_LENGTH`; polling 5s, cooldown 300s). KEDA is installed only when `DeployScannerApp=true` (full-auto). The scanner ScaledObject uses its own `scanner-sqs-trigger-auth` TriggerAuthentication
- **Nodes scale via Cluster Autoscaler** — standard `autoscaler/cluster-autoscaler` helm chart, Pod Identity (`ClusterAutoscalerRole`), ASG auto-discovery via the `k8s.io/cluster-autoscaler/*` tags EKS applies to managed node group ASGs automatically. Expander `least-waste`, scale-down after 2 min unneeded
- **Single managed node group** — r8g.xlarge (Graviton ARM) default, hosts system AND scanner workloads (no nodeAffinity, no separate workload tier). Sizing: min 2 (CoreDNS/AZ redundancy), max 8 (full-mode peak ≈ 26 vCPU at ~3.6 usable vCPU/node)
- **PodDisruptionBudgets are required** — `k8s/pdb.yaml` protects scanner-app (maxUnavailable 25%) and V1FS scanner (minAvailable 1) from Cluster Autoscaler node drains during active scanning
- **metrics-server must be installed before the V1FS chart** — the chart HPA needs CPU/memory metrics; bootstrap.sh installs it early. If HPA shows `<unknown>` targets, check metrics-server
- **V1FS scan cache affects benchmark results** — the scanner caches results by file hash. Running the same files on the same stack produces artificially fast results (~28ms vs ~4.3s real). Clear the cache without redeploying: `kubectl rollout restart deployment/my-release-visionone-filesecurity-scan-cache -n visionone-filesecurity`
- **V1FS scanner pods do not expose Prometheus metrics** — no `/metrics` HTTP endpoint; the chart HPA uses resource metrics (CPU/memory) from metrics-server

### Scanner Endpoint Exposure
- **`ScannerEndpointMode=auto` (default) → internal NLB for BOTH full-auto and BYO (THIS BRANCH)** — `bootstrap.sh` resolves `auto`→`nlb` (mirrors the `ExposeNLB` Condition). The NLB is VPC-reachable (gRPC :50051, ICAP :1344) AND doubles as the **pod-discovery registry**: with `target-type=ip` its target group tracks the live scanner pod IPs, which a client-side dispatcher reads via the ELB `DescribeTargetHealth` API to connect **directly** to pods (pull/semaphore balancing; NLB stays out of the scan path — no L4 hot-spot, no L7 latency). Client needs `elasticloadbalancing:DescribeTargetGroups`/`DescribeTargetHealth` IAM + in-VPC pod-IP reachability. `alb` is explicit opt-in (self-signed cert path retained). Detailed writeup: `temp/scanner-load-balancing.html` (local only)
- **`ScannerEndpointMode=alb`** (same as auto, explicit) — chart `scanner.ingress` with `className=alb`, `backend-protocol-version=GRPC`, `target-type=ip`, internal scheme, ACM cert, `listen-ports=[{"HTTPS":443}]`, and `load-balancer-attributes: idle_timeout.timeout_seconds=<ScanTimeoutSeconds+60>` (660 default — MUST exceed the scan timeout or the ALB severs long scans mid-analysis). **Must set `hosts[0].paths[0]` (path `/`, `pathType Prefix`)** — setting only `hosts[0].host` drops the chart's default path → invalid Ingress (`spec.rules[].http.paths` required). L7 ALB terminates HTTP/2 and balances **per gRPC call** across pods — validated at scale: 7,161 samples over 4 reused channels spread ~1,194 each across 6 pods. TLS: `amaas.grpc.init("<domain>:443", api_key, True)`
- **`ScannerEndpointMode=nlb`** — chart-native `scanner.externalService` (`helm/values-nlb.yaml`), an INTERNAL NLB with gRPC :50051 and ICAP :1344, plaintext: `amaas.grpc.init("<nlb-host>:50051", api_key, False)`. **Reserved for a future ICAP use case** (ICAP needs L4; ALB is HTTP-only) — not used otherwise. L4, so it has the gRPC connection-pinning limitation
- **Self-signed ALB cert (`SelfSignedScannerCert=true`, default)** — makes ALB no-touch: with no `ACMCertificateArn`, `bootstrap.sh` generates a self-signed cert with `SAN=ScannerDomain` (defaulted `scanner.v1fs.internal`), imports it to ACM for the listener, and stores the public cert in Secret `scanner-tls-ca` + SSM `/<stack>/scanner-ca-cert`. Clients trust it via the SDK `ca_cert` arg (there is NO skip-verify option in the SDK — the cert MUST be supplied, and its SAN must match the connect host, so `ScannerDomain` + DNS to the ALB remain required). Scanner-app supports TLS via `V1FS_TLS_ENABLED=true` + `V1FS_CA_CERT=/etc/v1fs-tls/ca.crt` (deployment mounts Secret `scanner-tls-ca` at `/etc/v1fs-tls`, `optional: true`). SDK `init()` real signature: `init(host, api_key, enable_tls=False, ca_cert=None)`. `CleanupLambda` deletes the imported cert (tag `scanner-selfsigned-<stack>`) on stack delete
- **Endpoint address in Outputs + SSM** — CFN Output `ScannerEndpointAddress` = `<ScannerDomain>:443` (ALB); the bastion also writes the resolved LB hostname to SSM `/<stack>/scanner-endpoint`
- **Both chart ingresses default to `enabled: true` upstream** — `helm/values-base.yaml` explicitly disables `scanner.ingress` and `managementService.ingress`. Never remove those lines; an installed ALB ingress class would silently expose them

### Existing-Bucket Mode (User-Owned Ingest)
- **Wiring is S3 → EventBridge → SQS** — an `AWS::Events::Rule` filtered on the bucket name targets the scan queue. NEVER write a `QueueConfiguration` onto a user bucket: `put-bucket-notification-configuration` is a full-replace API and would destroy their existing notification wiring
- **EventBridge enablement MERGES** — `EnableEventBridgeFunction` (custom resource) reads the bucket's current notification config, adds `EventBridgeConfiguration` alongside it, and writes the merged document. Stack Delete is a no-op on the bucket
- **Tag, don't delete** — scanner-app runs `DELETE_SOURCE_ENABLED=false`: source objects are tagged with the verdict (`ScanResult=...`) via `put_object_tagging`, never deleted. IAM has `s3:GetObject`/`s3:PutObjectTagging`/`s3:ListBucket` on the bucket, NO `s3:DeleteObject`
- **EventBridge S3 event keys are RAW** (not URL-encoded), unlike S3 notifications (form-encoded, need `unquote_plus`). `scanner.py:_extract_records()` handles both shapes — keep the decoding asymmetry intact

### Observability
- **Health probes on port 8080** — scanner-app serves `/healthz` (liveness) and `/readyz` (readiness) via a lightweight async TCP server. Readiness returns 503 until the gRPC scan handle is initialized and during shutdown. The network policy only restricts Egress, so kubelet probe ingress is unrestricted
- **Scan audit trail** — each scan result is written to CloudWatch Logs (`scan-audit-${StackName}`) as structured JSON. The `ScannerAppPolicy` includes `logs:CreateLogStream` and `logs:PutLogEvents` permissions. Audit entries are batched (up to 25 per write) and flushed on shutdown. If the log group doesn't exist, audit logging degrades gracefully
- **Review audit trail** — review scan results are written to a separate CloudWatch Logs group (`review-audit-${StackName}`). Same structured JSON format as the main audit trail
- **DLQ remediation Lambda** — triggered by SQS event source mapping on the DLQ. Re-queues messages with exponential backoff (60s → 300s → 900s) using a `DLQRetryCount` message attribute. After 3 DLQ retries (9 total scan attempts), logs `PERMANENT_FAILURE` and discards. Do NOT manually process the DLQ — the Lambda handles it automatically
- **Review DLQ alarm** — separate CloudWatch alarm for the review pipeline DLQ, same SNS topic as the main DLQ alarm
- **DLQ visibility timeout must be >= Lambda timeout** — the DLQ has `VisibilityTimeout: 120` (seconds) to satisfy the SQS event source mapping requirement (Lambda timeout is 60s). Without this, CloudFormation fails to create the `DLQEventSourceMapping`
- **SNS alarm topic requires subscription** — the `AlarmSNSTopic` is created but has no subscribers by default. Subscribe with: `aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint you@example.com`
- **CloudWatch Dashboard** — `scanner-${StackName}`, 29 widgets. CFN-managed, created/deleted with the stack. Dashboard URL is in stack outputs

### V1FS Helm Upgrades
- **Use `upgrade.py` for V1FS Helm upgrades** — `scripts/upgrade.py` upgrades `my-release` (and `rv` only if installed) while preserving values: it layers `helm/values-base.yaml` + the release's captured `helm get values` + live HPA min/max bounds. It also captures/re-applies the CLISH scan policy to `my-release` only, verifies the chart HPA EXISTS (and that no ScaledObject targets the chart scanner), and runs a sanity scan (S3 EICAR flow, or SSM-endpoint instructions when scanner-app is absent). Flags: `--dry-run`, `--version X.Y.Z`, `--skip-sanity`
- **Do not run `helm upgrade` manually without the values files** — a plain `helm upgrade` reverts to chart defaults: it would re-enable both chart ingresses, reset storage classes, and lose the HPA replica bounds. Always go through `upgrade.py` or pass `-f helm/values-base.yaml` plus the current release values
- **Chart version is pinned** — `V1FS_CHART_VERSION` in `scripts/bootstrap.sh` (currently 1.4.10). Bump deliberately, not implicitly

### Operational Gotchas
- **Bastion has S3 ingest write permission** — the bastion role includes `s3:PutObject` and `s3:ListBucket` on the ingest bucket. Use `aws s3 sync` from bastion for fastest file delivery
- **aws s3 sync with `--quiet` silently swallows errors** — always test S3 access separately before relying on sync output. A "fast" sync that completes in seconds for thousands of files likely means it failed silently
- **SSM command output truncation** — long SSM outputs get truncated, causing subsequent commands in the same invocation to silently not execute. Use `--quiet` for s3 operations when they're not the last command, or split into separate SSM invocations
- **SSM TimeoutSeconds minimum is 30** — values below 30 cause parameter validation errors
- **S3 event notifications encode spaces as `+`** — scanner must use `urllib.parse.unquote_plus()`, NOT `unquote()`. Using `unquote` silently fails on files with spaces in their names (the scanner tries to download a key with literal `+` characters that doesn't exist)
- **IAM roles need `s3:ListBucket` on source buckets** — without it, S3 returns `AccessDenied` instead of `NoSuchKey` when a file doesn't exist, causing infinite retry loops on duplicate SQS messages. Both `ScannerAppRole` and `ReviewScannerAppRole` include this permission
- **S3 `copy_object` onto itself does NOT trigger event notifications** — even with `s3:ObjectCreated:*` configured. Cannot "touch" files to re-trigger processing; must re-upload from an external source or use the reconciliation feature

### Cleanup & Lifecycle
- **Pre-delete cleanup Lambda** — `CleanupLambda` runs automatically during stack deletion. It deletes LB-controller-created load balancers/target groups/security groups (tagged `elbv2.k8s.aws/cluster=<cluster>` — the scanner NLB/ALB would otherwise orphan and block VPC deletion) and orphaned EBS volumes BEFORE CloudFormation tears down the VPC
- **Review pipeline resources are cleaned up with the stack** — review SQS queues, review DLQ remediation Lambda, and review audit log group are all CloudFormation-managed and deleted automatically during stack deletion
- **Existing-bucket mode never touches the user bucket on teardown** — the EventBridge-enable custom resource is a no-op on Delete; the bucket, its objects, and its notification configuration are left exactly as found
- **Orphaned EBS volumes after stack deletion** — V1FS PVCs (100 GB gp3 each) persist after stack deletion. Always check: `aws ec2 describe-volumes --filters Name=status,Values=available`

## Scaling Architecture (Trend-Aligned)

This deployment is deliberately aligned with TrendAI's supported methodology. Two guardrails to preserve it: **do not point KEDA at the chart-owned V1FS scanner** (its own HPA is the supported mechanism — KEDA scales only our scanner-app), and **do not add a node autoscaler other than the Cluster Autoscaler**.

### Three scaling layers

| Layer | Mechanism | Bounds | Why |
|---|---|---|---|
| V1FS scanner pods | Chart-native HPA (CPU 80% + memory 80%) | `ScannerMinReplicas`/`ScannerMaxReplicas` (1/10) | TrendAI's supported autoscaling. The scanner is memory-bound, so the memory target tracks load |
| scanner-app pods (optional module) | KEDA on SQS depth | 1–`ScannerAppMaxReplicas` (20) | Our own component — queue depth is the natural signal; user-owned territory, invisible to Trend supportability |
| Nodes | Cluster Autoscaler on the managed node group | `NodeGroupMinSize`–`NodeGroupMaxSize` (2–8) | The standard Kubernetes default; Trend docs leave node scaling to the user |

### Key configuration locations

- **Chart values**: `helm/values-base.yaml` (single source of truth), `helm/values-nlb.yaml` (NLB endpoint overlay). HPA replica bounds passed via `--set` from CFN params in `scripts/bootstrap.sh`
- **Cluster Autoscaler IAM**: `ClusterAutoscalerRole` + Pod Identity in `eks-v1fs.yaml`. ASG discovery uses the `k8s.io/cluster-autoscaler/*` tags EKS applies to managed node group ASGs automatically — no manual tagging
- **PDBs**: `k8s/pdb.yaml` — scanner-app (maxUnavailable 25%), V1FS scanner (minAvailable 1) — protect against CA node drains
- **Scale-down**: CA `scale-down-unneeded-time=2m`, expander `least-waste`

### Node group sizing

Single node group, r7i.xlarge (4 vCPU / 32 GiB), min 2 for CoreDNS/AZ redundancy. ~3.6 usable vCPU per node after kubelet + daemonsets. Full-mode peak (10 scanners × 800m + 20 scanner-app × 500m + review + system) ≈ 26 vCPU → max 8 nodes. CPU binds before memory on r-class (1:8 ratio vs the scanner's 1:2.5 request ratio).

### Deploy script rollout timeout

The deploy script (`scripts/deploy.sh`) waits up to 300s for the scanner-app rollout, but treats timeout as a **warning, not a failure**. On first deployment, Cluster Autoscaler may need to provision a node before the scanner-app pod can schedule (1–3 min). The bastion signals SUCCESS to CloudFormation regardless, and the pod starts once the node is ready.

## Performance Characteristics

- **V1FS scanner is I/O and memory bound, not CPU bound** — the scanning engine loads signature databases into memory and spends most time on network I/O (gRPC) and disk operations. This is why the chart HPA's memory target (80%) matters as much as its CPU target, and why KEDA scales our scanner-app on queue depth rather than CPU
- **HPA + CA scale-up takes 1–3 minutes** under sustained load — expected behavior for the supported configuration. Set evaluation throughput expectations accordingly; this deployment is tuned for correctness and supportability, not burst benchmarks
- **Memory-optimized xlarge instances only** — x86 (r7i/r7a/r6i xlarge, r7i.2xlarge) or Graviton ARM (r8g/r7g xlarge). 32 GiB per node provides headroom for signature databases; non-burstable CPU gives consistent performance; four 800m/2Gi scanner pods bin-pack per node. Graviton runs the identical pipeline at 11–19% lower node cost
- **Rewriting scanner-app in Go would not improve throughput** — the bottleneck is the V1FS scanner backend and network round-trips, not the Python runtime. The app spends nearly all time waiting on I/O
- **gRPC scan timeout is configurable** — the V1FS SDK reads `TM_AM_SCAN_TIMEOUT_SECS` from environment (default 300s). Set to 600s in the configmap to prevent "Deadline Exceeded" on complex files. Files exceeding the timeout go to DLQ after 3 retries
- **Cleanup Lambda for graceful stack deletion** — `CleanupLambda` in `eks-v1fs.yaml` deletes LB-controller-created load balancers/target groups/SGs and orphaned EBS volumes during stack deletion. Users can simply run `aws cloudformation delete-stack` without manual cleanup
- **DLQ remediation Lambda** — `DLQRemediationLambda` in `eks-v1fs.yaml` auto-re-queues failed messages with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard. Scan failures that are transient (network blips, scanner restarts) recover automatically
- **CloudWatch Alarms** — DLQ alarm (any messages > 0) and Queue Age alarm (oldest message > 20 min for 5 consecutive minutes) alert via SNS topic. Subscribe to the topic to receive notifications
- **CloudWatch Dashboard** — `scanner-${StackName}`, created only with the scanner-app module. Covers queue health, scan throughput/latency (Logs Insights), malware detection stats, DLQ remediation, pod distribution, and recent scan results. CFN-managed, created/deleted with the stack. Dashboard URL is in stack outputs

