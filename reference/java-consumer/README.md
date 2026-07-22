# Reference Java consumer — SQS-driven, pull/competing-consumers scanning

A worked example for a deployment where an **API gateway (or Lambda) feeds an
SQS queue** and a long-lived service drains it, scans each file, and acts on the
verdict. It shows how to keep the **scanner-facing leg self-balancing without a
load balancer in the path** — no L4 connection hot-spot, no L7 latency.

> **Reference only.** This is *not* built or deployed by the CloudFormation
> stack. It is illustrative; adapt package names, the `scanBuffer` call, and the
> `act()` step to your codebase and SDK version.

## How it works

```
SQS queue ──(pull)──▶ worker pool ──(pull)──▶ scanner pod with a free slot ──▶ verdict ──▶ act ──▶ delete msg
   └──────── outer competing-consumers ────────┘   └──── inner competing-consumers ────┘
```

- **Outer pull (`Main`)** — a fixed pool of workers long-poll the same queue and
  compete for messages. A busy worker pulls less; work flows to whoever is free.
  A message is **deleted only after** a successful scan + action; any failure
  leaves it for SQS to **redeliver** (reliability net #1).
- **Inner pull (`ScannerPool`)** — each worker hands its scan to the scanner pod
  with the **most free capacity** (per-pod semaphores = "free slots"). If a pod
  errors, the scan is retried on a **different** pod (net #2). If every pod is
  saturated, the worker blocks (backpressure) until a slot frees.
- **Pod discovery** — the pool reads the live, healthy scanner pod IPs from the
  **NLB target group** (`target-type=ip`) via the ELB `DescribeTargetHealth`
  API, and connects **directly** to pod IPs. The NLB is only a discovery
  registry; it is never in the scan path. A background refresh (every 20s) picks
  up pods as KEDA scales the fleet and drains them on scale-down.

## Prerequisites

- Runs **in the VPC** — it connects straight to pod IPs (EKS VPC CNI makes them
  VPC-routable) and reads the ELB + SQS + S3 APIs. In-cluster is simplest;
  in-VPC/out-of-cluster works too (add an SG rule to reach pods on `:50051`).
- IAM for the runtime role: `elasticloadbalancing:DescribeTargetHealth`
  (+ `DescribeTargetGroups`/`DescribeTags` if you auto-discover the ARN),
  `sqs:ReceiveMessage`/`DeleteMessage`, and `s3:GetObject` on the source bucket.
- The scanner deployed with `ScannerEndpointMode=auto` (→ internal NLB) so the
  discovery target group exists.

## Config (environment variables)

| Var | Meaning |
|---|---|
| `SCAN_QUEUE_URL` | the SQS queue to drain |
| `SCANNER_TARGET_GROUP_ARN` | the scanner NLB's target group (the pod registry) |
| `V1FS_API_KEY` | Vision One API key for the SDK |
| `PER_POD_CAPACITY` | max concurrent scans per scanner pod (default 30) |
| `WORKERS` | worker threads (default ≈ total scanner capacity) |
| `SCANNER_TLS` / `SCANNER_CA_CERT` | TLS to the scanner (default plaintext, in-VPC NLB) |

Find the target group ARN once from the NLB:
```
aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName,'k8s')].TargetGroupArn" --output text
```

## Build

```
mvn -q package
java -jar target/v1fs-sqs-consumer-1.0.0.jar
```

## Why not just point the SDK at the NLB?

Because the V1FS SDK builds a bare gRPC channel (default `pick_first`, no
`round_robin`/`least_request` and no channel injection), a single client would
pin to one pod behind the L4 NLB. This consumer sidesteps that by discovering
pods and doing the balancing itself — the same effect as client-side gRPC LB,
but with real backpressure and no dependency on SDK channel configurability.
