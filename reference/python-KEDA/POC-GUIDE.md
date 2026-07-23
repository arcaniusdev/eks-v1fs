# V1FS on EKS — POC Guide (python-KEDA: queue-depth scaling + Python pull consumer)

> Everything needed to evaluate Vision One File Security on EKS: deploy the stack, scan your first files, connect your own application in Python, and tear it all down cleanly. One API key and about 30 minutes gets you a running scanner.

## Contents

**Part I — Deploy**

1. [What you're evaluating](#1-what-youre-evaluating)
2. [Prerequisites](#2-prerequisites)
3. [Pick your deployment mode](#3-pick-your-deployment-mode)
4. [Deploy the stack](#4-deploy-the-stack)
5. [Your first scans](#5-your-first-scans)
6. [Inside the scanning app](#6-inside-the-scanning-app)
7. [What the bastion does](#7-what-the-bastion-does)

**Part II — Integrate**

8. [Find your endpoint](#8-find-your-endpoint)
    - [8a. Scaling a high-volume client — pull / semaphore dispatcher](#8a-scaling-a-high-volume-client--the-pull--semaphore-dispatcher)
9. [Python: connect and scan](#9-python-connect-and-scan)

**Part III — Operate & Wrap Up**

16. [Tune the scan policy](#16-tune-the-scan-policy)
17. [Scaling expectations](#17-scaling-expectations)
18. [Troubleshooting](#18-troubleshooting)
19. [Teardown](#19-teardown)
20. [Quick reference](#20-quick-reference)
21. [References](#21-references)


---

# Part I — Deploy


## 1. What you're evaluating

> **This is the **python-KEDA** scenario of one unified scanning app: `ScannerAppFlavor=python` + `ScannerDispatchMode=pull` + `ScannerScalingMode=keda`. The V1FS scanner is scaled by KEDA on SQS queue depth, and the Python app runs in **pull** dispatch mode — discovering live scanner pod IPs and spreading scans across the fleet itself (§8a).**

A single CloudFormation template stands up an EKS cluster running the **TrendAI Vision One File Security scanner**, installed from the official Helm chart. The scanning application that drives the S3 pipeline comes in two feature-equivalent language flavors — Python (`app/scanner.py`) and Java (`app-java/`) — selected by `ScannerAppFlavor`; this scenario uses the **Python** flavor (`ScannerAppFlavor=python`) in **pull** dispatch mode (`ScannerDispatchMode=pull`), where the app discovers live scanner pod IPs and dispatches each scan to the least-busy pod directly (§8a). The scanner fleet is scaled by **KEDA on SQS queue depth** (`ScannerScalingMode=keda`) — the number of scanner pods tracks your scan backlog directly, rather than reacting to CPU. (This is a queue-driven variant tuned for an SQS-fed workload: the chart's own CPU/memory HPA is disabled so the two autoscalers don't conflict — see §17, and note the supportability trade-off there.)

```text
[Your files (S3 upload, or your own app)] ──▶ [V1FS Scanner on EKS (official chart · KEDA queue-depth scaling)] ──▶ [Verdicts (clean tagged in place / malicious to quarantine)]
```

Files are scanned **inside your VPC** — the bytes never leave your account. Scan metadata (verdicts, threat names) reports to your Vision One console. Around the scanner core, optional modules — a complete S3 scanning pipeline, a deep-analysis review pipeline, an external endpoint — turn on and off with template parameters.


### The architecture in one picture

Every resource the stack creates, and how a scan flows through them. Node color shows which parameter creates each piece — a default deploy builds the green (core) and amber (scanner-app) items; purple, blue, and red switch on with their modes.

```mermaid
flowchart TB
  classDef core stroke:#1D8102,stroke-width:3px
  classDef app stroke:#906806,stroke-width:3px
  classDef review stroke:#8C4FFF,stroke-width:3px
  classDef endpoint stroke:#0073BB,stroke-width:3px
  classDef existing stroke:#D13212,stroke-width:3px

  subgraph REGION["AWS account — region services"]
    S3A["S3 × 2<br/>ingest · quarantine"]:::app
    S3R["S3 review bucket"]:::review
    SQSA["SQS + DLQ<br/>scan queue pair"]:::app
    SQSR["Review SQS + DLQ"]:::review
    LDLQ["Lambda: DLQ remediation"]:::app
    LRDLQ["Lambda: review DLQ"]:::review
    LEB["EventBridge rule + merge Lambda<br/>wires YOUR bucket"]:::existing
    LCLEAN["Lambda: pre-delete cleanup"]:::core
    ECR["ECR repository<br/>git-SHA image tags"]:::app
    SEC["Secrets Manager<br/>API key (+ optional token)"]:::core
    SSMP["SSM parameter<br/>/&lt;stack&gt;/scanner-endpoint"]:::endpoint
    IAM["IAM roles + Pod Identity<br/>~12 scoped roles"]:::core
    CW["CloudWatch<br/>dashboard · alarms + SNS · audit logs"]:::app
  end

  subgraph VPC["VPC 10.2.0.0/16 — created by the stack, or yours (ExistingVpcId)"]
    NET["IGW · 2× NAT + EIP · 2 public + 2 private subnets · route tables · flow logs"]:::core
    SGS["Security groups<br/>bastion SG: no inbound (SSM-only)"]:::core
    BAST["Bastion t3.medium<br/>runs bootstrap.sh"]:::core
    EFS["EFS + 2 mount targets<br/>scanner scratch space"]:::core
    LB["Internal NLB or ALB<br/>gRPC :50051 / TLS :443"]:::endpoint

    subgraph EKS["EKS cluster — managed node group r8g.xlarge × 2–8, Cluster Autoscaler"]
      V1FS["V1FS scanner<br/>KEDA 1–10 on queue depth"]:::core
      SUP["V1FS support pods<br/>management · database · cache · communicator"]:::core
      RV["V1FS rv release<br/>no decompression limits · HPA 1–3"]:::review
      APP["scanner-app<br/>KEDA 1–20 on queue depth"]:::app
      RAPP["review-scanner-app<br/>KEDA 1–5 always-warm"]:::review
      SYS["System: Cluster Autoscaler · LB controller · metrics-server · Pod Identity agent · EBS/EFS CSI · KEDA operator"]:::core
    end
  end

  S3A -- "S3 events" --> SQSA
  SQSA -- "long-poll" --> APP
  APP -- "gRPC :50051" --> V1FS
  LB --> V1FS
  S3R --> SQSR --> RAPP -- "gRPC" --> RV
  BAST -. "provisions" .-> EKS
```

| Edge color | Created by |
|---|---|
| 🟢 green | always (core) |
| 🟡 amber | `DeployScannerApp` (default on) |
| 🟣 purple | `DeployReviewPipeline` |
| 🔵 blue | `ScannerEndpointMode` |
| 🔴 red | `ExistingIngestBucket` |


## 2. Prerequisites

| You need | Details |
|---|---|
| **One Vision One API key** | Console → *Administration → API Keys → Add API Key*, with the **"Run file scan via SDK"** permission. That's the only credential — the scanner's registration token is fetched automatically at deploy time using this key. |
| **An AWS account** | Admin-level access in one region. The stack idles at roughly 10 vCPU — inside default compute quotas. |
| **Quota headroom** | Each stack uses **2 Elastic IPs and 1 VPC**. Default quotas (5 each) support two concurrent stacks — and a *deleting* stack still holds its quota until deletion completes. Deploy sequentially, or raise the quotas. |
| *ALB endpoint only* | An ACM certificate and a DNS name you control. Skip unless you specifically want TLS termination at an ALB — the default internal NLB needs neither. |
| *Existing VPC only* | By default the stack creates its own network. To deploy into a VPC you already manage, set `ExistingVpcId` + CIDR + two private subnets + a bastion subnet. Your VPC needs DNS enabled, outbound internet from the private subnets (the scanner must reach Vision One), and the `kubernetes.io/role/internal-elb=1` tag on the private subnets. The bastion works from a private subnet — it's SSM-only. |

> [!NOTE]
> **Non-US Vision One tenant?** Set the `VisionOneApiEndpoint` parameter to your regional API host. The default is the US host, `https://api.xdr.trendmicro.com`.


## 3. Pick your deployment mode

Four parameter combinations cover the common evaluation goals. Everything else defaults sensibly.

| Your goal | Parameters | What you get |
|---|---|---|
| **See the full pipeline work** (recommended first run) | *all defaults* | New ingest bucket → SQS → scanning app → clean files tagged in place, malicious moved to quarantine. Drop a file in, watch the verdict land. |
| **I have my own scanning app** | `DeployScannerApp=false` | Just the scanner plus a gRPC endpoint address. No buckets, queues, or pipeline. Connect via the SDK — that's Part II. |
| **Scan a bucket I already own** | `ExistingIngestBucket=` | Your bucket is wired via EventBridge — its existing notification configuration is preserved, and your objects are *tagged* with verdicts, never deleted or moved. |
| **Deep archive analysis** | `DeployReviewPipeline=true` | Adds a second scanner with no decompression limits that re-scans archives too deep/large for the main policy before the final verdict. |

Modes combine (e.g. existing bucket + review pipeline). Invalid combinations are rejected at stack creation by built-in rules.


### App flavor, dispatch, and scaling

Three more parameters select the *shape* of the deployment — which language the scanning app is, how it reaches the scanner, and how the scanner scales. This guide documents one fixed combination; the other combinations have their own guides.

| Parameter | Values | This scenario (python-KEDA) |
|---|---|---|
| `ScannerAppFlavor` | `python` (default) · `java` | **`python`** — the app is `app/scanner.py` |
| `ScannerDispatchMode` | `clusterip` (default) · `pull` | **`pull`** — the app discovers scanner pod IPs from the NLB target group and dispatches to the least-busy pod (§8a) |
| `ScannerScalingMode` | `hpa` (default) · `keda` | **`keda`** — the scanner scales on SQS queue depth (§17 covers the supportability trade-off) |

Both flavors are feature-equivalent and read the same configuration; the flavor and dispatch modes are described in §6. `pull` mode requires an NLB endpoint — `ScannerEndpointMode=auto` (the default) provides one and doubles as the pod-discovery registry. The supported chart-HPA baseline with simple `clusterip` dispatch is the **python-default** guide.


## 4. Deploy the stack

Deploy from the **AWS Console** (click-by-click, below) or the **AWS CLI** (automation — end of this section). Both produce the same stack.

**Before you start:** have your Vision One API key ready (§2) and decide your deployment mode (§3). The default mode — the full scanning pipeline — needs only the API key.

### Step 1 — Get the template

Download `eks-v1fs.yaml` from the [**eks-v1fs repository**](https://github.com/arcaniusdev/eks-v1fs) — ideally from the [latest tagged release](https://github.com/arcaniusdev/eks-v1fs/tags), so the components the bastion pulls at deploy time match the template you launch. That's the only file you need. (Direct link to the current release template: `https://raw.githubusercontent.com/arcaniusdev/eks-v1fs/v1.0.2/eks-v1fs.yaml`.)

### Step 2 — Start the Create stack wizard

1. Open the **CloudFormation console** → **Create stack** → **With new resources (standard)**.
2. Under **Specify template**, choose **Upload a template file** → **Choose file** → select `eks-v1fs.yaml` → **Next**. (The console stages it to S3 for you — no bucket to create.)

> [!NOTE]
> Prefer hosting it yourself? Upload `eks-v1fs.yaml` to any S3 bucket and choose **Amazon S3 URL** instead. This is also what the CLI path at the end of this section requires, since the template is larger than CloudFormation's 51 KB inline limit.

### Step 3 — Name the stack and enter parameters

1. **Stack name:** use a new, unique name for every deploy — `v1fs-eval-1`, then `-2`, `-3`… (reusing a name collides with retained buckets).
2. **ApiKey:** paste your Vision One API key.
3. **RegistrationToken:** leave **blank** — it's fetched automatically from Vision One using your API key.
4. Set the parameters for your chosen mode from §3; leave everything else at its default:
   - **Full pipeline** (default): nothing else to change.
   - **BYO scanning app**: set `DeployScannerApp` = `false`.
   - **Existing bucket**: set `ExistingIngestBucket` to your bucket name.
   - **Review pipeline**: set `DeployReviewPipeline` = `true`.
   - **Non-US tenant**: set `VisionOneApiEndpoint` to your regional API host.
   - **Node type**: `NodeInstanceType` picks the EC2 instance type for the worker nodes. The default is `r8g.xlarge` (Graviton ARM) — ~11–19% lower compute cost than the x86 equivalents. The stack handles ARM automatically: the node group uses an ARM64 AMI and the scanning application image is built for ARM. All scanner components ship multi-architecture images, so functionality is identical. Prefer x86? Choose `r7i.xlarge`, `r7a.xlarge`, `r6i.xlarge`, or `r7i.2xlarge`.
5. Click **Next**.

### Step 4 — Stack options

Nothing required here — scroll to the bottom and click **Next**. (Tags, permissions, and rollback settings can stay at their defaults.)

### Step 5 — Review and submit

1. Review the summary.
2. At the bottom, tick **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"** — the stack creates named IAM roles.
3. Click **Submit**.

### Step 6 — Watch it build (~25–35 minutes)

The stack opens on the **Events** tab; refresh to follow progress. Order of events: the VPC and EKS cluster come up first (~10 min), then the bastion host bootstraps everything — installs the scanner from the official Helm chart (GPG-verified, version-pinned), fetches your registration token from the Vision One API, applies the scan policy, and (in pipeline mode) builds and deploys the scanning application. The bastion signals CloudFormation only when every component is healthy, so **`CREATE_COMPLETE` means the whole system is ready** — there are no extra install steps.

If the stack fails, see [Troubleshooting](#18-troubleshooting) — the bastion logs every step to `/var/log/cloud-init-output.log`.

### Step 7 — Collect the outputs

Open the **Outputs** tab. Depending on mode you'll find the ingest and quarantine bucket names, the CloudWatch dashboard URL, and the scanner endpoint SSM parameter. You'll use these in §5 and Part II.

### CLI alternative

Same result from a terminal:

```bash
# Stage the template (once)
aws s3 mb s3://<your-template-bucket>
aws s3 cp eks-v1fs.yaml s3://<your-template-bucket>/eks-v1fs.yaml

# Create the stack (defaults = full pipeline mode)
aws cloudformation create-stack \
  --stack-name v1fs-eval-1 \
  --template-url https://<your-template-bucket>.s3.amazonaws.com/eks-v1fs.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=ApiKey,ParameterValue="<your-V1-api-key>"

# Watch until CREATE_COMPLETE (~25–35 min)
aws cloudformation describe-stacks --stack-name v1fs-eval-1 \
  --query 'Stacks[0].StackStatus' --output text
```

> [!IMPORTANT]
> **Always pick a new stack name.** Retained buckets keep stack-derived names, so reusing a name collides. Increment: `v1fs-eval-1`, `v1fs-eval-2`, …


## 5. Your first scans

In default mode, scanning is just an S3 upload. Grab the ingest bucket name from stack outputs, then:

```bash
INGEST=$(aws cloudformation describe-stacks --stack-name v1fs-eval-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`IngestBucketName`].OutputValue' --output text)

# A clean file
echo "hello, harmless file" | aws s3 cp - s3://$INGEST/hello.txt

# The EICAR test string — the industry-standard fake "virus" every engine detects.
# (Split into two parts so nothing on YOUR machine flags this command.)
printf 'X5O!P%%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-''ANTIVIRUS-TEST-FILE!$H+H*' \
  | aws s3 cp - s3://$INGEST/eicar.txt
```

Within seconds, each file gets a verdict — clean files stay in the ingest bucket, tagged in place; malicious and unscannable files move to quarantine:

| File | Ends up | Tagged |
|---|---|---|
| `hello.txt` | Ingest bucket (tagged in place) | **S3-Clean** |
| `eicar.txt` | Quarantine bucket | **S3-Malware** |
| A too-deep archive | Quarantine bucket | **S3-DecompressionLimit** plus which limit was hit |

Three other places to see results: object **tags** (on every scanned object), the **CloudWatch dashboard** (`scanner-` — throughput, latency, detections, recent verdicts), and your **Vision One console**, where detections appear with their scan tags.

> [!NOTE]
> **Scanning an existing bucket instead?** Same test — upload to *your* bucket. Your objects always stay put and receive the verdict *tag* in place; clean objects simply stay tagged, and for malicious objects a *copy* also lands in the quarantine bucket. Nothing in your bucket is ever deleted.


## 6. Inside the scanning app

The pipeline you just watched is driven by one small application. It ships in two feature-equivalent language flavors — Python (`app/scanner.py`) and Java (`app-java/`), selected by `ScannerAppFlavor` — and this scenario runs the **Python** flavor: **one Python file** (`app/scanner.py`, ~600 lines), a config loader, and a 22-line Dockerfile with **three pinned dependencies** — the V1FS SDK, an async AWS client, and boto3. Small enough to read in one sitting, which is the point: in an evaluation you should be able to see exactly what touches your files.

The same one app does everything — drains SQS, scans, and routes every verdict (tag clean in place, move malicious to quarantine, send decompression-limited or oversize files to review or quarantine, write the audit trail, reconcile stragglers). How it reaches the scanner pods is a mode, set by `ScannerDispatchMode`. This scenario uses **`pull`**: instead of one connection to the in-cluster Service, the app discovers the live scanner pod IPs from the NLB target group and dispatches each scan to the least-busy pod directly — no load balancer in the scan path. That is the mode that pairs with KEDA queue-depth scanner scaling here; its architecture and rationale are in **§8a**. (The other mode, `clusterip`, is a single reused gRPC connection to the in-cluster Service — the **python-default** scenario.)


### How it works

One async event loop runs the whole show. S3 announces each new object on an SQS queue; the app pulls from that queue and pushes each file through six steps:

```text
[Poll (SQS long-poll)] ▶ [Parse (bucket/key?)] ▶ [Download (into memory)] ▶ [Scan (gRPC → V1FS)] ▶ [Route (tag clean in place / quarantine / review)] ▶ [Finalize (ack + tag + audit)]
```

A semaphore caps how many scans run at once per pod (`MAX_CONCURRENT_SCANS`, default 50). Three small background loops support the main flow: a health server for Kubernetes probes, an audit writer that batches every verdict to CloudWatch Logs, and a reconciliation sweep that catches any file that somehow never got scanned.


### Why it's built this way

| Choice | Reasoning |
|---|---|
| **SQS between S3 and the app** | The queue absorbs bursts (nothing is lost if uploads outpace scanning), gives every file automatic retries, and its depth is the signal that scales the app's pods. After three failed attempts a message moves to a dead-letter queue, where a Lambda retries it with backoff — poison files can't wedge the pipeline. |
| **Python + asyncio** | The app is a traffic director, not a compute engine — nearly all of its time is spent *waiting* on network I/O (S3, SQS, the scanner). Async lets one small pod hold 50 scans in flight. A compiled language would not speed this up: the scanner backend is the bottleneck, deliberately. |
| **Files scanned from memory** | `scanBuffer`-style scanning means no temp files — which is what allows the container to run with a read-only filesystem, and leaves nothing to clean up. Files over `MAX_FILE_SIZE_MB` (default 500) are routed by server-side S3 copy instead, so a huge file can never blow out pod memory. |
| **Visibility heartbeat + fast retry** | While a long scan runs, the app keeps extending the SQS message's invisibility so no other pod grabs it. On failure it does the opposite — shortens the timer to ~30s so the retry happens quickly instead of waiting out a long timeout. |
| **Never mark the unscanned clean** | The routing rule you saw in §5: an archive the scanner couldn't fully open (decompression limits) goes to *quarantine with explanatory tags* — or to the review pipeline if enabled — never tagged clean. A clean verdict means a completed scan. |
| **Tag, don't delete, on your buckets** | In existing-bucket mode the app writes a verdict tag on your object instead of removing it — and its IAM role simply has no delete permission there. The safety property is enforced by AWS, not by good intentions in code. |
| **Hardened container** | Non-root user, read-only root filesystem, all Linux capabilities dropped, dependencies pinned to exact versions, images tagged by git commit — the app that handles potentially-malicious bytes is the one you most want locked down. |


### Where to read the code

| You want to see… | Look at |
|---|---|
| The whole flow in 20 lines of comments | `app/scanner.py` — module docstring at the top |
| Poll loop & concurrency cap | `_poll_loop()` / `_guarded_process()` |
| Event parsing (S3 vs EventBridge shapes) | `_extract_records()` |
| Scan call & routing decision | `_process_record()` |
| Delete-vs-tag finalization | `_finalize_source()` |
| Every knob, with defaults and validation | `app/config.py` |

If your own application will replace this module (`DeployScannerApp=false`), these same patterns — reuse connections, bound your concurrency, heartbeat long scans, never trust a partial scan — are the ones worth carrying over. Part II shows the connection half in Python.


## 7. What the bastion does

CloudFormation can build AWS resources, but it cannot talk to a Kubernetes API, run `helm install`, or build a container image. Someone has to do that part — normally *you*, command by command, from a machine with network access to the cluster. The **bastion host** is that machine, automated: a small EC2 instance inside the VPC that runs the entire post-provisioning sequence in `scripts/bootstrap.sh`, then tells CloudFormation whether everything succeeded.

Why it exists, in three points:

| Reason | Detail |
|---|---|
| **Something must bridge CloudFormation → Kubernetes** | Helm releases, namespaces, secrets, and StorageClasses aren't CloudFormation resources. The bastion runs those steps and signals success or failure back, so a broken install fails the stack loudly instead of leaving a half-configured cluster. |
| **It must sit inside the VPC** | The EKS API endpoint is private — only reachable from within the network. An external machine couldn't run these steps without extra plumbing. |
| **It stays useful after deployment** | It's your pre-configured operations seat: `kubectl`, `helm`, and cluster credentials ready for the scan-policy changes in [§16](#16-tune-the-scan-policy), upgrades, and troubleshooting. Access is exclusively `aws ssm start-session` — the bastion's security group has **no inbound rules at all**. No SSH, no public exposure. |

Its lifecycle: CloudFormation hands it ~30 environment variables (resource names, mode flags), it clones this repository, runs `bootstrap.sh`, and signals the result. Everything it does is logged to `/var/log/cloud-init-output.log` — the first place to look if a deployment fails ([§18](#18-troubleshooting)).


### The commands it runs for you

Every step below is something you would otherwise type yourself, in this order. Module-gated steps are marked.


### Phase 1 — Install the toolchain

| Command | What it does | Reference |
|---|---|---|
| `apt-get install … curl gpg jq unzip` | Base utilities for everything below | — |
| `awscli-exe-linux-x86_64.zip → ./aws/install` | AWS CLI v2 — every AWS call the bastion makes | [AWS CLI install](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| `apt-get install helm` | Helm — installs the V1FS chart and platform components | [Helm install](https://helm.sh/docs/intro/install/) |
| `curl …/kubectl → /usr/local/bin` | kubectl — all Kubernetes operations | [kubectl install](https://kubernetes.io/docs/tasks/tools/) |
| `eksctl → /usr/local/bin` | eksctl — EKS utility, kept available for operations | [eksctl](https://eksctl.io/) |
| `apt-get install docker-ce` *(app module)* | Docker — builds the scanning-app image | [Docker install](https://docs.docker.com/engine/install/ubuntu/) |


### Phase 2 — Connect to the cluster and register the scanner

| Command | What it does | Reference |
|---|---|---|
| `aws eks update-kubeconfig --name ` | Writes the kubeconfig so kubectl can authenticate to your new cluster | [EKS kubeconfig](https://docs.aws.amazon.com/eks/latest/userguide/create-kubeconfig.html) |
| `kubectl create namespace visionone-filesecurity` | Home for the scanner release | [Namespaces](https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/) |
| `curl -X POST …/beta/fileSecurity/ctr/registration` | Mints the scanner registration token from the Vision One API using your API key — replacing the manual console step *File Security → Get registration token*. (Skipped if you passed `RegistrationToken` yourself.) | [Automation Center](https://automation.trendmicro.com/xdr/home) |
| `kubectl create secret generic token-secret …` | Stores that token where the chart expects it — the credential the scanner uses to register with Vision One on first start | [K8s Secrets](https://kubernetes.io/docs/concepts/configuration/secret/) |


### Phase 3 — Install platform components

| Command | What it does | Reference |
|---|---|---|
| `helm install aws-load-balancer-controller eks/…` | The controller that turns Kubernetes Services and Ingresses into real NLBs/ALBs — it creates the scanner endpoint | [LB Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/deploy/installation/) |
| `kubectl apply -f …metrics-server…/components.yaml` | CPU/memory metrics — the chart's HPA cannot scale without it | [metrics-server](https://github.com/kubernetes-sigs/metrics-server) |
| `helm install cluster-autoscaler autoscaler/…` | Node autoscaling — grows and shrinks the node group as pods need capacity | [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler/cloudprovider/aws) |
| `helm install keda kedacore/keda` *(app module)* | Queue-depth autoscaling for the scanning app | [KEDA deploy](https://keda.sh/docs/latest/deploy/) |
| `kubectl apply` — `gp3` + `efs-sc` StorageClasses | Storage for the chart's database volume (encrypted EBS gp3) and the scanners' shared scratch space (EFS, multi-pod) | [StorageClasses](https://kubernetes.io/docs/concepts/storage/storage-classes/) |


### Phase 4 — Install and configure the V1FS scanner

| Command | What it does | Reference |
|---|---|---|
| `helm repo add visionone-filesecurity …` + `gpg --import` + `helm pull --verify` | Adds TrendAI's chart repository and cryptographically verifies the chart signature before installing anything | [Helm provenance](https://helm.sh/docs/topics/provenance/) |
| `helm install my-release … --version 1.4.10 -f values-base.yaml` | The scanner itself — pinned version, chart HPA **disabled** (KEDA scales the scanner on queue depth instead), storage wired to the classes above, endpoint mode applied | [V1FS Helm chart](https://trendmicro.github.io/visionone-file-security-helm/) |
| `kubectl exec … clish scanner scan-policy modify …` | Applies the four decompression limits from your stack parameters — the scanner ships with no limits (see [§16](#16-tune-the-scan-policy)) | [Containerized Scanner](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-containerized-scanner) |
| `helm install rv …` *(review module)* | The second, no-limits scanner release in its own namespace | [V1FS Helm chart](https://trendmicro.github.io/visionone-file-security-helm/) |


### Phase 5 — Build and deploy the scanning app *(app module)*

| Command | What it does | Reference |
|---|---|---|
| `aws ecr get-login-password \| docker login …` | Authenticates Docker to your private ECR registry | [ECR push](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html) |
| `docker build` + `docker push` (git-SHA tag) | Builds the scanning-app image from this repository and pushes it — immutable tags, never `:latest` | [ECR push](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html) |
| `kubectl apply` — ServiceAccount, NetworkPolicy, ConfigMap, Deployment, PodDisruptionBudget, ScaledObject | Deploys the app with queue/bucket configuration substituted in, egress locked down, and KEDA scaling attached | [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) |


### Phase 6 — Finish

| Command | What it does | Reference |
|---|---|---|
| `kubectl get svc …` + `aws ssm put-parameter` | Waits for the load-balancer hostname and publishes the scanner endpoint to `//scanner-endpoint` | [Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html) |
| `aws cloudformation signal-resource --status SUCCESS\|FAILURE` | Reports the outcome — CloudFormation marks the stack complete only if every step above worked | [CreationPolicy](https://docs.aws.amazon.com/AWSCloudFormation/latest/userguide/aws-attribute-creationpolicy.html) |

> [!NOTE]
> **Using the bastion yourself later:** `aws ssm start-session --target `, then `sudo su -` — kubectl and helm are ready to go.


---

# Part II — Integrate


## 8. Find your endpoint

If your own application will submit the scans, it talks to the scanner through a **load balancer endpoint** using **gRPC** — a fast binary protocol over HTTP/2. Good news up front: *you never write gRPC code.* The SDK's client class wraps all of it. You give it an address, call a scan method, and get back JSON with the verdict.

```text
[Your Java app (AMaasClient)] ──gRPC──▶ [Endpoint (NLB :50051 or ALB :443)] ──▶ [V1FS scanner pods (scan & verdict)]
```

The deployment publishes the address to an SSM parameter:

```bash
aws ssm get-parameter --name /v1fs-eval-1/scanner-endpoint \
  --query Parameter.Value --output text
# → k8s-visionon-xxxx.elb.us-east-1.amazonaws.com:50051
```

| Endpoint mode | Address looks like | Encryption | Reachable from |
|---|---|---|---|
| **NLB** (default) | `k8s-…elb.amazonaws.com:50051` | None — plaintext gRPC | Inside the VPC (or peered networks) only |
| **ALB** | `scanner.example.com:443` | TLS | Inside the VPC, via your DNS name |

> [!IMPORTANT]
> **The plaintext NLB endpoint does not check your API key.** Network access *is* the access control — that's why it's VPC-internal only. Still pass a real API key in your code: the same code then works unchanged against TLS endpoints and Trend's cloud service, which do enforce it.

The section below (§9) uses **Python** for the basic single reused-connection client. But this scenario runs the app in **pull** dispatch mode, so read **§8a** first — a single connection alone will hot-spot one scanner pod.

## 8a. Scaling a high-volume client — the pull / semaphore dispatcher

This scenario runs the app in **`DISPATCH_MODE=pull`** (`ScannerDispatchMode=pull`) — the same one app from §6, in its pull dispatch mode rather than a separate program. Instead of one connection to the in-cluster Service, the app spreads scans across the scanner fleet itself; how it does that matters as much as the scanner's own autoscaling for a busy, queue-fed workload. This section explains the architecture.

### Why a single connection hot-spots

gRPC opens **one** connection and reuses it for every scan (§11) — the efficient, recommended pattern. But the NLB is **Layer 4**: it balances whole *connections*, choosing a backend pod once at connect time and pinning every packet on that connection to it. So one reused client connection sends **all** its scans to **one** scanner pod — the other pods sit idle, no matter how far KEDA scales the fleet. And an L7 ALB would fix the balancing but add a termination hop of latency. Neither is ideal for a latency-sensitive, high-volume client.

### The answer: pull the work, discover the pods, balance in the client

Move the balancing into the client and let it flow by demand — the *competing-consumers* pattern, with no load balancer in the scan path:

```text
                 ┌─ worker ─┐   acquire least-busy pod slot
 SQS queue ──▶   │  pool    │ ─────────────────────────────▶  scanner pod A  [■■□]  (2/3 busy)
 (your work) ◀── │ (compete │ ◀── verdict ── direct gRPC ───  scanner pod B  [■□□]  ← picked (most free)
     ack/redeliver│ for msgs)│                                 scanner pod C  [■■■]  (full, skipped)
                 └──────────┘        ▲
                 pod roster + health ─┘  ELB DescribeTargetHealth on the NLB target group (every ~20s)
```

Three moving parts:

1. **Pod discovery via the NLB target group.** The NLB does double duty as a **registry**: with `target-type=ip` its target group tracks the live scanner pod IPs and health-checks each one. The client reads them with the ELB `DescribeTargetHealth` API (no Kubernetes access needed) and connects **directly** to pods — the NLB is never in the scan path (no L4 pinning, no L7 latency). A background refresh picks up pods as KEDA scales the fleet and drains them on scale-down.
2. **A per-pod client pool with capacity semaphores.** One `AMaasClient` (one reused connection) per scanner pod, each guarded by a semaphore = its free scan "slots."
3. **Least-busy dispatch + backpressure.** Each scan goes to the pod with the **most free slots**. If every pod is full, the worker blocks until one frees — that backpressure ripples out to the queue, which simply holds the backlog (nothing is dropped). A pod stuck on a slow file naturally receives less, so load self-levels without anyone computing it.

### Reliability — two independent safety nets

- **The queue** — a message is deleted only after a successful scan + action; any failure leaves it to **redeliver** to another worker. Nothing is lost if a worker or pod dies mid-scan.
- **The dispatcher** — a pod-level gRPC error is retried on a **different** pod; a pod that goes unhealthy/draining is dropped from rotation on the next refresh.

### It's the app's pull mode, not a separate program

Everything above is built into the one app from §6 and is active whenever `ScannerDispatchMode=pull` — you don't build a separate consumer. The Python flavor's pull-mode code (SQS drain, target-group discovery, the per-pod handle pool, least-busy dispatch, and both reliability nets) lives in [`app/`](../../app/). A standalone, adapt-me illustration of the same pattern — useful if you're porting it into your own service — is in [`reference/python-KEDA/`](.) (`consumer.py` + `scanner_pool.py`). The full design rationale (L4 vs L7, why pull beats push, the SDK channel constraints) is in `temp/scanner-load-balancing.html`.

> The V1FS SDK builds a bare gRPC channel (no client-side `round_robin`/`least_request` and no channel injection), so this client-side dispatcher — the app's pull mode, not SDK config — is how a single client distributes its scans across the fleet.

## 9. Python: connect and scan

Install the SDK (`pip install visionone-filesecurity`), then `init()` **once** and
reuse the handle for every scan — one connection, JSON verdicts.

```python
import amaas.grpc

# region=None for a self-hosted endpoint; host is the SSM-published address.
handle = amaas.grpc.init(endpoint, api_key, enable_tls, ca_cert)   # once, reused
try:
    with open(path, "rb") as f:
        result = amaas.grpc.scan_buffer(handle, f.read(), path, tags=[])
    # result is JSON: scanResult 0 = clean, 1 = malware; foundMalwares[] on a hit.
    print(result)
finally:
    amaas.grpc.quit(handle)
```

- **enable_tls** — `False` for the plaintext in-VPC NLB (default), `True` for the ALB (`:443`).
- **ca_cert** — path to the self-signed cert PEM when using the ALB with `SelfSignedScannerCert=true`; otherwise `None`.
- **Anti-pattern** — never `init()` per request or per file: you pay full connection setup each time and leak channels. Build the handle once and share it.

In this scenario the deployed app already runs in pull mode (`DISPATCH_MODE=pull`, §8a): it drains SQS, discovers scanner pod IPs from the NLB target group, and dispatches across a per-pod handle pool with least-busy balancing and two-layer redelivery reliability — all built into the Python app at [`app/`](../../app/). A standalone, adapt-me illustration of the same pattern, for porting into your own service, is in [`reference/python-KEDA/`](.) (`consumer.py` + `scanner_pool.py`).

## 16. Tune the scan policy

The decompression limits that produce `ATSE_*` errors are set at deploy time by template parameters, and can be changed live — no restart, effective immediately:

| Parameter | Default | Protects against |
|---|---|---|
| `MaxDecompressionLayer` | 10 | Deeply nested archives (zip in zip in zip…) |
| `MaxDecompressionFileCount` | 1000 | File-count bombs |
| `MaxDecompressionRatio` | 150 | Classic zip bombs (tiny file → huge payload) |
| `MaxDecompressionSize` | 512 MB | Memory/disk exhaustion |

View or change on a live cluster (run from the bastion host via Session Manager):

```bash
kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- clish scanner scan-policy show

kubectl exec deploy/my-release-visionone-filesecurity-management-service \
  -n visionone-filesecurity -- clish scanner scan-policy modify \
  --max-decompression-layer=15
```


## 17. Scaling expectations

The scanner fleet is scaled by **KEDA on the scan queue's depth** (1–10 pods by default), so it grows and shrinks with your backlog rather than with CPU load; the cluster adds nodes through the standard Kubernetes Cluster Autoscaler.

> **Configuration note.** This is a queue-driven variant. The chart's own CPU/memory HPA is **disabled** (`scanner.autoscaling.enabled=false`) so it doesn't fight KEDA — which means scanner scaling here differs from TrendAI's *supported* chart-HPA mechanism. Run Helm upgrades **only** through `scripts/upgrade.py` (never a bare `helm upgrade`): it keeps the chart HPA off and fails the upgrade if one reappears alongside KEDA (two autoscalers would thrash the replica count).

| What you'll observe | Why it's normal |
|---|---|
| The scanner fleet tracks queue depth (≈1 pod per 50 queued messages, up to `ScannerMaxReplicas`) | KEDA reads SQS depth every few seconds and scales the scanner deployment directly |
| Scale-up still takes **1–3 minutes** when new *nodes* are needed | Cluster Autoscaler provisions capacity before newly-requested pods can schedule |
| Nothing is lost during a burst | the SQS queue holds the backlog and drains as capacity arrives |
| Repeat scans of the same file are near-instant | the scanner caches verdicts by file hash. For honest benchmarks use unique files, or restart the scan-cache deployment between runs |

Raise the ceilings with the `ScannerMaxReplicas`, `ScannerAppMaxReplicas`, and `NodeGroupMaxSize` parameters if your evaluation needs more sustained throughput.

**Node instance type.** `NodeInstanceType` accepts memory-optimized xlarge classes. The default is **Graviton ARM** (`r8g.xlarge`) — 11–19% cheaper per node-hour than the x86 equivalents with identical functionality; the template selects the matching ARM64 node image and builds the scanning application for ARM automatically. Select an x86 class (`r7i.xlarge`, `r7a.xlarge`, `r6i.xlarge`, `r7i.2xlarge`) if you prefer Intel/AMD.


## 17a. Upgrading the V1FS scanner (KEDA mode — read before you upgrade)

The chart version is pinned (currently **1.4.10**). Because this option scales the scanner with **KEDA on queue depth**, the chart's own HPA is turned **off** (`scanner.autoscaling.enabled=false`) — two autoscalers on one Deployment would fight over the replica count. That makes the upgrade path slightly different from the TrendAI-supported chart-HPA build, and there is exactly one rule to internalize: **the chart HPA must never come back.** `scripts/upgrade.py` enforces that for you.

**Always upgrade from the bastion with the script — never a bare `helm upgrade`:**

```bash
# Preview the exact helm command first (recommended on any chart bump)
python3 /opt/eks-v1fs/scripts/upgrade.py --version 1.4.11 --dry-run

# Apply
python3 /opt/eks-v1fs/scripts/upgrade.py --version 1.4.11
```

What the script guarantees in KEDA mode:

| Step | Guarantee |
|---|---|
| Values | Layers `helm/values-base.yaml` (HPA off) over the release's captured install values, and additionally passes `--set scanner.autoscaling.enabled=false` — so a new chart version's default cannot silently re-enable the chart HPA |
| Bounds | Passes **no** HPA replica bounds — in this mode they live on the KEDA `v1fs-scanner-sqs-scaler` ScaledObject, which Helm never touches |
| Guard | After upgrading, verifies the scanner has the KEDA ScaledObject and **no** chart HPA. If both are present, the script **fails hard (exit 1)** so you catch the conflict immediately instead of shipping a thrashing deployment |
| Scan policy | Re-captures and re-applies your CLISH decompression settings (§16) as a safeguard |

**Never use `helm upgrade --reuse-values`** — if a new chart version renames or adds a value, `--reuse-values` can drop `scanner.autoscaling.enabled=false` and resurrect the HPA. Always go through the script.

**To change scanner min/max replicas in this mode,** edit `k8s/scanner-scaledobject.yaml` and re-apply it — do **not** set `scanner.autoscaling.*` (that path is disabled here).


## 18. Troubleshooting

| Symptom | Where to look |
|---|---|
| Stack stuck >40 min, or bastion signals FAILURE | Session Manager onto the bastion → `tail -100 /var/log/cloud-init-output.log`. The bootstrap logs every step it runs. |
| Stack fails creating `NGWEIP*` or `BaseVPC` | EIP or VPC quota exhausted — a deleting stack still holds its quota. Wait for deletions to finish, or raise the quota. |
| Files sit in the ingest bucket unscanned | Check the DLQ (in stack outputs) for failed messages; check app logs: `kubectl logs -l app=scanner-app -n visionone-filesecurity` |
| HPA shows `` targets | metrics-server hiccup: `kubectl get pods -n kube-system \| grep metrics` |
| Your app can't reach the endpoint | Your app must run inside (or be routed into) the VPC. Test reachability first: `nc -zv 50051` |
| Registration or auth failures at install | API key lacks the SDK-scan permission, or wrong regional endpoint — check `VisionOneApiEndpoint` for non-US tenants |


## 19. Teardown

One command removes the stack:

```bash
aws cloudformation delete-stack --stack-name v1fs-eval-1
```

Before CloudFormation tears down the cluster, a pre-delete cleanup Lambda removes the resources Kubernetes created *outside* CloudFormation — otherwise they'd orphan and block VPC deletion. It handles: the scanner load balancer(s), their target groups and security groups, the V1FS EBS volumes (polling a few rounds so volumes still detaching are caught), and the service-created CloudWatch log groups (EKS control plane, Lambda execution logs). Errors are non-fatal, so a hiccup here never blocks the delete.

**What CloudFormation deletes automatically:** the EKS cluster, node group, NAT gateways and EIPs, EFS, SQS queues, DLQ Lambdas, IAM roles, dashboard, alarms, SSM parameter, VPC — everything that costs money to run.

**What deliberately survives — clean up by hand if you want it gone:**

| Left behind | Why | Remove it |
|---|---|---|
| **The verdict buckets** (ingest / quarantine / review) | `DeletionPolicy: Retain` — kept on purpose so scanned files and verdicts survive teardown (forensics). They're versioned. | Empty (including versions) and delete: `aws s3 rb s3://<bucket> --force` |
| **Secrets Manager secrets** (API key, and registration token if you supplied one) | Deletion enters a 7–30 day recovery window rather than removing immediately; the name stays reserved meanwhile (another reason to increment stack names). | `aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery` |
| **The cleanup Lambda's own log group** (`/aws/lambda/cleanup-<stack>`) | It's still in use while doing the cleanup, so it can't delete itself. | `aws logs delete-log-group --log-group-name /aws/lambda/cleanup-<stack>` (negligible cost otherwise) |
| **Your existing ingest bucket** (existing-bucket mode) | Never touched on teardown — the bucket, its objects, and its notification configuration are left exactly as found. | Not applicable — intentional |

A quick post-delete sweep to confirm a clean account:

```bash
aws ec2 describe-volumes --filters Name=status,Values=available --query 'Volumes[].VolumeId' --output text
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName' --output text
aws ec2 describe-vpcs --filters Name=is-default,Values=false --query 'Vpcs[].VpcId' --output text
```

Anything returned that carries the stack's name is safe to delete.


## 20. Quick reference

| Task | How |
|---|---|
| Deploy (full pipeline) | `create-stack … ParameterKey=ApiKey,ParameterValue=` |
| Deploy (endpoint only) | add `ParameterKey=DeployScannerApp,ParameterValue=false` |
| Scan via S3 | `aws s3 cp s3:///` |
| Get the endpoint | `aws ssm get-parameter --name //scanner-endpoint` |
| Connect (Java, NLB) | `new AMaasClient(null, host, key, 300, false, null)` |
| Scan (Java) | `client.scanFile(path, true, options)` / `scanBuffer(bytes, name, true, options)` |
| Concurrency | Share one client across threads; `close()` once at shutdown |
| Clean | `scanResult == 0` and `foundErrors` empty |
| Malware | `scanResult > 0` — names in `foundMalwares[].malwareName` |
| Not fully scanned | `scanResult == 0` with `foundErrors` entries (`ATSE_*`) |
| Scan policy | `clish scanner scan-policy show\|modify` on the management service |
| Teardown | `delete-stack`, then empty retained buckets |


## 21. References

Public documentation germane to this deployment, grouped by what you're trying to understand.


### TrendAI Vision One File Security — product documentation

| Resource | What it covers |
|---|---|
| [What is File Security?](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-what-is-file-security) | Product overview: detection engines, use cases, how File Security fits in Vision One |
| [File Security Containerized Scanner](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-containerized-scanner) | The self-hosted scanner this stack deploys — requirements, registration, deployment overview |
| [ICAP Protocol and Containerized Scanner](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-icap-protocol-file-security-scanner) | The ICAP interface the scanner also exposes (port 1344 on the endpoint) for ICAP-speaking clients |
| [Supported Helm Versions](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-supported-helm-versions) | Which chart/app versions are supported — check before upgrading |
| [File Security FAQs](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-file-security-faqs) | Common questions on scanning behavior, file handling, and privacy |
| [Vision One Data Collection Notice](https://success.trendmicro.com/en-US/solution/KA-0010805) | Exactly what telemetry/metadata the product sends to Trend — useful for security reviews |
| [Vision One Automation Center](https://automation.trendmicro.com/xdr/home) | The Vision One API reference — including the File Security endpoints used for registration-token retrieval |


### Official Trend charts, modules & SDKs

| Resource | What it covers |
|---|---|
| [File Security Helm chart](https://trendmicro.github.io/visionone-file-security-helm/) | The chart this stack installs (pinned version) — values reference, defaults, release index |
| [File Security Terraform module](https://github.com/trendmicro/visionone-file-security-terraform) | Trend's official Terraform deployment — a useful second reference for supported topologies (EKS with ALB Ingress, autoscaling parameters) |
| [Java SDK](https://github.com/trendmicro/tm-v1-fs-java-sdk) | The SDK used in Part II — `AMaasClient`, scan methods, thread-safety notes |
| [Python SDK](https://github.com/trendmicro/tm-v1-fs-python-sdk) | Same concepts in Python (`amaas.grpc`) — what the bundled scanning app uses |
| [Go SDK](https://github.com/trendmicro/tm-v1-fs-golang-sdk) | Go client; its README also documents the gRPC port and TLS defaults |
| [Node.js SDK](https://github.com/trendmicro/tm-v1-fs-nodejs-sdk) | Node client for the same gRPC interface |
| [Serverless scanning reference (Lambda/SQS)](https://github.com/trendmicro/tm-v1-filesecurity) | Trend's own event-driven S3-scanning architecture — the non-containerized sibling of the pipeline in this stack; good for comparing approaches |


### AWS building blocks used by this deployment

| Resource | Where this stack uses it |
|---|---|
| [EKS Managed Node Groups](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html) | The single node group hosting all workloads |
| [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) | How every pod gets AWS permissions — no IRSA annotations, no static keys |
| [EKS Best Practices — Cluster Autoscaling](https://docs.aws.amazon.com/eks/latest/best-practices/cluster-autoscaling.html) | Background for the node-scaling behavior described in §17 |
| [S3 Event Notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html) | How the stack-created ingest bucket announces new objects to SQS |
| [S3 → EventBridge notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventBridge.html) | The wiring used in existing-bucket mode (preserves your bucket's own notification config) |
| [SQS Dead-Letter Queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html) | Where messages go after three failed scan attempts, before Lambda-driven retry |


### Kubernetes ecosystem components

| Resource | Where this stack uses it |
|---|---|
| [Cluster Autoscaler on AWS](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler/cloudprovider/aws) | Node scaling — configuration flags, ASG auto-discovery |
| [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/) | Creates the scanner's NLB (or ALB) — service annotations, ingress annotations, IAM policy |
| [KEDA — AWS SQS scaler](https://keda.sh/docs/latest/scalers/aws-sqs/) | Queue-depth scaling — for both the bundled scanning app AND the V1FS scanner fleet on this branch (the chart's own HPA is disabled) |


### Testing

| Resource | What it covers |
|---|---|
| [EICAR test file](https://www.eicar.org/download-anti-malware-testfile/) | The harmless industry-standard detection test string used throughout this guide |

Deployment internals beyond this guide: the repository's README and docs/ directory.
