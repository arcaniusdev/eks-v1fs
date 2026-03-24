# Scanner Application

## Service Account (REQUIRED)

The scanner app Deployment MUST use `serviceAccountName: scanner-app`. The CloudFormation template creates a Pod Identity Association binding `ScannerAppRole` to this service account in `visionone-filesecurity`. Without this, all S3/SQS calls fail with access denied.

EKS Pod Identity is a newer alternative to IRSA (IAM Roles for Service Accounts). With Pod Identity, the credential binding is managed entirely by CloudFormation (via `PodIdentityAssociation` resources) and a DaemonSet agent on each node that intercepts credential requests. Unlike IRSA, Pod Identity does NOT use ServiceAccount annotations. Do not add `eks.amazonaws.com/role-arn` to the ServiceAccount.

## Scanner Endpoint

The main V1FS scanner (`my-release`) runs in the `visionone-filesecurity` namespace alongside scanner-app. The review V1FS scanner (`rv`) runs in the `visionone-review` namespace alongside review-scanner-app. Each scanner-app connects to the V1FS scanner in its own namespace via in-cluster gRPC:

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

## V1FS API Key vs Registration Token

Both stored in AWS Secrets Manager:
- **Registration Token** (`V1FSRegistrationSecret`): used by scanner pods to register with Vision One cloud. Already in `token-secret` K8s secret. NOT needed in scanner app.
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
- `scanResult > 0` = malware; `scanResult == 0` with no `foundErrors` = clean; `scanResult == 0` with `foundErrors` indicating decompression limit violations = review (when `REVIEW_ROUTING_ENABLED=true`) or clean (when `REVIEW_ROUTING_ENABLED=false`)
- `foundErrors` array contains `{name, description}` entries: `ATSE_ZIP_RATIO_ERR`, `ATSE_MAXDECOM_ERR`, `ATSE_ZIP_FILE_COUNT_ERR`, `ATSE_EXTRACT_TOO_BIG_ERR`
- `init()` is synchronous; `quit()` and `scan_buffer()` are async
- `scan_buffer()` positional args: `(channel, bytes_buffer, uid, tags=None, pml=False, ...)`
- SDK response keys: `scanResult`, `fileSHA256`, `fileSHA1`, `foundMalwares`, `foundErrors`, `scanId`, `scannerVersion`, `scanTimestamp`, `fileName`, `schemaVersion`
- PML not currently supported on this account — use `pml=False`

## File Lifecycle

A file passes through at most two scan stages before reaching its final destination:

1. **File arrives in the ingest bucket** (uploaded by user, application, or pipeline)
2. **S3 event notification** triggers an SQS message on the main queue
3. **Scanner-app** (main) picks up the message, downloads the file, scans via gRPC to the main V1FS scanner (`my-release`)
4. **Routing decision** based on scan result:
   - **Malicious** (`scanResult > 0`) — copied to quarantine bucket. Done.
   - **Clean** (`scanResult == 0`, no decompression errors) — copied to clean bucket. Done.
   - **Oversized** (exceeds `MAX_FILE_SIZE_MB`) — server-side copied to review bucket without scanning (no download into pod memory). Continues to step 5.
   - **Decompression limits exceeded** (`scanResult == 0` with `foundErrors`) — copied to review bucket for deep analysis. Continues to step 5.
5. **Review bucket S3 event** triggers an SQS message on the review queue
6. **Review-scanner-app** picks up the message, downloads the file, scans via gRPC to the review V1FS scanner (`rv` — no decompression limits)
7. **Final routing** — clean or quarantine only (never back to review). `REVIEW_ROUTING_ENABLED=false` and IAM policy both enforce this

## Core Application Logic

1. **Startup**: Initialize V1FS SDK handle, create boto3 SQS/S3 clients. Credentials injected by Pod Identity.
2. **Poll Loop**: Long-poll SQS (`WaitTimeSeconds=20`). Jittered exponential backoff on errors (2^n, max 60s).
3. **Per message** (records processed independently):
   - Parse S3 event JSON, extract bucket/key/size (`unquote_plus()` the key)
   - Files exceeding `MAX_FILE_SIZE_MB` (default 500): server-side copy to review bucket with tag `ScanResult=S3-Review-Oversize` (or quarantine if `REVIEW_ROUTING_ENABLED=false`)
   - Download to memory, scan with `scan_buffer()`
   - Malicious (`scanResult > 0`): upload to quarantine (`ScanResult=S3-Malware`), delete from ingest
   - Review (`scanResult == 0` with `foundErrors` indicating decompression limits exceeded, only when `REVIEW_ROUTING_ENABLED=true`): upload to review bucket (`ScanResult=S3-Review`), delete from ingest. When `REVIEW_ROUTING_ENABLED=false` (review scanner), these files are routed to clean instead
   - Clean (`scanResult == 0`, no errors): upload to clean (`ScanResult=S3-Clean`), delete from ingest
   - All records succeed: delete SQS message. Any failure: leave for retry.
4. **Error handling**: Failed scans stay in queue; after 3 failures → DLQ. Heartbeat extends visibility every 240s.
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

| Variable | Description | Source |
|---|---|---|
| `SQS_QUEUE_URL` | SQS queue URL | `FileScanQueueUrl` |
| `S3_INGEST_BUCKET` | Source bucket | `IngestBucketName` |
| `S3_QUARANTINE_BUCKET` | Quarantine bucket | `QuarantineBucketName` |
| `S3_CLEAN_BUCKET` | Clean bucket | `CleanBucketName` |
| `S3_REVIEW_BUCKET` | Review bucket (decompression limits exceeded) | `ReviewBucketName` |
| `V1FS_SERVER_ADDR` | Scanner gRPC endpoint | `my-release-visionone-filesecurity-scanner:50051` |
| `V1FS_API_KEY_SECRET_ARN` | Secrets Manager ARN | `ApiKeySecretArn` |
| `AWS_REGION` | AWS region | `us-east-1` |
| `MAX_CONCURRENT_SCANS` | Concurrent scans per pod | `50` |
| `MAX_FILE_SIZE_MB` | Max file size before routing to review bucket (0 = unlimited) | `500` (main), `0` (review) |
| `PML_ENABLED` | Predictive ML scanning | `false` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `AUDIT_LOG_GROUP` | CloudWatch log group for scan audit trail | `ScanAuditLogGroupName` |
| `REVIEW_ROUTING_ENABLED` | Enable routing to review bucket for decompression limit errors | `true` (set to `false` for review scanner) |
| `RECONCILIATION_ENABLED` | Enable background loop to detect and re-queue orphaned ingest files | `false` (set to `true` for review scanner) |
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

## Deployment Workflow

Automated by bastion UserData during stack creation:
1. Clones repo from `https://github.com/arcaniusdev/eks-v1fs.git` to `/opt/eks-v1fs`
2. `build-and-push.sh` — builds image, pushes to ECR with git SHA tag
3. `deploy.sh` — applies ServiceAccount, ConfigMap, NetworkPolicy, Deployment, PDB, KEDA ScaledObject
4. `deploy.sh --review` — applies review-specific manifests (review-serviceaccount, review-configmap, review-networkpolicy, review-deployment, review-scaledobject) in the `visionone-review` namespace

Manual re-deployment from bastion:
```bash
export CFN_STACK_NAME=<stack-name>
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
/opt/eks-v1fs/scripts/deploy.sh --review  # if review pipeline changes were made
```

## Review Scanner Deployment

The review scanner uses the **same Docker image** as the main scanner — behavior is controlled entirely by environment variables. Key differences in the review scanner configuration:

- **`SQS_QUEUE_URL`**: points to the review SQS queue (`ReviewScanQueueUrl`)
- **`S3_INGEST_BUCKET`**: points to the review bucket (reads files from review, not ingest)
- **`V1FS_SERVER_ADDR`**: points to `rv-visionone-filesecurity-scanner:50051` (the review V1FS scanner release with no decompression limits)
- **`REVIEW_ROUTING_ENABLED=false`**: prevents routing files back to the review bucket, which would create an infinite loop. Files are routed only to clean or quarantine
- **`AUDIT_LOG_GROUP`**: points to `review-audit-${StackName}` for separate audit trail

The review scanner is deployed alongside the main scanner using `deploy.sh --review`, which applies the review-specific k8s manifests (`review-serviceaccount.yaml`, `review-deployment.yaml`, `review-networkpolicy.yaml`, `review-scaledobject.yaml`). It uses a separate service account (`review-scanner-app`) bound to `ReviewScannerAppRole` via Pod Identity.

## Reconciliation (Orphaned File Detection)

The review scanner runs an optional background reconciliation loop that detects files in the ingest bucket that were never processed — typically due to transient failures such as IAM propagation delays, scanner pod restarts, or SQS message expiration.

How it works:
1. Every `RECONCILIATION_INTERVAL` seconds (default 300), lists all objects in `RECONCILIATION_BUCKET`
2. For each object with `LastModified` older than `RECONCILIATION_AGE_THRESHOLD` seconds (default 1800), sends a synthetic S3 event notification to `RECONCILIATION_QUEUE_URL`
3. The main scanner picks up the synthetic message and processes the file normally

The reconciliation loop is enabled only on the review scanner (`RECONCILIATION_ENABLED=true` in the review ConfigMap). The `ReviewScannerAppRole` includes `s3:ListBucket` on the ingest bucket and `sqs:SendMessage` on the main FileScanQueue for this purpose.
