# Security

## Container Hardening
- Non-root user (UID 999) — Kubernetes enforces `runAsNonRoot: true`
- Read-only root filesystem (`readOnlyRootFilesystem: true`) — only `/tmp` writable (10Mi emptyDir)
- All Linux capabilities dropped (`capabilities.drop: ALL`), privilege escalation disabled
- Base image: `python:3.11-slim` (~76MB attack surface)
- Dependencies pinned to exact versions to prevent supply chain drift
- `PYTHONDONTWRITEBYTECODE=1` prevents `.pyc` writes (required for read-only fs)

## Image Supply Chain
- ECR `ImageTagMutability: IMMUTABLE` — tags cannot be overwritten after push
- Images tagged with 12-character git SHA, not `:latest`
- ECR scan-on-push enabled for vulnerability scanning

## Network Security
- **NetworkPolicy** restricts scanner-app egress to: DNS (53), V1FS gRPC (50051, same namespace), AWS HTTPS (443, external IPs only)
- Security groups use least-privilege port rules; node-to-node rule uses all protocols (`IpProtocol: "-1"`) for DNS UDP
- EKS API endpoint is private-only

## Data Protection
- All S3 buckets: AES256 encryption, public access blocked
- Ingest and quarantine buckets have versioning enabled
- All S3 buckets: `DeletionPolicy: Retain` for forensic preservation
- Ingest bucket: 7-day lifecycle policy expires unprocessed files
- SQS queues: SSE encryption; queue policy restricts SendMessage to S3 from same account
- All EBS volumes encrypted
- EFS filesystem encrypted at rest and in transit (TLS mount option)

## Secrets Management
- V1FS registration token and API key in AWS Secrets Manager (never plaintext)
- CloudFormation parameters use `NoEcho: true`
- Bastion SSH key auto-generated, stored in SSM Parameter Store
- Scanner app retrieves API key from Secrets Manager at startup via ARN

## Application Resilience
- Configurable file size limit (`MAX_FILE_SIZE_MB`, default 500) prevents OOM — oversized files quarantined via server-side S3 copy
- S3 records within a message processed independently — one failure doesn't block siblings
- SQS polling: jittered exponential backoff (2^n, max 60s)
- SQS visibility heartbeat extends timeout every 240s for long-running scans

## Infrastructure Security
- Session Manager (SSM) available on bastion and worker nodes
- IMDSv2 enforced on workers (`HttpTokens: required`)
- EKS audit logging enabled for all control plane components
- VPC Flow Logs capture all traffic
- IAM policies use scoped resource ARNs — no account-wide wildcards except where AWS requires them
- Review scanner IAM role (`ReviewScannerAppRole`) prevents routing loops by excluding review bucket write permissions — the review scanner can only route files to clean or quarantine
- Review pipeline has its own DLQ with remediation Lambda for independent failure handling
