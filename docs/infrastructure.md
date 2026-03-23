# Infrastructure (provisioned by CloudFormation)

All resources are created by `eks-v1fs.yaml`. The scanner application should NOT create or manage any of these — it consumes them via environment variables.

## Networking
- **VPC**: `10.2.0.0/16` with DNS support and hostnames enabled
- **Public subnets**: `10.2.1.0/24` (AZ1), `10.2.2.0/24` (AZ2) — bastion, NAT gateways
- **Private subnets**: `10.2.3.0/24` (AZ1), `10.2.4.0/24` (AZ2) — EKS nodes
- **NAT Gateways**: one per AZ for HA; private subnet pods reach the internet through these
- **VPC Flow Logs**: all traffic logged to CloudWatch (90-day retention)

## EKS Cluster
- **Cluster name**: `EKSCluster-{StackName}`
- **API endpoint**: private only (`EndpointPublicAccess: false`)
- **Authentication**: `API_AND_CONFIG_MAP` mode with EKS Access Entries
- **Logging**: API, audit, authenticator, controller manager, scheduler — all enabled
- **Managed node group**: `r7i.large` instances in private subnets, min 3 / max 6, default 3. Hosts system components only (CoreDNS, KEDA, Karpenter, CSI drivers, LB controller, metrics server). Scanner workloads run on Karpenter-provisioned xlarge nodes (r7i.xlarge, r7a.xlarge, r6i.xlarge — 4 vCPU, 32 GiB each).
- **IMDSv2**: enforced on nodes (`HttpTokens: required`)
- **EBS**: encrypted, gp3 volumes on all instances
- **Managed addons**: vpc-cni, eks-pod-identity-agent, coredns, kube-proxy, aws-ebs-csi-driver, aws-efs-csi-driver

## Kubernetes Resources (created by bastion UserData)
- **Namespace**: `visionone-filesecurity`
- **Secrets**: `token-secret` (V1FS registration token from Secrets Manager), `device-token-secret`
- **Metrics Server**: installed in `kube-system` — provides CPU/memory metrics for cluster monitoring
- **Scanner Helm release**: `my-release` from `visionone-filesecurity/visionone-filesecurity` chart, with scanner HPA disabled (scaling handled by KEDA), scanner resources set to 800m CPU / 2Gi memory, PostgreSQL database enabled (`dbEnabled: true`) on gp3 EBS storage, scanner ephemeral volume on EFS (ReadWriteMany)
- **StorageClasses**: `gp3` (EBS CSI, encrypted, WaitForFirstConsumer) and `efs-sc` (EFS CSI, access point provisioning, TLS) — both created by bastion UserData
- **LB Controller Helm release**: `aws-load-balancer-controller` in `kube-system`
- **Karpenter Helm release**: `karpenter` in `kube-system` — provisions scanner workload nodes directly via EC2 Fleet API. NodePool `scanner-pool` and EC2NodeClass `scanner-nodes` applied inline in bastion UserData. EC2NodeClass uses `instanceProfile` (not `role`) referencing the CloudFormation-managed `KarpenterNodeInstanceProfile` — this ensures clean deletion with the stack
- **KEDA Helm release**: `keda` in `keda` namespace — scales scanner app replicas based on SQS queue depth
- **Review scanner Helm release**: `review-release` from `visionone-filesecurity/visionone-filesecurity` chart, same custom values as `my-release` (HPA disabled, 800m CPU / 2Gi memory, dbEnabled, EFS ephemeral volume). No CLISH scan policy applied — runs with unlimited decompression for deep analysis. Shares the same `token-secret` as the main release
- **KEDA ScaledObjects**: Two SQS-driven scalers sharing the same TriggerAuthentication (`provider: aws`, `identityOwner: keda`):
  - `scanner-app-sqs-scaler` — 1 pod per 5 messages, min 1 / max 150, polling 10s, cooldown 90s
  - `v1fs-scanner-sqs-scaler` — 1 pod per 50 messages, min 1 / max 150, polling 10s, cooldown 90s
  - `review-scanner-app-sqs-scaler` — 1 pod per 50 messages, min 0 / max 5, cooldown 300s, scale to zero when idle
  - `review-v1fs-scanner-sqs-scaler` — 1 pod per 50 messages, min 0 / max 5, cooldown 300s, scale to zero when idle
- **ReviewAuditLogGroup**: CloudWatch log group `review-audit-${StackName}` for review scan results, 30-day retention
- **ReviewDLQRemediationLambda**: same retry logic as the main DLQ Lambda, handles review pipeline failures independently with exponential backoff (60s/300s/900s)

## ECR Repository
- Named `scanner-app-{StackName}`; scan-on-push enabled; AES256 encryption; **image tag immutability enabled** — each push requires a unique tag (git SHA). Deleted with the stack. Check `ECRRepoUrl` output.

## S3 Buckets
- **Ingest Bucket**: receives incoming files; `s3:ObjectCreated:*` event notification wired to SQS; versioning enabled; AES256 encryption; public access blocked; **7-day lifecycle policy** expires unprocessed objects. Check `IngestBucketName` output.
- **Clean Bucket**: destination for files that pass scanning; AES256 encryption; public access blocked. Check `CleanBucketName` output.
- **Quarantine Bucket**: destination for malicious files; versioning enabled; AES256 encryption; public access blocked.
- **Review Bucket**: destination for files that scanned clean but exceeded decompression limits (nesting depth, file count, compression ratio, or decompressed size); AES256 encryption; public access blocked. Requires manual review.

All four S3 buckets have `DeletionPolicy: Retain` — they survive stack deletion to preserve files. The ECR repository is deleted with the stack.

## EFS
- **EFS Filesystem**: encrypted at rest, general purpose performance mode, elastic throughput. Used by V1FS scanner pods for shared ephemeral storage (ReadWriteMany).
- **Mount Targets**: one per private subnet (AZ1, AZ2), attached to the EFS security group (NFS TCP 2049 from node SG only).
- **EFS CSI Driver**: EKS managed addon. IAM role via Pod Identity for `efs-csi-controller-sa` in `kube-system`.
- **EBS CSI Driver**: EKS managed addon. IAM role via Pod Identity for `ebs-csi-controller-sa` in `kube-system` (AWS managed policy `AmazonEBSCSIDriverPolicy`). Required for the gp3 StorageClass.

## SQS
- **FileScanQueue**: standard queue; SSE encrypted; 600s visibility timeout; 20s long polling; 4-day retention; wired to receive S3 events from the ingest bucket. Check `FileScanQueueUrl` output.
- **FileScanDLQ**: dead letter queue; SSE encrypted; 14-day retention; receives messages after 3 failed processing attempts.
- **ReviewScanQueue**: standard queue; SSE encrypted; 600s visibility timeout; 20s long polling; 4-day retention; wired to receive S3 events from the review bucket. Check `ReviewScanQueueUrl` output.
- **ReviewScanDLQ**: dead letter queue; SSE encrypted; 14-day retention; receives messages after 3 failed review scanning attempts.

## IAM
- **ScannerAppRole**: least-privilege, bound to `scanner-app` SA in `visionone-filesecurity` via Pod Identity. Permissions: SQS poll/delete/visibility on FileScanQueue; S3 get/delete on ingest; S3 put/tag on clean, review, and quarantine; Secrets Manager read on API key secret; CloudWatch Logs create stream and put events for scan audit trail.
- **Node role**: EKS worker node policy, CNI policy, ECR read-only, SSM, CloudWatch Logs.
- **Bastion role**: EKS describe/access, ECR read/push, STS, CloudFormation signal/describe, Secrets Manager read.
- **KedaOperatorRole**: bound to `keda-operator` in `keda` via Pod Identity. SQS GetQueueAttributes/GetQueueUrl (read-only).
- **KarpenterControllerRole**: bound to `karpenter` in `kube-system` via Pod Identity. EC2 fleet/instance management, IAM instance profile read-only (`iam:GetInstanceProfile`), SSM parameter read, pricing read, EKS describe. Instance profile is CloudFormation-managed (`KarpenterNodeInstanceProfile` resource), not dynamically created by Karpenter.
- **EBSCSIDriverRole**: bound to `ebs-csi-controller-sa` in `kube-system`. AWS managed policy.
- **EFSCSIDriverRole**: bound to `efs-csi-controller-sa` in `kube-system`. EFS access point CRUD, describe, tag; EC2 describe AZs.
- **ReviewScannerAppRole**: bound to `review-scanner-app` SA in `visionone-filesecurity` via Pod Identity. Permissions: SQS poll/delete/visibility on ReviewScanQueue; S3 get/delete on review bucket; S3 put/tag on clean and quarantine; Secrets Manager read on API key secret; CloudWatch Logs for review audit trail. Has NO write permission to the review bucket — this prevents routing loops back to the review pipeline.

## Access
- **Bastion host**: Ubuntu `t3.medium` in public subnet with AWS CLI v2, kubectl, helm, eksctl pre-installed. Kubeconfig and KUBECONFIG env var pre-configured. Access via AWS Systems Manager Session Manager (primary). SSH key stored in SSM Parameter Store (`BastionKeySSMParameter` output) for emergency use only.
- **All tools on bastion**: `/usr/local/bin` (aws, kubectl, eksctl) or `/usr/bin` (helm). PATH configured via `/etc/profile.d/local-bin.sh`.
