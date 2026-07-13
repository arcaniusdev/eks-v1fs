# Scanner Application

The scanner-app module is OPTIONAL (`DeployScannerApp` CloudFormation parameter, default `true`). When disabled, the stack deploys only the V1FS scanner and publishes its gRPC endpoint to SSM (`/<stack>/scanner-endpoint`) for external scanning applications. Everything below applies when the module is deployed.

## Service Account (REQUIRED)

The scanner app Deployment MUST use `serviceAccountName: scanner-app`. The CloudFormation template creates a Pod Identity Association binding `ScannerAppRole` to this service account in `visionone-filesecurity`. Without this, all S3/SQS calls fail with access denied.

EKS Pod Identity is a newer alternative to IRSA (IAM Roles for Service Accounts). With Pod Identity, the credential binding is managed entirely by CloudFormation (via `PodIdentityAssociation` resources) and a DaemonSet agent on each node that intercepts credential requests. Unlike IRSA, Pod Identity does NOT use ServiceAccount annotations. Do not add `eks.amazonaws.com/role-arn` to the ServiceAccount.

## Scanner Endpoint

The main V1FS scanner (`my-release`) runs in the `visionone-filesecurity` namespace alongside scanner-app. The review V1FS scanner (`rv`, review mode only) runs in the `visionone-review` namespace alongside review-scanner-app. Each scanner-app connects to the V1FS scanner in its own namespace via in-cluster gRPC:

```
Main:   my-release-visionone-filesecurity-scanner:50051  (visionone-filesecurity)
Review: rv-visionone-filesecurity-scanner:50051           (visionone-review)
```

Use `amaas.grpc.aio.init()` with the appropriate address (not `init_by_region()`). TLS disabled for in-cluster:

```python
# init() is SYNCHRONOUS — do NOT await it
handle = amaas.grpc.aio.init(
    "my-release-visionone-filesecurity-scanner:50051",
    api_key,
    False  # TLS disabled for in-cluster
)

# quit() and scan_buffer() ARE async — must be awaited
await amaas.grpc.aio.quit(handle)
```

External applications (outside the cluster) use the published endpoint instead, depending on `ScannerEndpointMode`:

```python
# nlb mode (default): plaintext gRPC over the internal NLB, VPC-reachable only
handle = amaas.grpc.init("<nlb-hostname>:50051", api_key, False)

# alb mode: TLS via the ALB ingress (ACM certificate)
handle = amaas.grpc.init("<scanner-domain>:443", api_key, True)
```

The endpoint address is in SSM Parameter Store: `aws ssm get-parameter --name /<stack>/scanner-endpoint`.

## V1FS API Key vs Registration Token

Both stored in AWS Secrets Manager:
- **Registration Token** (`V1FSRegistrationSecret`): used by scanner pods to register with TrendAI cloud. Already in `token-secret` K8s secret. NOT needed in scanner app.
- **V1FS API Key** (`V1FSApiKeySecret`): used by scanner app to authenticate scan requests. Retrieved at startup via Secrets Manager ARN from `ApiKeySecretArn` output.

## V1FS Python SDK

Package: `visionone-filesecurity` on PyPI. Dependencies: `grpcio`, `protobuf`. Python 3.9-3.13.

```python
import amaas.grpc.aio
import boto3, json, os

sm = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
api_key = sm.get_secret_value(SecretId=os.environ["V1FS_API_KEY_SECRET_ARN"])["SecretString"]

handle = amaas.grpc.aio.init(os.environ.get("V1FS_SERVER_ADDR", "my-release-visionone-filesecurity-scanner:50051"), api_key, False)

result_json = await amaas.grpc.aio.scan_buffer(handle, file_bytes, "filename.exe", pml=False, tags=["S3-Scan"])
result = json.loads(result_json)
is_malicious = result.get("scanResult", 0) > 0

await amaas.grpc.aio.quit(handle)
```

Key details:
- `scanResult > 0` = malware; `scanResult == 0` with no `foundErrors` = clean; `scanResult == 0` with `foundErrors` indicating decompression limit violations = NOT fully inspected — routed to review (review enabled) or quarantine with explanatory tags (review disabled)
- `foundErrors` array contains `{name, description}` entries: `ATSE_ZIP_RATIO_ERR`, `ATSE_MAXDECOM_ERR`, `ATSE_ZIP_FILE_COUNT_ERR`, `ATSE_EXTRACT_TOO_BIG_ERR`
- `init()` is synchronous; `quit()` and `scan_buffer()` are async
- `scan_buffer()` positional args: `(channel, bytes_buffer, uid, tags=None, pml=False, ...)`
- SDK response keys: `scanResult`, `fileSHA256`, `fileSHA1`, `foundMalwares`, `foundErrors`, `scanId`, `scannerVersion`, `scanTimestamp`, `fileName`, `schemaVersion`
- PML not currently supported on this account — use `pml=False`

## Event Parsing (Two Message Shapes)

`_extract_records()` in `scanner.py` normalizes both message shapes that can arrive on the queue into `[{bucket, key, size}]` records:

| Shape | Source | Location of bucket/key | Key encoding |
|---|---|---|---|
| S3 event notification | Stack-created ingest bucket → SQS directly | `Records[].s3.bucket.name` / `.object.key` | Form-encoded (spaces as `+`) — MUST use `urllib.parse.unquote_plus()` |
| EventBridge "Object Created" | Existing customer bucket → EventBridge rule → SQS | `detail.bucket.name` / `detail.object.key` | RAW — NO URL decoding. Decoding would corrupt keys containing literal `+` or `%` |

Messages with no processable records (e.g., `s3:TestEvent`, non-Object-Created EventBridge events) are deleted without processing.

## Routing Decision

| Scan outcome | Review pipeline ON | Review pipeline OFF |
|---|---|---|
| Malicious (`scanResult > 0`) | Quarantine (`ScanResult=S3-Malware`) | Quarantine (`ScanResult=S3-Malware`) |
| Decompression limits exceeded (`scanResult == 0` + `foundErrors`) | Review bucket (`ScanResult=S3-Review`) | Quarantine with `ScanResult=S3-DecompressionLimit` + `ScanErrors=<comma-separated error names>` |
| Clean (`scanResult == 0`, no errors) | Clean (`ScanResult=S3-Clean`) | Clean (`ScanResult=S3-Clean`) |
| Oversize (> `MAX_FILE_SIZE_MB`, server-side copy, never downloaded) | Review bucket (`ScanResult=S3-Review-Oversize`) | Quarantine (`ScanResult=S3-Oversize`) |

**Bug fix in the realignment**: previously, with review routing disabled, decompression-limit files were routed to the CLEAN bucket despite never being fully inspected. They now go to QUARANTINE with explanatory tags so the incomplete scan is visible and the file is contained.

`_upload()` takes a tags dict and applies it as a URL-encoded `Tagging` string on `put_object`.

## Source Object Finalization (Tag vs Delete)

After routing, `_finalize_source()` handles the source object based on `DELETE_SOURCE_ENABLED`:

- **`true` (default — stack-owned ingest bucket)**: delete the source object; the verdict bucket now holds the file
- **`false` (existing-customer-bucket mode)**: NEVER delete the customer's object — tag it with the verdict via `put_object_tagging` so the result is visible in place. The IAM policy enforces this: `ScannerAppRole` has `s3:PutObjectTagging` instead of `s3:DeleteObject` on existing buckets

`bootstrap.sh` sets `DELETE_SOURCE_ENABLED=false` automatically when `ExistingIngestBucket` is configured.

## File Lifecycle

1. **File arrives in the ingest bucket** (stack-created or customer-owned)
2. **Notification** → SQS message: direct S3 → SQS (created bucket) or S3 → EventBridge rule → SQS (existing bucket)
3. **Scanner-app** picks up the message, downloads the file, scans via gRPC to the main V1FS scanner (`my-release`)
4. **Routing decision** per the table above; source object deleted or tagged per `DELETE_SOURCE_ENABLED`
5. **(Review mode only)** Review bucket S3 event → review queue → **review-scanner-app** scans via `rv` (no decompression limits) → final routing to clean or quarantine only (never back to review; `REVIEW_ROUTING_ENABLED=false` and IAM both enforce this)

## Core Application Logic

1. **Startup**: Initialize V1FS SDK handle, create boto3 SQS/S3 clients. Credentials injected by Pod Identity.
2. **Poll Loop**: Long-poll SQS (`WaitTimeSeconds=20`). Jittered exponential backoff on errors (2^n, max 60s).
3. **Per message** (records processed independently): normalize via `_extract_records()`, route per the table above, upload with verdict tags, finalize source (delete or tag).
4. **Error handling**: Failed records shorten the message visibility to 30s for fast retry; after 3 receive failures → DLQ. Heartbeat extends visibility during long scans.
5. **Graceful shutdown**: SIGTERM handler drains in-flight scans (5-minute grace period).

## Container Image

```dockerfile
FROM python:3.11-slim
RUN groupadd -g 999 scanner && useradd -r -u 999 -g scanner -d /app -s /sbin/nologin scanner
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chown -R scanner:scanner /app
USER scanner
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "scanner.py"]
```

requirements.txt (pinned):
```
visionone-filesecurity==1.4.1
aiobotocore==3.3.0
boto3==1.42.70
```

Notes: `aiobotocore==3.3.0` requires `botocore>=1.42.62,<1.42.71` — keep `boto3` in sync.

## Configuration (environment variables via ConfigMap)

| Variable | Description | Default / Source |
|---|---|---|
| `SQS_QUEUE_URL` | SQS queue URL | `FileScanQueueUrl` |
| `S3_INGEST_BUCKET` | Source bucket (stack-created or `ExistingIngestBucket`) | `IngestBucketName` |
| `S3_QUARANTINE_BUCKET` | Quarantine bucket | `QuarantineBucketName` |
| `S3_CLEAN_BUCKET` | Clean bucket | `CleanBucketName` |
| `S3_REVIEW_BUCKET` | Review bucket — OPTIONAL; empty when the review pipeline is not deployed. Required only when `REVIEW_ROUTING_ENABLED=true` (config validation enforces this) | `ReviewBucketName` or empty |
| `V1FS_SERVER_ADDR` | Scanner gRPC endpoint | `my-release-visionone-filesecurity-scanner:50051` |
| `V1FS_API_KEY_SECRET_ARN` | Secrets Manager ARN | `ApiKeySecretArn` |
| `AWS_REGION` | AWS region | `us-east-1` |
| `MAX_CONCURRENT_SCANS` | Concurrent scans per pod | `50` |
| `MAX_FILE_SIZE_MB` | Max file size before oversize routing (0 = unlimited) | `500` (main), `0` (review) |
| `PML_ENABLED` | Predictive ML scanning | `false` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `AUDIT_LOG_GROUP` | CloudWatch log group for scan audit trail | `ScanAuditLogGroupName` |
| `REVIEW_ROUTING_ENABLED` | Route decompression-limit files to the review bucket. `deploy.sh` derives it from `S3_REVIEW_BUCKET` presence unless set explicitly | `true` if review bucket exists, else `false`; always `false` on the review scanner |
| `DELETE_SOURCE_ENABLED` | Delete source objects after routing (`true`) or tag them with the verdict instead (`false`, existing-bucket mode) | `true` |
| `SQS_VISIBILITY_TIMEOUT` | Visibility timeout used for heartbeat extension | `600` (matches `ScanTimeoutSeconds`) |
| `TM_AM_SCAN_TIMEOUT_SECS` | V1FS SDK gRPC scan timeout | `600` |
| `RECONCILIATION_ENABLED` | Background loop to detect and re-queue orphaned ingest files | see Reconciliation section |
| `RECONCILIATION_BUCKET` | Bucket to scan for orphaned files (the ingest bucket) | (required if enabled) |
| `RECONCILIATION_QUEUE_URL` | SQS queue to re-queue orphaned files to (the main scan queue) | (required if enabled) |
| `RECONCILIATION_INTERVAL` | Seconds between reconciliation scans (range 60-3600) | `300` |
| `RECONCILIATION_AGE_THRESHOLD` | Only re-queue files older than this many seconds (range 300-86400) | `1800` |

## Deployment Specs

Scanner-app pod resources:
- Requests: 500m CPU, 512Mi memory
- Limits: 1000m CPU, 1024Mi memory
- Security: non-root (UID 999), read-only root fs, all capabilities dropped
- `/tmp` writable via 10Mi emptyDir
- No nodeAffinity — pods schedule onto the single managed node group (Karpenter node targeting is removed)

## Deployment Workflow

Automated during stack creation: bastion UserData exports CFN-derived environment variables, clones `https://github.com/arcaniusdev/eks-v1fs.git` to `/opt/eks-v1fs`, and runs `scripts/bootstrap.sh`, which (when `DEPLOY_SCANNER_APP=true`):
1. Installs Docker, sets `DELETE_SOURCE_ENABLED` (false in existing-bucket mode) and `REVIEW_ROUTING_ENABLED` (true only when review is deployed)
2. `build-and-push.sh` — builds image, pushes to ECR with git SHA tag
3. `deploy.sh` — applies ServiceAccount, NetworkPolicy, generated ConfigMap, Deployment, PDBs, KEDA ScaledObject
4. `deploy.sh --review` (review mode only) — applies review-specific manifests in `visionone-review`

`deploy.sh` details:
- Substitutes `<SQS_QUEUE_URL>`, `<AWS_REGION>`, and `<MAX_REPLICAS>` (from `SCANNER_APP_MAX_REPLICAS`, default 20) into `k8s/scaledobject.yaml` — never apply the raw template
- Derives `REVIEW_ROUTING_ENABLED` from `S3_REVIEW_BUCKET` presence unless explicitly set; `S3_REVIEW_BUCKET` is optional
- Emits a reconciliation block into the MAIN ConfigMap only when review routing is false AND delete-source is true (see Reconciliation)
- Treats a 300s rollout timeout as a warning — on first deploy the Cluster Autoscaler may still be provisioning a node

Manual re-deployment from bastion:
```bash
export CFN_STACK_NAME=<stack-name>
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
/opt/eks-v1fs/scripts/deploy.sh --review  # if the review pipeline is deployed
```

## Review Scanner Deployment (optional, default OFF)

Deployed only when `DeployReviewPipeline=true` (which requires `DeployScannerApp=true`). The review scanner uses the **same Docker image** as the main scanner — behavior is controlled entirely by environment variables:

- **`SQS_QUEUE_URL`**: the review SQS queue (`ReviewScanQueueUrl`)
- **`S3_INGEST_BUCKET`**: the review bucket (reads files from review, not ingest)
- **`V1FS_SERVER_ADDR`**: `rv-visionone-filesecurity-scanner:50051` (the review release, no decompression limits, chart HPA min 1 / max 3)
- **`REVIEW_ROUTING_ENABLED=false`**: prevents routing files back to the review bucket (infinite loop). Files go only to clean or quarantine
- **`AUDIT_LOG_GROUP`**: `review-audit-${StackName}` for a separate audit trail
- **`RECONCILIATION_ENABLED=true`**: the review scanner hosts the reconciliation loop when the pipeline is deployed

It uses a separate service account (`review-scanner-app`) bound to `ReviewScannerAppRole` via Pod Identity, and scales via the `review-scanner-app-sqs-scaler` KEDA ScaledObject (min 1 / max 5).

## Reconciliation (Orphaned File Detection)

A background loop detects files in the ingest bucket that were never processed — typically due to transient failures such as IAM propagation delays, scanner pod restarts, or SQS message expiration.

How it works:
1. Every `RECONCILIATION_INTERVAL` seconds (default 300), lists all objects in `RECONCILIATION_BUCKET`
2. For each object with `LastModified` older than `RECONCILIATION_AGE_THRESHOLD` seconds (default 1800), sends a synthetic S3 event notification to `RECONCILIATION_QUEUE_URL`
3. The main scanner picks up the synthetic message and processes the file normally

**Where it runs** (exactly one place, or nowhere):

| Deployment mode | Reconciliation host |
|---|---|
| Review pipeline deployed | Review scanner-app (`RECONCILIATION_ENABLED=true` in the review ConfigMap) |
| No review pipeline, stack-created ingest bucket | MAIN scanner-app — `deploy.sh` adds the reconciliation block to the main ConfigMap when review routing is false and delete-source is true. `ScannerAppRole` includes `sqs:SendMessage` on the main queue for this |
| Existing-bucket mode (`DELETE_SOURCE_ENABLED=false`) | Forced OFF — customer objects legitimately remain in the bucket, so "old object" does not mean "orphaned" |

The `ReviewScannerAppRole` includes `s3:ListBucket` on the ingest bucket and `sqs:SendMessage` on the main FileScanQueue for the review-hosted case.
