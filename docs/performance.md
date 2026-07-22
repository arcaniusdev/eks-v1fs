# Performance and Scaling

## Autoscaling Architecture (`ScannerScalingMode`)

The V1FS scanner autoscales one of two ways, chosen by the `ScannerScalingMode` parameter:

- **`hpa` (default, TrendAI-supported)** — the chart's own CPU/memory HPA (targets 80%/80%, min/max from `ScannerMinReplicas`/`ScannerMaxReplicas`). Used by the **python-default** option.
- **`keda`** — KEDA scales the scanner on SQS **queue depth** so the fleet tracks the backlog; the chart HPA is disabled. Used by the **python-KEDA** / **java-KEDA** options. A customer variant, *not* Trend-supported; `scripts/upgrade.py` enforces exactly one autoscaler and fails an upgrade if a chart HPA reappears alongside KEDA.

Either way, KEDA scales our scanner-app (full-auto) on queue depth, and nodes scale via the standard Cluster Autoscaler on a single managed node group.

### `keda` mode — V1FS scanner pods (queue depth)
- ScaledObject: `v1fs-scanner-sqs-scaler` (`k8s/scanner-scaledobject.yaml`), TriggerAuthentication `scanner-sqs-trigger-auth`
- Trigger: `aws-sqs-queue` on `ApproximateNumberOfMessages`, `scaleOnInFlight: true`
- Queue length target: `SCANNER_QUEUE_LENGTH` (default 50 messages per pod)
- Range: `ScannerMinReplicas` (1) to `ScannerMaxReplicas` (10)
- Chart HPA DISABLED: `scanner.autoscaling.enabled=false` in `helm/values-base.yaml`; polling 5s, cooldown 300s
- **BYO mode**: set `ExternalScanQueueArn` to scale the scanner on your own SQS queue (KEDA is installed and the ScaledObject points at it; cross-account queues need a matching resource policy)
- Fallback: in BYO with NO queue, `bootstrap.sh` re-enables the chart CPU/mem HPA so the scanner still autoscales
- Review release `rv`: not queue-scaled on this branch (chart HPA off via values-base) — pin its replicas if you enable the review pipeline

### KEDA (scanner-app pods only)
- ScaledObject: `scanner-app-sqs-scaler`
- Trigger: `aws-sqs-queue` on `ApproximateNumberOfMessages`
- Queue length target: 5 messages per pod
- Scale on in-flight: true
- Polling interval: 5s; cooldown: 300s
- Range: 1 to `ScannerAppMaxReplicas` (default 20; substituted into the `<MAX_REPLICAS>` placeholder by `deploy.sh`)
- Auth: `provider: aws`, `identityOwner: keda` (KEDA operator's Pod Identity)
- KEDA is installed only when `DeployScannerApp=true`

### Cluster Autoscaler (node scaling)
- Helm chart `autoscaler/cluster-autoscaler` in `kube-system`, IAM via Pod Identity (`ClusterAutoscalerRole`)
- ASG auto-discovery via the `k8s.io/cluster-autoscaler/*` tags EKS applies to managed node group ASGs automatically
- `expander=least-waste`, `balance-similar-node-groups=true`, `scale-down-unneeded-time=2m`
- Single managed node group: `NodeInstanceType` (default r8g.xlarge, Graviton ARM; x86 r7i/r7a/r6i optional), min 2 / desired 2 / max 8
- **PodDisruptionBudgets are retained** (`k8s/pdb.yaml`) — they now protect scanner-app (maxUnavailable 25%) and the V1FS scanner (minAvailable 1) from Cluster Autoscaler node drains during scale-down

## Scaling Limits

| Component | Min | Max | Mechanism | Config |
|---|---|---|---|---|
| V1FS scanner pods | 1 | 10 | **KEDA (queue depth, 50 msgs/pod)** | `ScannerMinReplicas`/`ScannerMaxReplicas` CFN params |
| Scanner-app pods | 1 | 20 | KEDA (5 msgs/pod) | `ScannerAppMaxReplicas` CFN param → `<MAX_REPLICAS>` in `k8s/scaledobject.yaml` |
| Review scanner-app pods | 1 | 5 | KEDA (50 msgs/pod) | `k8s/review-scaledobject.yaml` |
| Review V1FS scanner pods (`rv`) | 1 | 3 | not queue-scaled on this branch (chart HPA off) | `--set` in `scripts/bootstrap.sh` |
| Managed node group | 2 | 8 | Cluster Autoscaler | `NodeGroupMinSize`/`MaxSize` CFN params |

## Expected Scale-Up Latency

- **Chart HPA scale-up: 1–3 minutes is normal and expected.** The HPA reacts to Metrics Server samples (15–30s), then new scanner pods must schedule, pull, start, and register with TrendAI cloud. This is slower than the old KEDA queue-depth scaling but is the supported behavior — do not "fix" it by re-adding ScaledObjects for the chart scanner
- **Node provisioning adds 1–2 minutes** when the Cluster Autoscaler must grow the ASG before pods can schedule
- **This deployment is evaluation-sized by design** — steady, supported scaling rather than peak burst throughput

## Scanner-App Settings
- `MAX_CONCURRENT_SCANS`: 50 per pod (at default max scale: 20 pods × 50 = 1,000 scan slots)
- Main scanner-app resources: 500m/512Mi requests, 1000m/1024Mi limits
- Review scanner-app resources: 500m/2Gi requests, 1000m/4Gi limits (higher memory for oversize files)
- Health probes: liveness `/healthz`, readiness `/readyz` on port 8080
- Scan audit trail: structured JSON to CloudWatch Logs (`scan-audit-${StackName}`), batched writes

## V1FS Scanner Settings
- Pod resources: 800m CPU / 2Gi memory — these are now the chart defaults, so no override is set in `values-base.yaml`
- Fits 4 scanner pods per xlarge node (4 vCPU / 32 GiB)
- No Prometheus /metrics endpoint — custom I/O metrics not available
- Scan cache: caches results by file hash. Same files on the same stack produce artificially fast results

## Performance Characteristics
- **The V1FS scanner is I/O and memory bound, not CPU bound.** The engine loads signature databases into memory and spends most time on network I/O (gRPC) and disk operations; CPU stays low even under heavy load. This is exactly why the chart HPA's **memory target** matters — a CPU-only target would rarely trigger. With CPU 80% / memory 80% dual targets, memory pressure drives most scale-ups
- **Real malware scan latency** (fresh stack, no cache): p50 = 7.2s, avg = 16.1s, p90 = 43.4s, p95 = 59.5s, p99 = 92.4s, max = 131.6s
- **Cached scan latency**: p50 = 28ms (cache lookup, not real analysis)
- **Rewriting scanner-app in Go would not improve throughput** — the bottleneck is the V1FS scan engine and network round-trips, not the Python runtime
- **gRPC scan timeout**: the SDK reads `TM_AM_SCAN_TIMEOUT_SECS` (set to 600s via the `ScanTimeoutSeconds` CFN parameter, which also sets the SQS visibility timeout)

## Review Pipeline Scaling

The review pipeline handles low-volume deep analysis of files that exceeded the main scanner's decompression limits (nesting depth, file count, compression ratio, or total decompressed size). It is optional and OFF by default (`DeployReviewPipeline=false`). When deployed, it re-scans these files using the `rv` V1FS release with no decompression limits. When NOT deployed, the main scanner quarantines decompression-limit files with explanatory tags instead (they are never routed to clean — they were not fully inspected).

- **KEDA ScaledObject**: `review-scanner-app-sqs-scaler` — review-scanner-app pods, min 1 / max 5, threshold 50 messages, polling 5s, cooldown 300s
- **`rv` scanner pods**: min 1 / max 3, not queue-scaled on this branch (chart HPA off) — pin replicas if you enable review
- **Always-warm** — one pod of each always running to avoid cold-start gRPC connection failures when files arrive for review
- **No PDB** — the review pipeline is low-volume

## Observability
- **CloudWatch Dashboard**: `scanner-${StackName}` (queue health, throughput, latency, detection stats, pod distribution, recent scans, review pipeline metrics). CFN-managed, conditional on `DeployScannerApp=true`
- **CloudWatch Alarms**: DLQ messages (any > 0), Queue Age (> 20 min for 5 consecutive minutes), Review DLQ messages when review is enabled, via SNS topic
- **DLQ Remediation Lambda**: auto re-queues with backoff (60s/300s/900s), max 3 DLQ retries before permanent discard
- **Review DLQ Remediation Lambda**: same retry logic, review pipeline only
