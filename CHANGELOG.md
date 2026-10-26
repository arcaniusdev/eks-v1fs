# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release tag is what the CloudFormation template's `RepoRef` parameter
defaults to, so a template downloaded at tag `vX.Y.Z` deploys the matching
scripts, app code, and Helm values.

## [Unreleased]

## [2.5.0]
### Added
- **`V1FSChartVersion` parameter.** Empty (the new default) installs the latest
  published TrendAI File Security chart at deploy time, resolved once so the main
  and review releases match; a version such as `1.4.10` pins it. Validated on
  1.4.10 and 1.6.3.
- **PML on the containerized scanner.** Chart 1.6.x honors the SDK `pml` flag, so
  `PMLEnabled=true` now takes effect (it was a no-op on 1.4.10). The three
  reference clients read `PML_ENABLED` and pass it on every scan.
### Changed
- **New deployments default to the latest chart** instead of the pinned 1.4.10.
  Set `V1FSChartVersion` for reproducible environments.
- **`noProxy` covers the Kubernetes API server.** Required by chart 1.6.x (the
  scanner proxies HTTPS through backend-communicator). `bootstrap.sh` sets the chart
  defaults + VPC CIDR + live API server IPs; `upgrade.py` merges the live API IPs
  into each release, so existing 1.4.10 stacks upgrade to 1.6.x cleanly.
- **S3 buckets deploy without versioning.** The ingest and quarantine buckets no
  longer set `VersioningConfiguration: Enabled` (the review bucket was already
  unversioned), so evaluation stacks tear down cleanly without version/delete-marker
  purges. Enable versioning yourself if you need object-recovery on these buckets.
### Fixed
- **`upgrade.py` failed in KEDA mode under Helm 4** (server-side apply conflict on the
  scanner's `.spec.replicas`, owned by KEDA). It now renders the live replica count,
  so upgrades apply cleanly without changing the scanner fleet size.

## [2.4.0]
### Added
- **Scan-latency attribution audit fields `acquireWaitMs` + `scanCallMs`** (python and
  java flavors). Additive, non-breaking split of the unchanged `scanDurationMs`:
  `acquireWaitMs` = client-side wait for a free scanner-pod slot (`_acquire_least_busy`,
  pull mode; 0 in clusterip), `scanCallMs` = the gRPC `scan_buffer` call. Lets a high
  `scanDurationMs` be attributed to fan-out queueing vs genuine scan cost.
### Changed
- Exposed `ScannerQueueLength`, `ScannerAppPerPodCapacity`, and
  `ScannerAppMaxConcurrentScans` as CloudFormation parameters (wired via UserData →
  bootstrap/deploy env); previously runtime-only.
- Raised parameter ceilings: `ScannerMaxReplicas` MaxValue 50 → 100, `NodeGroupMaxSize`
  MaxValue 30 → 60 (for high-throughput ~50M/day profiles).
- `RepoRef` default bumped v2.3.0 → v2.4.0.

## [2.3.0]
### Changed
- **keda + pull are now the shipped defaults.** `ScannerScalingMode` defaults to
  `keda` (was `hpa`) and `ScannerDispatchMode` to `auto` (was `clusterip`) — `auto`
  resolves to `pull` when an NLB endpoint exists (the default) and `clusterip`
  otherwise. Queue-depth scaling with direct per-pod dispatch is now the
  out-of-the-box behavior for latency-sensitive workloads. `hpa` (the
  TrendAI-supported mechanism) remains available via `ScannerScalingMode=hpa`.
- **Warm floor.** `ScannerMinReplicas` default `1 → 3` and the scanner-app KEDA
  `minReplicaCount` `1 → 2`. Stops the fleet collapsing to a single pod during
  lulls: a lone pod otherwise absorbs a whole burst at high concurrency and
  per-scan latency inflates until KEDA re-scales and pull discovers the new pods
  (a warm floor materially lowers burst latency and raises throughput versus a
  cold single pod absorbing the spike).
- **Gentle KEDA scale-down** on both ScaledObjects via the HPA
  `behavior.scaleDown` (600s stabilization window, at most 1 pod/min,
  `selectPolicy: Min`); scale-up is left at the fast HPA default. Keeps the fleet
  warm and spread through traffic lulls instead of collapsing and re-expanding.
### Added
- **`auto` value for `ScannerDispatchMode`** — resolves to `pull` behind an NLB
  and `clusterip` otherwise, resolved identically in `bootstrap.sh` and the
  `PullDispatch` CloudFormation condition.
- **`NoClusterIpWithQueueScaling` CFN Rule** — blocks `clusterip` + `keda` when an
  NLB exists: the fleet grows on queue depth but each app pod's pinned gRPC
  connection never moves to the new pods, so they idle while the replica count
  looks healthy.

## [2.2.0]
### Added
- **Unified scanning app in two language flavors.** One app that does everything
  (drain SQS, scan, full routing, audit, reconciliation), available as **Python**
  (`app/`) or **Java** (`app-java/`), selected by the `ScannerAppFlavor` parameter.
  Both read the same environment contract.
- **Env-toggled dispatch** (`ScannerDispatchMode`): `clusterip` (one gRPC
  connection to the in-cluster Service) or `pull` (discover live scanner pod IPs
  from the NLB target group and dispatch each scan to the least-busy pod
  directly — no load balancer in the scan path). Pull mode adds a `PullDispatch`
  condition, an NLB-required rule, ELB pod-discovery IAM, and bastion
  target-group discovery.
### Changed
- The three `reference/` options (python-default, python-KEDA, java-KEDA) are now
  scenarios that configure this one app (flavor + dispatch + scaling) rather than
  three separate programs; POC guides rewritten to match.
- Java image builds its Maven stage natively via `$BUILDPLATFORM` (arch-independent
  jar), so cross-building for ARM nodes no longer emulates the whole build.
### Fixed
- Pull-mode target-group discovery runs on the bastion; granted the bastion role
  `elasticloadbalancing:DescribeLoadBalancers`/`DescribeTargetGroups` (without them
  discovery silently fell back to clusterip).
- Python ELB client service name corrected to botocore's `elbv2` (was
  `elasticloadbalancingv2`, which crashed pull mode at startup). The Java SDK's
  `elasticloadbalancingv2` name is correct and unchanged.

## [2.1.1]
### Fixed
- External-queue mode: an empty `S3_INGEST_BUCKET` no longer trips `deploy.sh`'s
  stack-Outputs fallback (which failed during `CREATE_IN_PROGRESS`). Optional
  buckets are bound under `set -u`; `get_output` hardened against null Outputs.

## [2.1.0]
### Added
- **Tag-in-place routing**: clean files are tagged `S3-Clean` in the ingest bucket
  (never moved); malicious files move to quarantine; decompression-limit files get
  a distinct `S3-DecompressionLimit` + `ScanErrors` tag.
- **External-queue drain mode**: `ExternalScanQueueArn` + `ExternalScanSourceBucketArns`
  let the scanner-app drain a user-owned SQS queue instead of a stack-built one.
- Documented the KEDA-mode safe Helm-upgrade path in the README and both KEDA POC guides.
### Changed
- **Graviton ARM (`r8g.xlarge`) is the default node type.**
### Removed
- The dedicated "clean bucket" (clean files are now tagged in place).

## [2.0.0]
### Added
- **Three deployment options** from one template via `ScannerScalingMode`
  (`hpa` | `keda`): `reference/python-default` (chart-HPA, TrendAI-supported),
  `reference/python-KEDA` and `reference/java-KEDA` (KEDA queue-depth scaling +
  client-side pull/semaphore dispatcher), each with its own POC guide.

## [1.0.2]
### Added
- **Graviton (arm64) node support** (`r8g`/`r7g`), with automatic AMI selection
  and cross-arch scanner-app image build.
### Fixed
- Missing `import re` in `scanner.py` (present in the broken `1.0.1`).

## [1.0.1]
Broken — missing `import re` crashes the scanner-app. Do not deploy; use `1.0.2`.

## [1.0.0]
### Added
- Initial evaluation-friendly EKS deployment of the TrendAI Vision One File
  Security containerized scanner, aligned with TrendAI's supported methodology
  (chart-native HPA, Cluster Autoscaler, pinned chart). Single CloudFormation
  template with parameter-toggled modules (scanner-app, review pipeline,
  existing-bucket, endpoint exposure).
