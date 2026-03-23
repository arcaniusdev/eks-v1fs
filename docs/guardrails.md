# Guardrails and Lessons Learned

## Workflow Rules

- **Do not commit and push changes until they have been tested and verified.** Deploy and validate in a live stack before committing. Exception: changes to files the bastion clones from git (k8s manifests, app code) must be pushed before they can be tested via stack deployment.
- **Always disable rollback when creating stacks.** Use `--disable-rollback` so you can inspect bastion `/var/log/cloud-init-output.log` when UserData fails.
- **Always use a unique stack name for each deployment.** Reusing names conflicts with retained resources (S3 buckets, log groups, Secrets Manager secrets in pending deletion). Use incrementing suffixes (e.g., `eks-v1fs-09`, `eks-v1fs-10`).
- **CloudFormation template exceeds 51,200-byte inline limit.** Deploy using `--template-url` with an S3-hosted copy instead of `--template-body file://`.

## Do NOT Do These Things

### Credentials and Identity
- **Do not hardcode AWS credentials.** Pod Identity injects temporary credentials automatically. Never set `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`.
- **Do not use IRSA annotations.** This cluster uses EKS Pod Identity. Do not add `eks.amazonaws.com/role-arn` to ServiceAccounts.
- **Do not use `aws-eks` as KEDA TriggerAuthentication podIdentity provider.** Use `provider: aws` with `identityOwner: keda`. The `aws-eks` provider is for IRSA and causes `awsAccessKeyID not found` errors.

### Infrastructure
- **Do not create S3 buckets, SQS queues, ECR repos, or IAM roles from application code.** All provisioned by CloudFormation.
- **Do not modify `visionone-filesecurity` namespace secrets.** `token-secret` and `device-token-secret` are managed by bastion provisioning.
- **Do not set explicit S3 bucket names or ECR repository names in CloudFormation.** Let CloudFormation auto-generate names. ECR names must be lowercase — removing the explicit `RepositoryName` property avoids uppercase stack name conflicts entirely.
- **All S3 buckets survive stack deletion** (`DeletionPolicy: Retain`). Delete manually after stack teardown (ingest, clean, review, quarantine). Versioned buckets (ingest, quarantine) require deleting all object versions and delete markers before the bucket can be removed.

### V1FS Helm and SDK
- **Do not add `--wait` to V1FS Helm install.** Scanner pods need time to register with Vision One cloud on first startup.
- **Do not enable Helm chart HPA when using KEDA.** Set `scanner.autoscaling.enabled=false` in the Helm install. Running both HPA and KEDA on the same deployment causes scaling conflicts.
- **Do not `await` `amaas.grpc.aio.init()`.** It's synchronous despite being in the `aio` module. `quit()` and `scan_buffer()` ARE async.
- **Do not enable PML unless the account supports it.** `pml=True` on unsupported accounts returns gRPC UNIMPLEMENTED.
- **V1FS scanner pods do not expose Prometheus metrics.** No `/metrics` HTTP endpoint on any port. Tested ports 9090, 9091, 8080, 8081, 2112 — all connection refused. Custom I/O-based HPA metrics require a sidecar proxy (Envoy/Istio), which is not worth the complexity.
- **Do not apply CLISH scan policy to `rv`** — it must run with unlimited decompression to properly analyze files that exceeded the main scanner's limits.
- **Do not set `REVIEW_ROUTING_ENABLED=true` on the review scanner** — it will create an infinite routing loop where files are perpetually routed back to the review bucket and re-scanned.

### Networking
- **Node-to-node security group rule must use `IpProtocol: "-1"` (all protocols).** TCP-only breaks cross-AZ DNS (UDP). Symptoms: TCP to CoreDNS port 53 works, but DNS queries time out.

### Container and Dependencies
- **Do not use non-numeric USER in Dockerfile with `runAsNonRoot`.** Use `useradd -u 999` and `runAsUser: 999`.
- **Do not use loose version pins (>=).** Pin exact versions (==). Keep `aiobotocore` and `boto3` botocore versions in sync.
- **Do not use `:latest` image tags.** Use immutable git SHA tags with ECR `ImageTagMutability: IMMUTABLE`.

### Debugging V1FS
- **V1FS SDK response and scanner pod logs have different formats.** The SDK returns `foundErrors` with string names (e.g., `ATSE_ZIP_RATIO_ERR`). The V1FS scanner pod logs (fluent-bit) show `atse.error` with numeric codes (e.g., `-71`). Do not look for `atse.error` in the SDK response — it does not exist there.
- **`kubectl cp` fails on scanner-app pods.** The read-only root filesystem blocks writes. To run scripts inside the pod, upload to S3 and use `kubectl exec` with inline Python, or mount a writable path. Do not attempt `kubectl cp` — it will silently fail.

### Testing and Operations
- **`aws s3 sync --quiet` silently swallows errors.** Always verify S3 write access with a single test file before relying on sync results. A sync that completes in seconds for thousands of files likely means it failed silently.
- **Apply ScaledObject templates through the deploy script, not raw kubectl.** The template file has `<SQS_QUEUE_URL>` and `<AWS_REGION>` placeholders that must be substituted. Applying the raw file breaks KEDA with "invalid input region" errors.
- **Delete orphaned CloudWatch log groups between stack deployments.** EKS cluster logs (`/aws/eks/...`) and VPC flow logs (`/vpc/flowlogs/...`) are not always cleaned up by CloudFormation and will cause `AlreadyExists` failures on the next stack.

## Environment and PATH (bastion context)

- Cloud-init runs with minimal env. Always `export HOME=/root` before kubectl/Helm/GPG.
- After `aws eks update-kubeconfig`, always `export KUBECONFIG=/root/.kube/config`.
- Ubuntu `/bin/sh` is `dash`. Use `>/dev/null 2>&1` not `&>`.
- AWS CLI v2: `/usr/local/bin/aws`. Helm: `/usr/bin/helm`. kubectl/eksctl: `/usr/local/bin/`.

## Networking from Pods

- Private subnet pods reach internet via NAT gateways (outbound only)
- DNS: Pod → CoreDNS → VPC resolver (10.2.0.2) → internet
- CoreDNS and kube-proxy are managed addons — both required for pod DNS
- EKS API endpoint is private-only — kubectl/Helm must run from within the VPC
