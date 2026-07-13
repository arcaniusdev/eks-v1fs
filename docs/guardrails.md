# Guardrails and Lessons Learned

## Workflow Rules

- **Do not commit and push changes until they have been tested and verified.** Deploy and validate in a live stack before committing. Exception: changes to files the bastion clones from git (k8s manifests, app code, `scripts/bootstrap.sh`, `helm/` values files) must be pushed before they can be tested via stack deployment.
- **Always disable rollback when creating stacks.** Use `--disable-rollback` so you can inspect bastion `/var/log/cloud-init-output.log` when UserData fails.
- **Always use a unique stack name for each deployment.** Reusing names conflicts with retained resources (S3 buckets, log groups, Secrets Manager secrets in pending deletion). Use incrementing suffixes (e.g., `eks-v1fs-09`, `eks-v1fs-10`).
- **CloudFormation template exceeds 51,200-byte inline limit.** Deploy using `--template-url` with an S3-hosted copy instead of `--template-body file://`.
- **No in-place migration across major architecture changes.** Delete and redeploy rather than stack-updating an older deployment.

## Do NOT Do These Things

### Credentials and Identity
- **Do not hardcode AWS credentials.** Pod Identity injects temporary credentials automatically. Never set `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`.
- **Do not use IRSA annotations.** This cluster uses EKS Pod Identity. Do not add `eks.amazonaws.com/role-arn` to ServiceAccounts.
- **Do not use `aws-eks` as KEDA TriggerAuthentication podIdentity provider.** Use `provider: aws` with `identityOwner: keda`. The `aws-eks` provider is for IRSA and causes `awsAccessKeyID not found` errors.

### Autoscaling (post-realignment)
- **Do not add KEDA ScaledObjects targeting the chart-owned V1FS scanner.** The chart's own HPA (`scanner.autoscaling.enabled=true`) is the TrendAI-supported scaling mechanism and the whole point of the realignment. A ScaledObject on the same deployment conflicts with the chart HPA. `upgrade.py` warns if it finds one — delete it. KEDA scales ONLY our scanner-app deployments.
- **Do not disable `scanner.autoscaling.enabled`.** It is set in `helm/values-base.yaml` and must stay enabled. The old guidance (disable chart HPA, scale with KEDA) is INVERTED and obsolete.
- **Do not "fix" HPA scale-up latency.** 1–3 minutes from load spike to new scanner pods is expected chart-HPA behavior (Metrics Server sampling + pod start + cloud registration), plus 1–2 minutes if the Cluster Autoscaler must add a node. Do not re-introduce queue-driven scaling for the chart scanner.
- **Node scaling is the Cluster Autoscaler on a single managed node group.** Do not introduce a separate node provisioner or per-workload node pools/affinity — one node group hosts everything.

### Infrastructure
- **Do not create S3 buckets, SQS queues, ECR repos, or IAM roles from application code.** All provisioned by CloudFormation.
- **Do not modify `visionone-filesecurity` namespace secrets.** `token-secret` and `device-token-secret` are managed by bastion provisioning.
- **Do not set explicit S3 bucket names or ECR repository names in CloudFormation.** Let CloudFormation auto-generate names. ECR names must be lowercase — removing the explicit `RepositoryName` property avoids uppercase stack name conflicts entirely.
- **All stack-created S3 buckets survive stack deletion** (`DeletionPolicy: Retain`). Delete manually after stack teardown. Versioned buckets (ingest, quarantine) require deleting all object versions and delete markers before the bucket can be removed.
- **Respect the parameter Rules.** `DeployReviewPipeline=true` requires `DeployScannerApp=true`; `ScannerEndpointMode=alb` requires both `ACMCertificateArn` and `ScannerDomain`. CloudFormation rejects violations at create time.

### Existing-User-Bucket Mode (`ExistingIngestBucket`)
- **NEVER write `QueueConfigurations` (or any notification config) directly onto a user's bucket.** `put-bucket-notification-configuration` is a full-replace API — a direct write destroys the user's existing notification wiring. The ONLY supported mechanism is the `EnableEventBridgeFunction` custom-resource Lambda, which reads the current config and MERGES `EventBridgeConfiguration` alongside it. Its Delete handler is intentionally a no-op — never mutate the user's bucket on teardown.
- **Never delete user bucket objects.** In existing-bucket mode `DELETE_SOURCE_ENABLED=false`: the scanner tags source objects with the verdict via `put_object_tagging`. The IAM policy enforces this (`s3:PutObjectTagging` instead of `s3:DeleteObject`). Do not grant delete permission or flip the flag.
- **Reconciliation must stay off in existing-bucket mode.** Objects legitimately remain in the user's bucket, so age-based re-queueing would endlessly re-scan everything.

### V1FS Helm and SDK
- **Do not run `helm upgrade` without the values files.** Always upgrade via `scripts/upgrade.py`, which layers `-f helm/values-base.yaml` + `-f <captured helm get values>` + live HPA min/max. A plain `helm upgrade` reverts to chart defaults — losing dbEnabled, storage classes, ephemeral volume, and re-enabling the chart's default-on ingresses.
- **The chart is pinned to 1.4.10** (`V1FS_CHART_VERSION` in `bootstrap.sh`). Use `upgrade.py --version X.Y.Z` to move deliberately.
- **Both chart ingresses default to ENABLED in the chart.** `values-base.yaml` explicitly disables `scanner.ingress` and `managementService.ingress`. Never expose the management service; the scanner ingress is re-enabled only by ALB endpoint mode with its own settings.
- **Do not install both V1FS Helm releases in the same namespace.** Each release creates a ServiceAccount named `visionone-filesecurity`. `my-release` runs in `visionone-filesecurity`, `rv` in `visionone-review`. Sharing a namespace causes a ServiceAccount ownership conflict that breaks Helm operations.
- **Do not add `--wait` to V1FS Helm install.** Scanner pods need time to register with TrendAI cloud on first startup.
- **Do not `await` `amaas.grpc.aio.init()`.** It's synchronous despite being in the `aio` module. `quit()` and `scan_buffer()` ARE async.
- **Do not enable PML unless the account supports it.** `pml=True` on unsupported accounts returns gRPC UNIMPLEMENTED.
- **V1FS scanner pods do not expose Prometheus metrics.** No `/metrics` HTTP endpoint on any port. The chart HPA uses Metrics Server (CPU/memory), which is why Metrics Server is a hard dependency.
- **Do not apply CLISH scan policy to `rv`** — it must run with unlimited decompression to properly analyze files that exceeded the main scanner's limits. `upgrade.py` re-applies the captured policy to `my-release` only.

### Scanner-App Routing and Parsing
- **Do not route decompression-limit files to the clean bucket.** They were NOT fully inspected. With the review pipeline they go to the review bucket; without it they go to QUARANTINE tagged `ScanResult=S3-DecompressionLimit` + `ScanErrors=<names>`. (Pre-realignment behavior sent them to clean when review was disabled — that was a bug.)
- **Do not set `REVIEW_ROUTING_ENABLED=true` on the review scanner** — it creates an infinite routing loop back to the review bucket.
- **Do not URL-decode EventBridge object keys.** `_extract_records()` handles two shapes: S3 notification keys are form-encoded and MUST go through `unquote_plus()` (never `unquote` — spaces arrive as `+`); EventBridge `detail.object.key` values are RAW and must NOT be decoded, or keys containing literal `+`/`%` are corrupted.
- **Reconciliation runs in exactly one place.** Review scanner when the review pipeline is deployed; MAIN scanner-app only when review is absent AND the ingest bucket is stack-created (deploy.sh derives this); nowhere in existing-bucket mode. Enabling it in two places causes duplicate scans.

### Networking
- **Node-to-node security group rule must use `IpProtocol: "-1"` (all protocols).** TCP-only breaks cross-AZ DNS (UDP). Symptoms: TCP to CoreDNS port 53 works, but DNS queries time out.
- **The scanner NLB/ALB is internal-only.** The endpoint ingress rules (50051/1344) allow `10.2.0.0/16` only. Do not create internet-facing schemes or widen the CIDR.

### Container and Dependencies
- **Do not use non-numeric USER in Dockerfile with `runAsNonRoot`.** Use `useradd -u 999` and `runAsUser: 999`.
- **Do not use loose version pins (>=).** Pin exact versions (==). Keep `aiobotocore` and `boto3` botocore versions in sync.
- **Do not use `:latest` image tags.** Use immutable git SHA tags with ECR `ImageTagMutability: IMMUTABLE`.

### Debugging V1FS
- **V1FS SDK response and scanner pod logs have different formats.** The SDK returns `foundErrors` with string names (e.g., `ATSE_ZIP_RATIO_ERR`). The V1FS scanner pod logs (fluent-bit) show `atse.error` with numeric codes (e.g., `-71`). Do not look for `atse.error` in the SDK response — it does not exist there.
- **`kubectl cp` fails on scanner-app pods.** The read-only root filesystem blocks writes. To run scripts inside the pod, upload to S3 and use `kubectl exec` with inline Python, or mount a writable path. Do not attempt `kubectl cp` — it will silently fail.

### Testing and Operations
- **`aws s3 sync --quiet` silently swallows errors.** Always verify S3 write access with a single test file before relying on sync results. A sync that completes in seconds for thousands of files likely means it failed silently.
- **Apply ScaledObject templates through the deploy script, not raw kubectl.** `k8s/scaledobject.yaml` has `<SQS_QUEUE_URL>`, `<AWS_REGION>`, and `<MAX_REPLICAS>` placeholders that deploy.sh substitutes. Applying the raw file breaks KEDA with "invalid input region" errors.
- **Delete orphaned CloudWatch log groups between stack deployments.** EKS cluster logs (`/aws/eks/...`) and VPC flow logs (`/vpc/flowlogs/...`) are not always cleaned up by CloudFormation and will cause `AlreadyExists` failures on the next stack.
- **PodDisruptionBudgets are still required.** `k8s/pdb.yaml` protects scanner-app (maxUnavailable 25%) and the V1FS scanner (minAvailable 1) from Cluster Autoscaler node drains during scale-down. Keep them — the autoscaler drains nodes when consolidating.
- **`upgrade.py` sanity scan without scanner-app**: when the scanner-app module is not deployed there is no ingest bucket ConfigMap; the script instead prints the SSM-published scanner endpoint (`/<stack>/scanner-endpoint`) and instructions for a manual SDK scan. This is expected, not a failure.

## Environment and PATH (bastion context)

- Cloud-init runs with minimal env. `bootstrap.sh` exports `HOME=/root` and a full PATH — keep that pattern for any new provisioning steps.
- After `aws eks update-kubeconfig`, always `export KUBECONFIG=/root/.kube/config`.
- Ubuntu `/bin/sh` is `dash`. Use `>/dev/null 2>&1` not `&>`.
- AWS CLI v2: `/usr/local/bin/aws`. Helm: `/usr/bin/helm`. kubectl/eksctl: `/usr/local/bin/`.
- Bastion UserData is intentionally thin (env exports + git clone + `scripts/bootstrap.sh`). Put new provisioning logic in `bootstrap.sh` (which is version-controlled and testable), not in the CloudFormation UserData.

## Networking from Pods

- Private subnet pods reach internet via NAT gateways (outbound only)
- DNS: Pod → CoreDNS → VPC resolver (10.2.0.2) → internet
- CoreDNS and kube-proxy are managed addons — both required for pod DNS
- EKS API endpoint is private-only — kubectl/Helm must run from within the VPC
