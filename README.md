# EKS Vision One File Security Scanner

Automated malware scanning pipeline on AWS. Files uploaded to an S3 bucket are automatically scanned using [Trend Micro Vision One File Security](https://www.trendmicro.com/en_us/business/products/network-security/file-security.html) and routed to a clean bucket or quarantine bucket based on the scan result.

Everything deploys from a single CloudFormation template — the EKS cluster, networking, storage, queues, IAM, and the scanner application itself.

## What is Vision One File Security?

Vision One File Security is Trend Micro's malware scanning service. It uses multiple detection engines — pattern matching, heuristics, and predictive machine learning (PML) — to identify threats in files of any type.

In this project, the scanner runs **inside the Kubernetes cluster** as a set of pods deployed via Helm. The scanner app communicates with these pods over gRPC to scan files, and the scanner pods phone home to the Vision One cloud for threat intelligence updates and to report results.

This means files are scanned locally within your VPC — they are never uploaded to an external service.

## How It Works

```
                         S3 Ingest Bucket
                               |
                    s3:ObjectCreated event
                               |
                               v
                          SQS Queue ---------> Dead Letter Queue
                               |                (after 3 failures)
                               v
                     Scanner App Pod (EKS)
                        |            |
                   Download file   Scan via gRPC
                   from S3         (in-cluster V1FS pods)
                        |
                  +-----+------+
                  |            |
              CLEAN        MALICIOUS
                  |            |
                  v            v
           Clean Bucket   Quarantine Bucket
                  |            |
              Delete from Ingest Bucket
                  |            |
              Delete SQS message
```

1. A file lands in the **Ingest Bucket** (uploaded by a user, application, or pipeline)
2. S3 sends an event notification to an **SQS queue**
3. The **scanner app pod** long-polls the queue, picks up the message, and downloads the file into memory
4. The file is scanned using the **Vision One File Security Python SDK** over gRPC to the in-cluster scanner pods
5. Based on the result:
   - **Clean** (`scanResult == 0`) — file is copied to the Clean Bucket
   - **Malicious** (`scanResult > 0`) — file is copied to the Quarantine Bucket
6. The original file is deleted from the Ingest Bucket and the SQS message is removed

If scanning fails, the message stays in the queue and is retried. After 3 failures it moves to a Dead Letter Queue for investigation.

## Architecture

### Infrastructure (CloudFormation)

The `eks-v1fs.yaml` template creates everything:

| Resource | Purpose |
|---|---|
| **VPC** | `10.2.0.0/16` with public and private subnets across 2 AZs |
| **NAT Gateways** | One per AZ — pods in private subnets reach the internet for threat intelligence updates |
| **EKS Cluster** | Private API endpoint, full audit logging, managed addons (vpc-cni, CoreDNS, kube-proxy, Pod Identity Agent) |
| **Node Group** | `r7i.large` instances (2 vCPU, 16 GiB) in private subnets, min 2 / max 10 — consistent CPU for sustained scanning |
| **ECR Repository** | Hosts the scanner app container image, scan-on-push enabled |
| **S3 Buckets** | Ingest (with event notifications), Clean, Quarantine (survives stack deletion) |
| **SQS Queues** | Main queue (300s visibility timeout, 20s long polling) + Dead Letter Queue |
| **IAM Roles** | Least-privilege roles for nodes, bastion, scanner app, KEDA operator, and Cluster Autoscaler |
| **Pod Identity** | Binds IAM roles to Kubernetes service accounts — no access keys needed |
| **Secrets Manager** | Stores the V1FS registration token and API key |
| **KEDA** | Scales scanner app pods based on SQS queue depth |
| **Cluster Autoscaler** | Adds/removes EKS nodes when pods can't be scheduled or nodes are underutilized |
| **Bastion Host** | Provisions the cluster, installs Helm charts, builds and deploys the scanner app |

### Scanner Application

A Python asyncio application optimized for high throughput:

- **Async I/O throughout** — `aiobotocore` for S3/SQS, `amaas.grpc.aio` for scanning
- **Concurrent processing** — up to 20 files scanned simultaneously (configurable via `MAX_CONCURRENT_SCANS`)
- **In-memory scanning** — files are downloaded as bytes and scanned with `scan_buffer()`, never written to disk
- **Visibility heartbeat** — extends SQS message visibility during long scans to prevent duplicate processing
- **Graceful shutdown** — handles SIGTERM to drain in-flight scans before exiting (5-minute grace period)
- **Predictive Machine Learning** — PML can be enabled for advanced threat detection (requires account-level PML support)

### Autoscaling

The system scales automatically at two levels to handle sudden bursts of thousands of files.

**Pod scaling (KEDA)** — [KEDA](https://keda.sh/) monitors the SQS queue depth and adjusts the number of scanner app pods accordingly. When files flood in, SQS messages pile up and KEDA responds by adding more pods to drain the queue faster.

- Checks queue depth every 30 seconds
- Scales up at 5 messages per pod — if 50 messages are waiting, KEDA scales to 10 pods
- Includes in-flight messages (being processed but not yet deleted) in the count
- Scales back down after 5 minutes of low queue depth
- Range: 1 to 10 pods (always at least 1 pod running)

Each scanner app pod processes up to 20 files concurrently (via async I/O), so at max scale the system handles 200 concurrent scans.

**Node scaling (Cluster Autoscaler)** — when KEDA creates new pods but there aren't enough nodes to run them, the [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler) detects the unschedulable pods and adds nodes to the EKS node group.

- Watches for pods stuck in `Pending` state due to insufficient CPU/memory
- Adds `r7i.large` nodes (2 vCPU, 16 GiB each) to the node group
- Node group range: 2 to 10 nodes
- Scales down underutilized nodes after 10 minutes of low usage (threshold: 65% utilization)

**How they work together:**

```
Thousands of files arrive in S3
         |
         v
SQS queue depth spikes to 1000+
         |
         v
KEDA sees queue depth, scales scanner-app from 1 to 10 pods
         |
         v
Kubernetes can't schedule all 10 pods on 2 nodes (not enough CPU/memory)
         |
         v
Cluster Autoscaler adds nodes (up to 10) to fit the pending pods
         |
         v
All pods start, each processing 20 files concurrently
         |
         v
Queue drains → KEDA scales pods back down → Autoscaler removes idle nodes
```

Both KEDA and the Cluster Autoscaler get their AWS permissions through Pod Identity — KEDA reads SQS queue metrics, the autoscaler manages the Auto Scaling Group. No access keys are involved.

### How Credentials Work

The scanner app pod gets AWS permissions automatically through [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html). No access keys are configured anywhere.

1. CloudFormation creates an IAM role (`ScannerAppRole`) with permissions scoped to the specific S3 buckets, SQS queue, and Secrets Manager secret
2. A Pod Identity Association binds this role to the `scanner-app` Kubernetes service account
3. The Pod Identity Agent (a DaemonSet on each node) intercepts credential requests from the pod and injects temporary credentials
4. The app retrieves the V1FS API key from Secrets Manager at startup using these credentials

## Prerequisites

You need two credentials from the [Trend Micro Vision One console](https://portal.xdr.trendmicro.com/):

1. **Registration Token** — used by the scanner pods to register with Vision One. Generate this under **Cloud Security > File Security > Containerized Scanner > Get ready to deploy containerized scanner > Get registration token**.
2. **API Key** — used by the scanner application to authenticate scan requests. Generate this under **Administration > API Keys > Add API Key** with the **"Run file scan via SDK"** permission.

## Deployment

### Launch the stack

```bash
aws cloudformation create-stack \
  --stack-name my-scanner \
  --template-body file://eks-v1fs.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=RegistrationToken,ParameterValue='<your-registration-token>' \
    ParameterKey=ApiKey,ParameterValue='<your-api-key>' \
    ParameterKey=PrimaryAZ,ParameterValue=us-east-1a \
    ParameterKey=SecondaryAZ,ParameterValue=us-east-1b
```

That's it. The bastion host UserData automatically:

1. Installs kubectl, Helm, eksctl, Docker, and the AWS CLI
2. Configures kubeconfig and creates the `visionone-filesecurity` namespace
3. Installs Cluster Autoscaler and KEDA via Helm
4. Deploys the Vision One File Security scanner pods via Helm (GPG-verified)
5. Clones this repo, builds the scanner app Docker image, pushes it to ECR
6. Deploys the scanner app (ServiceAccount, ConfigMap, Deployment, KEDA ScaledObject) to the cluster

Stack creation takes approximately 20-30 minutes. Monitor progress:

```bash
aws cloudformation describe-stacks --stack-name my-scanner --query 'Stacks[0].StackStatus'
```

### Verify

Connect to the bastion via Session Manager:

```bash
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`BastionPublicIP`].OutputValue' --output text)

# Or use Session Manager (no SSH key needed):
aws ssm start-session --target <instance-id>
```

Check that everything is running:

```bash
kubectl get pods -n visionone-filesecurity
```

You should see the V1FS scanner pods and the `scanner-app` pod all in `Running` state.

### Test

Upload a file to the ingest bucket:

```bash
INGEST_BUCKET=$(aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`IngestBucketName`].OutputValue' --output text)

echo "Hello, this is a clean test file" > /tmp/testfile.txt
aws s3 cp /tmp/testfile.txt s3://$INGEST_BUCKET/
```

Watch the scanner app logs:

```bash
kubectl logs -f deployment/scanner-app -n visionone-filesecurity
```

You should see the file scanned and routed to the clean bucket.

To test malware detection, upload the [EICAR test file](https://www.eicar.org/download-anti-malware-testfile/):

```bash
echo 'X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' \
  > /tmp/eicar.txt
aws s3 cp /tmp/eicar.txt s3://$INGEST_BUCKET/
```

This should be flagged as malicious and routed to the quarantine bucket.

## Project Structure

```
eks-v1fs.yaml              CloudFormation template (all infrastructure)
app/
  Dockerfile               Ubuntu 22.04 container image
  requirements.txt         Python dependencies (visionone-filesecurity, aiobotocore)
  scanner.py               Async SQS polling, S3 download, V1FS scan, file routing
  config.py                Environment variable loading and validation
k8s/
  serviceaccount.yaml      Kubernetes ServiceAccount (Pod Identity, no annotations)
  configmap.yaml           Environment config template (populated by deploy script)
  deployment.yaml          Pod spec with 300s graceful shutdown period
  scaledobject.yaml        KEDA ScaledObject + TriggerAuthentication for SQS-driven autoscaling
scripts/
  build-and-push.sh        Build Docker image and push to ECR (reads stack outputs)
  deploy.sh                Template ConfigMap from stack outputs and apply k8s manifests
```

## Manual Re-deployment

If you need to update the scanner app after the initial deployment, SSH into the bastion and run:

```bash
export CFN_STACK_NAME=my-scanner
export AWS_REGION=us-east-1
/opt/eks-v1fs/scripts/build-and-push.sh
/opt/eks-v1fs/scripts/deploy.sh
```

Or pull the latest code first:

```bash
cd /opt/eks-v1fs && git pull
```

## Security

- All S3 buckets use AES256 encryption with public access fully blocked
- SQS queues use server-side encryption
- The quarantine bucket has `DeletionPolicy: Retain` — it survives stack deletion for forensic preservation
- ECR repository has scan-on-push enabled for container vulnerability scanning
- IMDSv2 is enforced on all nodes
- EBS volumes are encrypted
- VPC Flow Logs capture all network traffic
- EKS audit logging is enabled for all control plane components
- IAM policies use least-privilege, resource-scoped permissions
- Credentials are stored in Secrets Manager, never in plaintext
- The V1FS Helm chart is GPG-verified before installation

## Cleanup

```bash
aws cloudformation delete-stack --stack-name my-scanner
```

Note: The quarantine bucket is retained after stack deletion to preserve any malicious files for investigation. Delete it manually when no longer needed:

```bash
QUARANTINE_BUCKET=$(aws cloudformation describe-stacks --stack-name my-scanner \
  --query 'Stacks[0].Outputs[?OutputKey==`QuarantineBucketName`].OutputValue' --output text)
aws s3 rb s3://$QUARANTINE_BUCKET --force
```
