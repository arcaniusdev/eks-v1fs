# Scanner Application

## Service Account (REQUIRED)

The scanner app Deployment MUST use `serviceAccountName: scanner-app`. The CloudFormation template creates a Pod Identity Association binding `ScannerAppRole` to this service account in `visionone-filesecurity`. Without this, all S3/SQS calls fail with access denied.

Pod Identity does NOT use IRSA-style annotations. Do not add `eks.amazonaws.com/role-arn` to the ServiceAccount.

## Scanner Endpoint

The V1FS scanner runs in the same namespace. In-cluster gRPC endpoint:

```
my-release-visionone-filesecurity-scanner:50051
```

Use `amaas.grpc.aio.init()` with this address (not `init_by_region()`). TLS disabled for in-cluster:

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
- `scanResult > 0` = malware; `scanResult == 0` = clean
- `init()` is synchronous; `quit()` and `scan_buffer()` are async
- `scan_buffer()` positional args: `(channel, bytes_buffer, uid, tags=None, pml=False, ...)`
- PML not currently supported on this account — use `pml=False`

## Core Application Logic

1. **Startup**: Initialize V1FS SDK handle, create boto3 SQS/S3 clients. Credentials injected by Pod Identity.
2. **Poll Loop**: Long-poll SQS (`WaitTimeSeconds=20`). Jittered exponential backoff on errors (2^n, max 60s).
3. **Per message** (records processed independently):
   - Parse S3 event JSON, extract bucket/key/size (`unquote_plus()` the key)
   - Files >500MB: server-side copy to quarantine with tag `ScanResult=S3-Oversize`
   - Download to memory, scan with `scan_buffer()`
   - Malicious: upload to quarantine (`ScanResult=S3-Malware`), delete from ingest
   - Clean: upload to clean (`ScanResult=S3-Clean`), delete from ingest
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
| `V1FS_SERVER_ADDR` | Scanner gRPC endpoint | `my-release-visionone-filesecurity-scanner:50051` |
| `V1FS_API_KEY_SECRET_ARN` | Secrets Manager ARN | `ApiKeySecretArn` |
| `AWS_REGION` | AWS region | `us-east-1` |
| `MAX_CONCURRENT_SCANS` | Concurrent scans per pod | `50` |
| `PML_ENABLED` | Predictive ML scanning | `false` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

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
3. `deploy.sh` — applies ServiceAccount, ConfigMap, Deployment, KEDA ScaledObject

Manual re-deployment from bastion:
```bash
export CFN_STACK_NAME=<stack-name>
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
```
