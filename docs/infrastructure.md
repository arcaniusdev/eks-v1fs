# Infrastructure (provisioned by CloudFormation)

All resources are created by `eks-v1fs.yaml`. The scanner application should NOT create or manage any of these — it consumes them via environment variables.

## Deployment Modes (July 2026 realignment)

The stack is modular. Four parameters control which resources exist:

| Parameter | Default | Effect |
|---|---|---|
| `DeployScannerApp` | `true` | Deploys the S3/SQS scanning application module. When `false`, only the V1FS scanner is deployed and its gRPC endpoint is published for external scanning applications |
| `DeployReviewPipeline` | `false` | Deploys the review pipeline (second V1FS release with unlimited decompression). A CloudFormation Rule enforces `DeployScannerApp=true` when this is enabled |
| `ExistingIngestBucket` | `""` (create new) | When set to an existing bucket name, the stack wires that bucket via S3 → EventBridge → SQS instead of creating an ingest bucket, and the scanner tags source objects instead of deleting them |
| `ScannerEndpointMode` | `nlb` | How the scanner gRPC endpoint is exposed externally: `nlb` (internal NLB, default), `alb` (ALB Ingress with TLS — a Rule requires `ACMCertificateArn` and `ScannerDomain`), or `none` (in-cluster only) |

Conditional resources by mode:

- **`ScannerAppEnabled`** (`DeployScannerApp=true`): ECR repo, clean/quarantine buckets, `FileScanQueue`/`FileScanDLQ` + queue policy, `ScannerAppRole` + Pod Identity, `ScanAuditLogGroup`, SNS alarm topic + DLQ/queue-age alarms, DLQ remediation Lambda, CloudWatch dashboard, KEDA Helm install + `KedaOperatorRole`
- **`CreateIngestBucket`** (scanner app enabled AND `ExistingIngestBucket` empty): the stack-owned ingest bucket with direct S3 → SQS notification
- **`UseExistingBucket`** (scanner app enabled AND `ExistingIngestBucket` set): `EventBridgeIngestRule`, `EnableEventBridgeFunction` merge Lambda + custom resource, EventBridge statement on the SQS queue policy
- **`ReviewEnabled`** (`DeployReviewPipeline=true`): review bucket, review queues/DLQ, `ReviewScannerAppRole`, review audit log group, review DLQ Lambda/alarm, `rv` Helm release
- **`ExposeEndpoint`** (`nlb` or `alb`): node security group ingress rules for gRPC/ICAP, endpoint publication to SSM

Removed parameters: `DesiredCapacity`, `KarpenterCPULimit`. **There is no in-place migration path from Karpenter-era stacks — delete and redeploy.**

## Networking
- **VPC**: `10.2.0.0/16` with DNS support and hostnames enabled
- **Public subnets**: `10.2.1.0/24` (AZ1), `10.2.2.0/24` (AZ2) — bastion, NAT gateways
- **Private subnets**: `10.2.3.0/24` (AZ1), `10.2.4.0/24` (AZ2) — EKS nodes
- **NAT Gateways**: one per AZ for HA; private subnet pods reach the internet through these
- **VPC Flow Logs**: all traffic logged to CloudWatch (90-day retention)
- **Endpoint-mode ingress rules** (`ExposeEndpoint` condition): `NodeSecGroupScannerGrpc` (TCP 50051) and `NodeSecGroupScannerIcap` (TCP 1344) allow `10.2.0.0/16` into the node security group so load balancer ip-targets can reach scanner pod ENIs

## EKS Cluster
- **Cluster name**: `EKSCluster-{StackName}`
- **API endpoint**: private only (`EndpointPublicAccess: false`)
- **Authentication**: `API_AND_CONFIG_MAP` mode with EKS Access Entries
- **Logging**: API, audit, authenticator, controller manager, scheduler — all enabled
- **Single managed node group**: hosts BOTH system components and scanner workloads (no Karpenter, no node tiers, no nodeAffinity). `NodeInstanceType` default `r7i.xlarge` (allowed: r7i.xlarge, r7a.xlarge, r6i.xlarge, r7i.2xlarge — memory-optimized, 4 vCPU / 32 GiB fits four 800m/2Gi V1FS scanner pods per node). Sizing: `NodeGroupMinSize`/`NodeGroupDesiredSize`/`NodeGroupMaxSize` = 2/2/8. The default max of 8 fits full-mode peak (10 V1FS scanners + 20 scanner-app pods + review pipeline + system pods)
- **Cluster Autoscaler** scales the node group (see Kubernetes Resources below). EKS auto-tags the node group's ASG with `k8s.io/cluster-autoscaler/enabled` and `k8s.io/cluster-autoscaler/<cluster-name>`, so ASG auto-discovery works with no extra tagging resources
- **IMDSv2**: enforced on nodes (`HttpTokens: required`)
- **EBS**: encrypted, gp3 volumes on all instances
- **Managed addons**: vpc-cni, eks-pod-identity-agent, coredns, kube-proxy, aws-ebs-csi-driver, aws-efs-csi-driver

## Kubernetes Resources (created by scripts/bootstrap.sh)

Bastion UserData is now thin: it exports CloudFormation-derived environment variables (deployment modes, scaling params, resource identifiers), clones the repo, and runs `scripts/bootstrap.sh`, which contains all provisioning logic. Mode conditionals are plain shell on `DEPLOY_SCANNER_APP` / `DEPLOY_REVIEW` / `ENDPOINT_MODE` / `EXISTING_INGEST_BUCKET`.

- **Namespace**: `visionone-filesecurity`
- **Secrets**: `token-secret` (V1FS registration token from Secrets Manager), `device-token-secret`
- **LB Controller Helm release**: `aws-load-balancer-controller` in `kube-system` — provisions the scanner NLB/ALB when an endpoint mode is enabled
- **Metrics Server**: installed in `kube-system` — REQUIRED by the V1FS chart's HPA (CPU/memory metrics)
- **Cluster Autoscaler Helm release**: `cluster-autoscaler` from `autoscaler/cluster-autoscaler` in `kube-system`. IAM via Pod Identity (`ClusterAutoscalerRole`), ASG auto-discovery via the EKS auto-applied tags, `expander=least-waste`, `balance-similar-node-groups=true`, `scale-down-unneeded-time=2m`
- **KEDA Helm release**: `keda` in `keda` namespace — installed ONLY when `DeployScannerApp=true`. KEDA scales only our scanner-app deployments, never the chart-owned V1FS scanner
- **Scanner Helm release**: `my-release` from `visionone-filesecurity/visionone-filesecurity`, chart version pinned to **1.4.10**, installed with `-f helm/values-base.yaml` (the single source of truth for chart values) plus `--set scanner.autoscaling.minReplicas/maxReplicas` from the `ScannerMinReplicas`/`ScannerMaxReplicas` CFN parameters. The chart's own HPA is ENABLED (`scanner.autoscaling.enabled=true`, CPU 80% / memory 80% targets) — this is the TrendAI-supported autoscaling mechanism. PostgreSQL database enabled (`dbEnabled: true`) on gp3 EBS storage, scanner ephemeral volume on EFS (ReadWriteMany, 100Gi). Both chart ingresses (scanner and management service) are explicitly disabled in `values-base.yaml` because they default to enabled in the chart; ALB mode re-enables the scanner ingress with its own settings. Scanner resources (800m CPU / 2Gi memory) are now chart defaults — no override
- **Endpoint exposure overlays**: `nlb` mode adds `-f helm/values-nlb.yaml` (chart-native `scanner.externalService` as an internal NLB); `alb` mode adds `--set scanner.ingress.*` values (className=alb, GRPC backend-protocol-version, internal scheme, ACM certificate)
- **StorageClasses**: `gp3` (EBS CSI, encrypted, WaitForFirstConsumer) and `efs-sc` (EFS CSI, access point provisioning, TLS) — both created by bootstrap
- **Review scanner Helm release** (only when `DeployReviewPipeline=true`): `rv` from the same chart/version in `visionone-review` namespace, same `values-base.yaml`, chart HPA min 1 / max 3. No CLISH scan policy applied — runs with unlimited decompression for deep analysis. Shares the same `token-secret`. A separate namespace is required because the chart creates a ServiceAccount named `visionone-filesecurity` that conflicts if both releases share a namespace
- **KEDA ScaledObjects** (only our scanner-app deployments; `provider: aws`, `identityOwner: keda`):
  - `scanner-app-sqs-scaler` — 1 pod per 5 messages, min 1 / max `ScannerAppMaxReplicas` (default 20), polling 5s, cooldown 300s
  - `review-scanner-app-sqs-scaler` — 1 pod per 50 messages, min 1 / max 5, polling 5s, cooldown 300s (review mode only)
  - The former ScaledObjects targeting the chart-owned V1FS scanner are DELETED — the chart HPA scales V1FS pods

## Scanner Endpoint Exposure

For external scanning applications (including `DeployScannerApp=false` stacks):

- **`nlb` (default)**: chart-native `scanner.externalService` creates `my-release-visionone-filesecurity-scanner-lb`, an INTERNAL Network Load Balancer with gRPC `:50051` and ICAP `:1344`. VPC-reachable only (or peered networks), never internet-facing. SDK: `amaas.grpc.init("<nlb-hostname>:50051", api_key, False)` (plaintext in-VPC)
- **`alb`**: chart `scanner.ingress` with `className=alb`, `backend-protocol-version=GRPC`, `target-type=ip`, `scheme=internal`, ACM certificate, HTTPS 443. Requires `ACMCertificateArn` and `ScannerDomain`; the operator must create a DNS CNAME from `ScannerDomain` to the ALB hostname. SDK: `amaas.grpc.init("<scanner-domain>:443", api_key, True)` (TLS)
- **SSM publication**: the bastion waits (up to 10 minutes) for the load balancer hostname and publishes the endpoint address to SSM Parameter Store at `/<stack-name>/scanner-endpoint`. `BastionRole` has `ssm:PutParameter` scoped to this parameter

## ECR Repository
- Conditional on `ScannerAppEnabled`. Auto-generated name; scan-on-push enabled; AES256 encryption; **image tag immutability enabled** — each push requires a unique tag (git SHA); `EmptyOnDelete: true` so it is emptied and deleted with the stack. Check `ECRRepoUrl` output.

## S3 Buckets
- **Ingest Bucket** (`CreateIngestBucket` only): receives incoming files; `s3:ObjectCreated:*` event notification wired directly to SQS; versioning enabled; AES256 encryption; public access blocked; **7-day lifecycle policy** expires unprocessed objects. Check `IngestBucketName` output. Not created when `ExistingIngestBucket` is set
- **Clean Bucket** (`ScannerAppEnabled`): destination for files that pass scanning; AES256 encryption; public access blocked. Check `CleanBucketName` output.
- **Quarantine Bucket** (`ScannerAppEnabled`): destination for malicious files — and, when the review pipeline is NOT deployed, for files that exceeded decompression limits (tagged `ScanResult=S3-DecompressionLimit` + `ScanErrors=<error names>`); versioning enabled; AES256 encryption; public access blocked.
- **Review Bucket** (`ReviewEnabled` only): destination for files that scanned clean but exceeded decompression limits; AES256 encryption; public access blocked; `s3:ObjectCreated:*` event notification wired to the Review SQS queue for automatic re-scanning.

All stack-created S3 buckets have `DeletionPolicy: Retain` — they survive stack deletion to preserve files. The ECR repository is deleted with the stack.

## Existing-Bucket Wiring (S3 → EventBridge → SQS)

When `ExistingIngestBucket` is set, the stack must not touch the customer's bucket notification configuration destructively (`put-bucket-notification-configuration` is a full-replace API). Instead:

- **`EventBridgeIngestRule`** (`AWS::Events::Rule`): matches `source=aws.s3`, `detail-type="Object Created"`, filtered on the bucket name; targets `FileScanQueue`. The SQS queue policy gains an `events.amazonaws.com` statement scoped to this rule's ARN
- **`EnableEventBridgeFunction`** (custom-resource Lambda): reads the bucket's current notification configuration and MERGES `EventBridgeConfiguration` into it — every existing Queue/Topic/Lambda configuration is preserved. If EventBridge is already enabled, it is a no-op. On stack **Delete it is a NO-OP** — the customer's bucket is never mutated on teardown
- **Scanner behavior**: `DELETE_SOURCE_ENABLED=false` — the scanner tags source objects with the verdict via `put_object_tagging` and NEVER deletes them. `ScannerAppRole` swaps `s3:DeleteObject` for `s3:PutObjectTagging` on the bucket. Reconciliation is forced off (objects legitimately remain in the bucket)

## EFS
- **EFS Filesystem**: encrypted at rest, general purpose performance mode, elastic throughput. Used by V1FS scanner pods for shared ephemeral storage (ReadWriteMany).
- **Mount Targets**: one per private subnet (AZ1, AZ2), attached to the EFS security group (NFS TCP 2049 from node SG only).
- **EFS CSI Driver**: EKS managed addon. IAM role via Pod Identity for `efs-csi-controller-sa` in `kube-system`.
- **EBS CSI Driver**: EKS managed addon. IAM role via Pod Identity for `ebs-csi-controller-sa` in `kube-system` (AWS managed policy `AmazonEBSCSIDriverPolicy`). Required for the gp3 StorageClass.

## SQS (conditional on ScannerAppEnabled)
- **FileScanQueue**: standard queue; SSE encrypted; visibility timeout = `ScanTimeoutSeconds` (default 600s); 20s long polling; 4-day retention; receives S3 events directly (created bucket) or via EventBridge (existing bucket). Check `FileScanQueueUrl` output.
- **FileScanDLQ**: dead letter queue; SSE encrypted; 14-day retention; receives messages after 3 failed processing attempts; 120s visibility timeout (must be >= the remediation Lambda's 60s timeout).
- **ReviewScanQueue** / **ReviewScanDLQ** (`ReviewEnabled` only): same pattern for the review bucket.

## Observability (CloudFormation-managed, conditional on ScannerAppEnabled)
- **ScanAuditLogGroup**: CloudWatch log group `scan-audit-${StackName}` for main scanner audit trail, 30-day retention
- **ReviewAuditLogGroup** (`ReviewEnabled`): `review-audit-${StackName}`, 30-day retention
- **DLQRemediationLambda**: re-queues failed messages from `FileScanDLQ` with exponential backoff (60s/300s/900s), max 3 DLQ retries before permanent discard
- **ReviewDLQRemediationLambda** (`ReviewEnabled`): same retry logic for the review pipeline
- **CloudWatch Alarms**: DLQ messages (any > 0), Queue Age (> 20 min for 5 consecutive minutes), Review DLQ messages when review enabled, via SNS topic
- **CloudWatch Dashboard**: `scanner-${StackName}` covering queue health, scan throughput/latency, malware detection stats, DLQ remediation, pod distribution, recent scans, and review pipeline metrics

## IAM
- **ScannerAppRole** (`ScannerAppEnabled`): least-privilege, bound to `scanner-app` SA in `visionone-filesecurity` via Pod Identity. Permissions: SQS poll/delete/visibility AND `sqs:SendMessage` on FileScanQueue (SendMessage supports reconciliation when the review pipeline — which otherwise runs it — is absent); S3 access to the ingest bucket depends on mode — created bucket: `s3:GetObject` + `s3:DeleteObject`; existing bucket: `s3:GetObject` + `s3:PutObjectTagging` (never delete); `s3:ListBucket` on the ingest bucket; S3 put/tag on clean and quarantine; S3 put/tag on review only when `ReviewEnabled`; Secrets Manager read on the API key secret; CloudWatch Logs create stream / put events for the audit trail.
- **ReviewScannerAppRole** (`ReviewEnabled`): bound to `review-scanner-app` SA in `visionone-review` via Pod Identity. SQS poll/delete/visibility on ReviewScanQueue; S3 get/delete on review bucket; S3 put/tag on clean and quarantine; Secrets Manager read; CloudWatch Logs for review audit trail; S3 list on ingest bucket and SQS send on main FileScanQueue for reconciliation. NO write permission to the review bucket — prevents routing loops.
- **ClusterAutoscalerRole**: bound to `cluster-autoscaler` in `kube-system` via Pod Identity. Read-only autoscaling/EC2/EKS discovery, plus `autoscaling:SetDesiredCapacity` / `TerminateInstanceInAutoScalingGroup` restricted by condition to ASGs tagged `k8s.io/cluster-autoscaler/enabled=true`.
- **KedaOperatorRole** (`ScannerAppEnabled`): bound to `keda-operator` in `keda` via Pod Identity. SQS GetQueueAttributes/GetQueueUrl (read-only) on the scan queue(s) — the review queue is included only when `ReviewEnabled`.
- **EnableEventBridgeRole** (`UseExistingBucket`): Lambda role with `s3:GetBucketNotification`/`s3:PutBucketNotification` scoped to the customer bucket.
- **Node role**: EKS worker node policy, CNI policy, ECR read-only, SSM, CloudWatch Logs.
- **Bastion role**: EKS describe/access, ECR read/push, STS, CloudFormation signal/describe, Secrets Manager read, `ssm:PutParameter` on `/<stack>/scanner-endpoint`, S3 ingest write (created-bucket mode only).
- **EBSCSIDriverRole** / **EFSCSIDriverRole**: as before (managed policy / EFS access point CRUD).

All Karpenter IAM (controller role, node instance profile) is REMOVED.

## Pre-Delete Cleanup Lambda

`CleanupLambda` runs automatically during stack deletion (via a custom resource whose `DependsOn` forces it to run before node/VPC teardown). It deletes, in order:
1. Load balancers tagged `elbv2.k8s.aws/cluster=<cluster-name>` (the LB-controller-created scanner NLB/ALB), then waits 30s for ENI release
2. Target groups with the same tag
3. Security groups with the same tag
4. Orphaned `available` EBS volumes tagged `kubernetes.io/cluster/<cluster-name>=owned` (V1FS PVCs)

All Karpenter cleanup logic (instance termination, instance profile cleanup) is gone — there are no Karpenter resources to clean up. Errors are non-fatal; the Lambda always signals SUCCESS so stack deletion is never blocked.

## Access
- **Bastion host**: Ubuntu `t3.medium` in public subnet with AWS CLI v2, kubectl, helm, eksctl installed by bootstrap. Kubeconfig and KUBECONFIG env var pre-configured. Access via AWS Systems Manager Session Manager (primary). SSH key stored in SSM Parameter Store for emergency use only.
- **All tools on bastion**: `/usr/local/bin` (aws, kubectl, eksctl) or `/usr/bin` (helm). PATH configured via `/etc/profile.d/local-bin.sh`.
