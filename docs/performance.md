# Performance and Scaling

## Autoscaling Configuration

### KEDA (scanner-app pods)
- ScaledObject: `scanner-app-sqs-scaler`
- Trigger: `aws-sqs-queue` on `ApproximateNumberOfMessages`
- Queue length target: 5 messages per pod
- Scale on in-flight: true
- Polling interval: 10s
- Cooldown: 90s
- Range: 1-150 pods
- Auth: `provider: aws`, `identityOwner: keda` (uses KEDA operator's Pod Identity)

### KEDA (V1FS scanner pods)
- ScaledObject: `v1fs-scanner-sqs-scaler`
- Trigger: `aws-sqs-queue` on `ApproximateNumberOfMessages`
- Queue length target: 50 messages per pod
- Scale on in-flight: true
- Polling interval: 10s
- Cooldown: 90s
- Range: 1-150 pods
- Auth: same `sqs-trigger-auth` TriggerAuthentication
- **Replaces the original CPU-based HPA** which only scaled to 4 pods under heavy load. Helm chart `scanner.autoscaling.enabled` must be `false` to prevent HPA/KEDA conflicts.

### Karpenter (replaces Cluster Autoscaler)
- Provisions nodes directly via EC2 Fleet API (30-60s vs 1-2min with Cluster Autoscaler)
- Flexible instance types: r7i.xlarge, r7a.xlarge, r6i.xlarge (xlarge only, on-demand only)
- Consolidation policy: WhenEmptyOrUnderutilized, 2-minute consolidation delay
- Disruption budget: max 10% of nodes consolidated simultaneously
- CPU limit: 300 (matches vCPU quota)
- Memory limit: 2,400 GiB (proportional to CPU)
- Uses CloudFormation-managed instance profile (`instanceProfile` not `role` in EC2NodeClass) — prevents orphaned instance profiles on stack deletion
- Managed node group (max 6 nodes) reserved for system components only
- PodDisruptionBudgets protect active scan workloads during consolidation

## Scanner-App Settings
- `MAX_CONCURRENT_SCANS`: 50 per pod
- At max scale: 150 pods × 50 concurrent = 7,500 scan slots
- Pod resources: 500m/512Mi requests, 1000m/1024Mi limits
- Health probes: liveness `/healthz`, readiness `/readyz` on port 8080
- Scan audit trail: structured JSON to CloudWatch Logs (`scan-audit-${StackName}`), batched writes

## V1FS Scanner Settings
- Pod resources: 800m CPU / 2Gi memory
- Fits 4 scanner pods per xlarge node (4 vCPU each)
- No Prometheus /metrics endpoint — custom I/O metrics not available
- Scan cache: caches results by file hash. Same files on the same stack produce artificially fast results

## Performance Characteristics
- **Scanner is I/O and memory bound, not CPU bound** — CPU stays below 4% even at 150+74 pods. The bottleneck is the V1FS scan engine analysis time
- **Real malware scan latency** (eks-v1fs-27, fresh stack, no cache): p50 = 7.2s, avg = 16.1s, p90 = 43.4s, p95 = 59.5s, p99 = 92.4s, max = 131.6s
- **Cached scan latency**: p50 = 28ms (cache lookup, not real analysis)
- **KEDA scaling speed**: 1 → 150 pods in 105 seconds (doubles every ~15s)
- **Karpenter provisioning**: 39 r6i.xlarge nodes in ~2 minutes via EC2 Fleet API

## Review Pipeline Scaling

The review pipeline handles low-volume deep analysis of files that exceeded decompression limits.

- **ScaledObject**: `review-scanner-app-sqs-scaler` — scales review-scanner-app pods based on review SQS queue depth
- **ScaledObject**: `review-v1fs-scanner-sqs-scaler` — scales review V1FS scanner pods based on review SQS queue depth
- Both: min 0, max 5, threshold 50 messages per pod, cooldown 300s
- **Scale to zero when idle** — no pods running when the review queue is empty, keeping costs near zero
- **No PDB** — the review pipeline is low-volume and does not need Karpenter consolidation protection

## Observability
- **CloudWatch Dashboard**: `scanner-${StackName}`, 32 widgets (queue health, throughput, latency, detection stats, pod distribution, recent scans, review pipeline metrics). CFN-managed
- **CloudWatch Alarms**: DLQ messages (any > 0), Queue Age (> 20 min for 5 consecutive minutes), Review DLQ messages (any > 0), via SNS topic
- **DLQ Remediation Lambda**: auto re-queues with backoff (60s/300s/900s), max 3 DLQ retries before permanent discard
- **Review DLQ Remediation Lambda**: same retry logic, handles review pipeline failures independently
